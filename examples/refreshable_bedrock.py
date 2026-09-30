"""Lazy, refresh-aware credentials for direct Amazon Bedrock Runtime calls.

The broker remains out of the inference path. Botocore asks this provider for
credentials on the first signed Bedrock request and near the effective lease
expiration. All calls between refreshes reuse the cached credential set.

Two levels of integration are offered:

- ``BedrockSpendControls`` is the one-object-per-process entry point for an
  application backend that serves many end users. ``client_for(user_jwt)``
  returns an ordinary ``bedrock-runtime`` client whose credentials belong to
  that user, and keeps one cached credential set per quota identity behind
  the scenes. This is what most applications should use.
- ``QuotaBrokerCredentialProvider`` is the single-user building block the
  factory is made of: one JWT, one lease, one refreshable credential object.
  Use it directly for CLIs, notebooks, or when you already manage sessions.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import boto3
import httpx
from botocore.credentials import DeferredRefreshableCredentials
from botocore.exceptions import CredentialRetrievalError
from botocore.session import get_session

try:
    from .sigv4_gateway import signed_request
except ImportError:  # Direct execution from the examples directory.
    from sigv4_gateway import signed_request


class BrokerCredentialError(CredentialRetrievalError):
    """The broker refused or could not complete a credential refresh.

    ``error_type`` carries the broker's machine-readable code. The quota
    refusals also arrive as the typed subclasses below so application code
    can catch what it cares about without inspecting strings.
    """

    def __init__(
        self,
        message: str,
        *,
        terminal: bool,
        error_type: str = "",
        status_code: int | None = None,
        retry_after: int | None = None,
        resets_at: datetime | None = None,
        breached_period: str = "",
        breached_dimension: str = "",
    ):
        super().__init__(provider="bedrock-spend-controls-broker", error_msg=message)
        self.terminal = terminal
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = retry_after
        self.resets_at = resets_at
        self.breached_period = breached_period
        self.breached_dimension = breached_dimension


class QuotaExceededError(BrokerCredentialError):
    """The user reached a ``block`` threshold (HTTP 429 ``quota_exceeded``).

    ``resets_at`` is the UTC end of the exhausted calendar window and
    ``breached_period`` / ``breached_dimension`` name what ran out
    (``daily`` / ``usd``, ``weekly`` / ``input_tokens``, ...).
    """


class UserBlockedError(BrokerCredentialError):
    """The user's status is ``blocked`` (HTTP 403 ``quota_blocked``).

    Automatic blocks lift on their own once every enabled period is back
    under quota; administrative blocks need an operator.
    """


class VendRateLimitedError(BrokerCredentialError):
    """Too many credential requests for this identity this minute (HTTP 429).

    Covers ``lease_rate_limited`` and ``lease_not_refreshable``. Usually a
    sign that the application creates a provider per request instead of
    reusing one per user; ``retry_after`` says when the next attempt may
    succeed.
    """


_TYPED_ERRORS: dict[str, type[BrokerCredentialError]] = {
    "quota_exceeded": QuotaExceededError,
    "quota_blocked": UserBlockedError,
    "lease_rate_limited": VendRateLimitedError,
    "lease_not_refreshable": VendRateLimitedError,
}


def _parse_optional_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def jwt_claim(token: str, claim: str) -> str:
    """Return one string claim from a JWT payload **without verifying it**.

    The broker is the verifier; the application only needs a stable cache
    key per quota identity, and the claim it uses for that must match the
    deployment's ``jwt_user_claim`` so one identity maps to one lease.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("user token is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("user token payload is not valid JSON") from exc
    value = claims.get(claim) if isinstance(claims, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"user token has no string claim {claim!r}")
    return value


