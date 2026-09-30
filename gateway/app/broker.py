"""Credential broker: turn a verified end-user JWT into short-lived,
per-user AWS credentials scoped to Bedrock.

This is the heart of the per-user quota model. We do NOT proxy inference
traffic; instead the user authenticates once with the JWT their app
already has, and — if they are within budget — we hand back temporary AWS
credentials they use to call Bedrock *natively* (any API, any provider,
streaming included). Enforcement and attribution both hinge on the JWT
identity:

  JWT `sub`  ->  DynamoDB budget row      (who is billed / blocked)
             ->  RoleSessionName          (shows up in model-invocation
                                            logs -> per-user metering)
             ->  SourceIdentity           (tamper-resistant; can't be
                                            changed on re-assume, so a user
                                            can't relabel as someone else)

RoleSessionName, SourceIdentity, and session-tag values each restrict which
characters they accept, and STS rejects the whole AssumeRole call if any of
them is out of range — so a raw IdP `sub` like "auth0|5f...e9" (pipe) or a
non-ASCII subject breaks vending entirely. We therefore sanitize the `sub`
into ONE collision-resistant identity (``session_name_for``) whose character
set is the intersection valid for all three, and use it for all three. A
short SHA-256 suffix of the *full* sub guarantees two distinct subs never
collapse onto one identity even after sanitization/truncation. The full,
unmodified `sub` is preserved as the DynamoDB key and in the ``SESSION#``
reverse-map row, so metering still attributes usage to the real user.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from .auth import Identity
from .config import settings
from .session_policy import permission_lease_policy

# The vended identity is used as RoleSessionName, SourceIdentity, AND a
# session-tag VALUE. Their allowed charsets differ:
#   RoleSessionName / SourceIdentity: [\w+=,.@-]
#   session-tag value:                [\p{L}\p{Z}\p{N}_.:/=+\-@]  (NO comma)
# We keep only the INTERSECTION so one sanitized value is valid in all three;
# notably comma is dropped (valid in a session name but rejected in a tag
# value, so a sub like an LDAP DN "CN=a,OU=b" would otherwise fail vending).
# re.ASCII so a non-ASCII `sub` (e.g. "josé", CJK) can't leave Unicode word
# characters that STS rejects.
_SESSION_SAFE = re.compile(r"[^\w+=.@-]", re.ASCII)
_MAX_SESSION_NAME = 64
# Hex chars of SHA-256(sub) appended to keep distinct subs from colliding onto
# one identity. 20 hex = 80 bits: birthday-safe past ~10^12 distinct subjects.
_HASH_HEX = 20


@dataclass(frozen=True)
class VendedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str  # Effective usable deadline, ISO8601.
    user_id: str
    session_name: str
    sts_expiration: str | None = None  # Actual STS credential expiration.


class BrokerError(Exception):
    """Vend refused; .status is the HTTP code, .reason is caller-safe."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def session_name_for(sub: str) -> str:
    """Map an arbitrary JWT `sub` to a single sanitized, collision-resistant
    identity valid as a RoleSessionName, SourceIdentity, AND session-tag value.

    We keep only characters in the intersection of all three fields' charsets
    (ASCII ``[\\w+=.@-]`` — see ``_SESSION_SAFE``; comma excluded because tag
    values reject it) and cap length at 64, since STS rejects the entire
    AssumeRole call if any field is out of range. IdP subs routinely violate
    this (e.g. "auth0|5f...e9", "google-oauth2|123", long GUIDs, non-ASCII).
    We strip disallowed characters to a readable prefix and append an
    ``_HASH_HEX``-char hash of the *full* sub so distinct subs don't collide
    even after sanitization/truncation. The full sub is preserved separately
    (DynamoDB key + SESSION# reverse map), so attribution is unaffected.
    """
    cleaned = _SESSION_SAFE.sub("-", sub).strip("-") or "user"
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:_HASH_HEX]
    suffix = "-" + digest
    prefix = cleaned[: _MAX_SESSION_NAME - len(suffix)]
    return prefix + suffix