class QuotaBrokerCredentialProvider:
    """Own one lazy, single-flight botocore credential cache per quota user."""

    def __init__(
        self,
        gateway_url: str,
        user_jwt: str | Callable[[], str],
        *,
        region: str = "us-east-1",
        aws_session: boto3.Session | None = None,
        http_client: httpx.Client | None = None,
        vend_callable: Callable[[str], dict] | None = None,
        time_fetcher: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.gateway_url = gateway_url.rstrip("/")
        self._user_token_supplier = (
            user_jwt if callable(user_jwt) else lambda: user_jwt
        )
        self.region = region
        self.aws_session = aws_session or boto3.Session(region_name=region)
        self.http_client = http_client
        self._vend_callable = vend_callable
        self._time = time_fetcher or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._max_attempts = max_attempts
        self._next_lease_id = str(uuid.uuid4())
        self._state_lock = threading.Lock()
        self._credentials_holder: dict[str, DeferredRefreshableCredentials] = {}
        credentials = DeferredRefreshableCredentials(
            refresh_using=self._refresh_metadata,
            method="bedrock-spend-controls-broker",
            time_fetcher=self._time,
        )
        self._credentials_holder["credentials"] = credentials
        self.credentials = credentials

    @staticmethod
    def _parse_time(value: str, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise BrokerCredentialError(
                f"broker response is missing {field}", terminal=True
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrokerCredentialError(
                f"broker returned invalid {field}", terminal=True
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BrokerCredentialError(
                f"broker returned timezone-naive {field}", terminal=True
            )
        return parsed.astimezone(timezone.utc)

    def _request_once(self, lease_id: str) -> dict:
        if self._vend_callable is not None:
            return self._vend_callable(lease_id)
        user_token = self._user_token_supplier()
        if not isinstance(user_token, str) or not user_token:
            raise BrokerCredentialError(
                "user token supplier returned no JWT", terminal=True
            )
        response = signed_request(
            "POST",
            self.gateway_url + "/v1/credentials",
            region=self.region,
            user_token=user_token,
            aws_session=self.aws_session,
            http_client=self.http_client,
            timeout=10,
            headers={"X-Quota-Lease-Id": lease_id},
            content=b"",
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 500:
            raise BrokerCredentialError(
                f"broker returned HTTP {response.status_code}", terminal=False
            )
        if response.status_code in {403, 409, 429}:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            error_type = str(error.get("type", "broker_refused"))
            message = str(error.get("message", error_type))
            # Quota, block, expired-ID, and premature-refresh responses stop
            # this callback immediately. Botocore can invoke a later refresh;
            # consuming the broker's whole per-minute budget in one callback
            # would turn small clock skew into self-throttling.
            error_class = _TYPED_ERRORS.get(error_type, BrokerCredentialError)
            headers = getattr(response, "headers", None) or {}
            raise error_class(
                f"{error_type}: {message}",
                terminal=True,
                error_type=error_type,
                status_code=response.status_code,
                retry_after=_parse_optional_int(headers.get("Retry-After")),
                resets_at=_parse_optional_time(headers.get("X-Quota-Resets-At")),
                breached_period=headers.get("X-Quota-Breached-Period", ""),
                breached_dimension=headers.get("X-Quota-Breached-Dimension", ""),
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerCredentialError(str(exc), terminal=True) from exc
        if not isinstance(body, dict):
            raise BrokerCredentialError(
                "broker response must be a JSON object", terminal=True
            )
        return body

    def _vend_with_retry(self, lease_id: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._request_once(lease_id)
            except (httpx.TransportError, BrokerCredentialError) as exc:
                last_error = exc
                if isinstance(exc, BrokerCredentialError) and exc.terminal:
                    raise
                if attempt + 1 == self._max_attempts:
                    break
                self._sleep(0.25 * (2**attempt))
        assert last_error is not None
        if isinstance(last_error, BrokerCredentialError):
            raise last_error
        raise BrokerCredentialError(str(last_error), terminal=False)

    def _refresh_metadata(self) -> dict[str, str]:
        # Botocore serializes this callback with its refresh lock. The local
        # lock protects lease-ID rotation for direct tests and future callers.
        with self._state_lock:
            lease_id = self._next_lease_id
            try:
                body = self._vend_with_retry(lease_id)
            except BrokerCredentialError as exc:
                if exc.error_type != "lease_expired":
                    raise
                # The broker requires a new ID after the fixed deadline. Rotate
                # once and retry; keep all transport retries for either logical
                # lease on their original ID.
                lease_id = str(uuid.uuid4())
                self._next_lease_id = lease_id
                body = self._vend_with_retry(lease_id)
            expiration = self._parse_time(body.get("expiration"), "expiration")
            refresh_raw = body.get("refresh_after")
            refresh_after = (
                self._parse_time(refresh_raw, "refresh_after")
                if refresh_raw
                else expiration - timedelta(seconds=60)
            )
            now = self._time()
            if expiration <= now:
                raise BrokerCredentialError(
                    "broker returned credentials that are already expired",
                    terminal=True,
                )
            refresh_after = min(expiration, max(now, refresh_after))
            advisory_seconds = max(
                0, int((expiration - refresh_after).total_seconds())
            )
            credentials = self._credentials_holder["credentials"]
            credentials._advisory_refresh_timeout = advisory_seconds
            credentials._mandatory_refresh_timeout = min(
                5, advisory_seconds
            )
            # Rotate only after a successful response. Transport retries use
            # the same ID, so they cannot extend an already-reserved lease.
            self._next_lease_id = str(uuid.uuid4())
            return {
                "access_key": body["aws_access_key_id"],
                "secret_key": body["aws_secret_access_key"],
                "token": body["aws_session_token"],
                "expiry_time": expiration.isoformat(),
            }

    def bedrock_client(self, **client_kwargs):
        """Return an ordinary boto3 Bedrock Runtime client using this cache."""
        botocore_session = get_session()
        botocore_session._credentials = self.credentials
        botocore_session.set_config_variable("region", self.region)
        session = boto3.Session(botocore_session=botocore_session)
        return session.client("bedrock-runtime", **client_kwargs)


# ---------------------------------------------------------------------------
# Application-facing factory: one object per process, one client per user.
# ---------------------------------------------------------------------------

_current_user_jwt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bedrock_spend_controls_user_jwt", default=None
)


class _UserEntry:
    __slots__ = ("provider", "client", "jwt", "last_used")

    def __init__(self, provider: QuotaBrokerCredentialProvider, client, jwt: str, now: float):
        self.provider = provider
        self.client = client
        self.jwt = jwt
        self.last_used = now


class BedrockSpendControls:
    """Hand out a per-user ``bedrock-runtime`` client from a shared backend.

    Create **one** instance when the process starts and call
    ``client_for(user_jwt)`` on every request. The factory keeps one
    ``QuotaBrokerCredentialProvider`` (one broker lease) per quota identity,
    reuses the boto3 client built on top of it, always presents the latest
    JWT seen for that identity when a lease is renewed, and forgets identities
    that have been idle for ``idle_ttl_seconds`` or that overflow
    ``max_users`` (least recently used first).

    ``identity_claim`` must match the deployment's ``jwt_user_claim`` (``sub``
    by default; a tenant/team claim for shared quotas) so that every JWT the
    broker maps to the same quota identity also maps to the same cached
    lease. Keying by a finer claim would still work but would spend one vend
    per user per refresh against a shared per-identity vend rate limit.

    Set ``set_current_user(jwt)`` in an authentication middleware and call
    ``client_for()`` with no argument from anywhere below it to keep the
    JWT out of every call site.
    """

    def __init__(
        self,
        gateway_url: str,
        *,
        region: str = "us-east-1",
        identity_claim: str = "sub",
        aws_session: boto3.Session | None = None,
        idle_ttl_seconds: int = 3600,
        max_users: int = 10_000,
        client_kwargs: dict | None = None,
        provider_kwargs: dict | None = None,
        time_fetcher: Callable[[], float] | None = None,
    ):
        if idle_ttl_seconds <= 0 or max_users <= 0:
            raise ValueError("idle_ttl_seconds and max_users must be positive")
        self.gateway_url = gateway_url.rstrip("/")
        self.region = region
        self.identity_claim = identity_claim
        self.aws_session = aws_session
        self.idle_ttl_seconds = idle_ttl_seconds
        self.max_users = max_users
        self._client_kwargs = dict(client_kwargs or {})
        self._provider_kwargs = dict(provider_kwargs or {})
        self._time = time_fetcher or time.monotonic
        self._entries: dict[str, _UserEntry] = {}
        self._lock = threading.Lock()

    # -- context helpers ----------------------------------------------------

    @staticmethod
    def set_current_user(user_jwt: str | None) -> contextvars.Token:
        """Bind the calling context (thread / task / request) to one user."""
        return _current_user_jwt.set(user_jwt)

    @staticmethod
    def reset_current_user(token: contextvars.Token) -> None:
        _current_user_jwt.reset(token)

    @staticmethod
    def current_user() -> str | None:
        return _current_user_jwt.get()

    # -- public API ---------------------------------------------------------

    def client_for(self, user_jwt: str | None = None):
        """Return a ``bedrock-runtime`` client authenticated as this user.

        The first call for an identity creates its provider; credentials are
        vended lazily on the first signed Bedrock request and renewed before
        the permission lease ends. Later calls with the same identity return
        the same client and record the newest JWT for the next renewal.
        """
        if user_jwt is None:
            user_jwt = _current_user_jwt.get()
        if not user_jwt:
            raise ValueError(
                "no user JWT: pass client_for(user_jwt) or call "
                "set_current_user(jwt) in your authentication middleware"
            )
        key = jwt_claim(user_jwt, self.identity_claim)
        now = self._time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._create_entry(key, user_jwt, now)
                self._entries[key] = entry
                self._evict_locked(now, keep=key)
            else:
                entry.jwt = user_jwt
                entry.last_used = now
            return entry.client

    def provider_for(self, user_jwt: str | None = None) -> QuotaBrokerCredentialProvider:
        """The underlying provider, for callers that build their own clients."""
        self.client_for(user_jwt)
        key = jwt_claim(user_jwt or _current_user_jwt.get() or "", self.identity_claim)
        with self._lock:
            return self._entries[key].provider

    def forget(self, user_jwt_or_identity: str) -> bool:
        """Drop one identity's cached lease (for example on sign-out)."""
        key = user_jwt_or_identity
        if user_jwt_or_identity.count(".") == 2:
            try:
                key = jwt_claim(user_jwt_or_identity, self.identity_claim)
            except ValueError:
                pass
        with self._lock:
            return self._entries.pop(key, None) is not None

    def active_identities(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    # -- internals ----------------------------------------------------------

    def _create_entry(self, key: str, user_jwt: str, now: float) -> _UserEntry:
        holder: dict[str, _UserEntry] = {}

        def latest_jwt() -> str:
            entry = holder.get("entry")
            return entry.jwt if entry is not None else user_jwt

        provider = QuotaBrokerCredentialProvider(
            self.gateway_url,
            latest_jwt,
            region=self.region,
            aws_session=self.aws_session,
            **self._provider_kwargs,
        )
        client = provider.bedrock_client(**self._client_kwargs)
        entry = _UserEntry(provider, client, user_jwt, now)
        holder["entry"] = entry
        return entry

    def _evict_locked(self, now: float, *, keep: str) -> None:
        stale = [
            key
            for key, entry in self._entries.items()
            if key != keep and now - entry.last_used > self.idle_ttl_seconds
        ]
        for key in stale:
            del self._entries[key]
        overflow = len(self._entries) - self.max_users
        if overflow > 0:
            by_age = sorted(
                (key for key in self._entries if key != keep),
                key=lambda key: self._entries[key].last_used,
            )
            for key in by_age[:overflow]:
                del self._entries[key]