class CredentialBroker:
    def __init__(
        self,
        sts_client=None,
        role_arn: str | None = None,
        ttl_seconds: int | None = None,
        lease_seconds: int | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._sts = sts_client or boto3.client("sts")
        self._role_arn = (
            role_arn
            if role_arn is not None
            else settings.bedrock_user_role_arn
        )
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else settings.vended_credential_ttl_seconds
        )
        # Fallback lease window when the caller does not supply an explicit
        # permission deadline (the vend path always does; this covers direct
        # broker use and tests). The runtime dial lives in the store.
        self._lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else settings.permission_lease_seconds
        )
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _jwt_expiration(identity: Identity) -> datetime:
        raw = identity.claims.get("exp")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BrokerError(401, "token has no valid expiration claim")
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise BrokerError(401, "token has no valid expiration claim") from exc

    def _permission_deadline(
        self, identity: Identity, now: datetime
    ) -> datetime:
        """Every credential carries a permission deadline: there is no
        deadline-free vend. The window is the lease; revocation and the
        emergency stop can only shorten effective access, never extend it.
        """
        jwt_expiration = self._jwt_expiration(identity)
        if jwt_expiration <= now:
            raise BrokerError(401, "token expiration is not in the future")
        return min(
            now + timedelta(seconds=self._lease_seconds), jwt_expiration
        )

    def vend(
        self,
        identity: Identity,
        *,
        permission_deadline: datetime | None = None,
    ) -> VendedCredentials:
        """Assume the Bedrock role on behalf of a within-budget user.

        Callers must have already: (1) verified the JWT, (2) confirmed via
        the quota layer that identity.user_id is neither blocked nor over
        budget. This method only performs the STS assumption + identity
        stamping; the budget gate lives in the request handler so a single
        DynamoDB read is shared with logging.
        """
        sub = identity.user_id
        if not sub:
            raise BrokerError(401, "token has no user identity claim")

        session_name = session_name_for(sub)
        if not self._role_arn:
            raise BrokerError(
                500,
                "broker role not configured (BEDROCK_USER_ROLE_ARN)",
            )
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise BrokerError(500, "broker clock must be timezone-aware")
        if permission_deadline is None:
            permission_deadline = self._permission_deadline(identity, now)
        else:
            if (
                permission_deadline.tzinfo is None
                or permission_deadline.utcoffset() is None
            ):
                raise BrokerError(
                    500, "permission lease deadline must be timezone-aware"
                )
            jwt_expiration = self._jwt_expiration(identity)
            permission_deadline = min(
                permission_deadline, jwt_expiration
            )
            if permission_deadline <= now:
                raise BrokerError(
                    401, "permission lease deadline is not in the future"
                )
        assume_kwargs = {
            "RoleArn": self._role_arn,
            "RoleSessionName": session_name,
            # SourceIdentity and the session tag share RoleSessionName's
            # charset limits, so stamp the same sanitized identity in all
            # three. The full JWT identity remains in the reverse map.
            "SourceIdentity": session_name,
            "DurationSeconds": self._ttl,
            "Tags": [{"Key": "quota-user", "Value": session_name}],
        }
        if permission_deadline is not None:
            assume_kwargs["Policy"] = permission_lease_policy(
                permission_deadline
            )
        try:
            resp = self._sts.assume_role(**assume_kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "STSError")
            # Trust-policy misconfig (can't set source identity / tags) is an
            # operator error, surface it distinctly from a plain deny.
            raise BrokerError(500, f"could not vend credentials ({code})")

        creds = resp["Credentials"]
        sts_expiration = creds["Expiration"].isoformat()
        effective_expiration = (
            permission_deadline.isoformat()
            if permission_deadline is not None
            else sts_expiration
        )
        return VendedCredentials(
            access_key_id=creds["AccessKeyId"],
            secret_access_key=creds["SecretAccessKey"],
            session_token=creds["SessionToken"],
            expiration=effective_expiration,
            sts_expiration=sts_expiration,
            user_id=sub,
            session_name=session_name,
        )
