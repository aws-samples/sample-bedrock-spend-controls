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

This file is self-contained: copy it into your application next to ``boto3``
and ``httpx``. It carries its own minimal SigV4 signer for the broker's
Function URL (``examples/sigv4_gateway.py`` is the full admin client).

**Security note.** ``BedrockSpendControls`` derives the cache key for a user
from a claim in the JWT *without verifying the signature*; the broker
verifies the token again on every vend, but the cached credentials are
handed out client-side before any broker call. **Pass only tokens your
application has already verified** (signature, issuer, audience, expiry),
or pass the identity you verified yourself via ``verified_identity=`` /
``client_for_identity``.
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
from email.utils import parsedate_to_datetime

import boto3
import botocore.session
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import DeferredRefreshableCredentials
from botocore.exceptions import CredentialRetrievalError

__all__ = [
    "AuthenticationError",
    "BROKER_TIMEOUT",
    "BedrockSpendControls",
    "BrokerCredentialError",
    "EmergencyStopError",
    "QuotaBrokerCredentialProvider",
    "QuotaExceededError",
    "UserBlockedError",
    "VendRateLimitedError",
    "jwt_claim",
    "signed_request",
]

#: Per-attempt broker timeout. Three attempts plus back-off stay around 20 s
#: worst case, inside the shortest refresh window the broker hands out.
BROKER_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)

# Longest server-requested pause a single refresh callback will sleep through
# before giving up and letting a later refresh retry.
_MAX_IN_CALL_RETRY_AFTER_SECONDS = 2
# Upper bound on how long a terminal refusal is served from memory.
_MAX_REFUSAL_CACHE_SECONDS = 30
# A joined lease whose refresh time is already "in the past" locally means
# the clocks disagree; wait at least this long before asking again.
_JOIN_MIN_BACKOFF_SECONDS = 2
# Date headers carry whole seconds and include network latency; offsets this
# small are measurement noise, not skew.
_SKEW_NOISE_SECONDS = 1.5


class BrokerCredentialError(CredentialRetrievalError):
    """The broker refused or could not complete a credential refresh.

    ``error_type`` carries the broker's machine-readable code. The quota
    refusals also arrive as the typed subclasses below so application code
    can catch what it cares about without inspecting strings.

    ``terminal`` means "do not retry inside this refresh callback"; it is not
    a statement about the user. ``retry_after`` (seconds), ``refresh_after``
    and ``resets_at`` (UTC) are only set when the broker sent them.
    """

    def __init__(
        self,
        message: str,
        *,
        terminal: bool,
        error_type: str = "",
        status_code: int | None = None,
        retry_after: int | None = None,
        refresh_after: datetime | None = None,
        resets_at: datetime | None = None,
        breached_period: str = "",
        breached_dimension: str = "",
    ):
        super().__init__(provider="bedrock-spend-controls-broker", error_msg=message)
        self.message = message
        self.terminal = terminal
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = retry_after
        self.refresh_after = refresh_after
        self.resets_at = resets_at
        self.breached_period = breached_period
        self.breached_dimension = breached_dimension


class AuthenticationError(BrokerCredentialError):
    """The broker rejected the user token (HTTP 401 ``authentication_error``).

    Expired or forged JWTs, a token for an unprovisioned user when
    auto-provisioning is off, and a missing ``exp`` claim all land here.
    """


class QuotaExceededError(BrokerCredentialError):
    """The user reached a ``block`` threshold (HTTP 429 ``quota_exceeded``).

    ``resets_at`` is the UTC end of the exhausted calendar window and
    ``breached_period`` / ``breached_dimension`` name what ran out
    (``daily`` / ``usd``, ``weekly`` / ``input_tokens``, ...). The broker
    only returns this on the vend that trips the block; later vends for the
    same user return ``UserBlockedError``.
    """


class UserBlockedError(BrokerCredentialError):
    """The user's status is ``blocked`` (HTTP 403 ``quota_blocked``).

    Automatic blocks lift on their own once every enabled period is back
    under quota; administrative blocks need an operator. ``resets_at`` is
    usually ``None`` here: the broker does not know when an operator acts.
    """


class VendRateLimitedError(BrokerCredentialError):
    """Too many credential requests for this identity this minute (HTTP 429).

    Covers ``lease_rate_limited`` and ``lease_not_refreshable``. Usually a
    sign that the application creates a provider per request instead of
    reusing one per user; ``retry_after`` / ``refresh_after`` say when the
    next attempt may succeed.
    """


class EmergencyStopError(BrokerCredentialError):
    """Vending is paused by the emergency stop (HTTP 503 ``emergency_stop``).

    Not terminal: the condition clears when an operator recovers. The
    provider does not retry inside one refresh callback (``retry_after`` is
    typically 60 s) and serves the refusal from memory for a short while.
    """


_TYPED_ERRORS: dict[str, type[BrokerCredentialError]] = {
    "authentication_error": AuthenticationError,
    "quota_exceeded": QuotaExceededError,
    "quota_blocked": UserBlockedError,
    "lease_rate_limited": VendRateLimitedError,
    "lease_not_refreshable": VendRateLimitedError,
    "emergency_stop": EmergencyStopError,
}
_CACHED_REFUSALS = (
    QuotaExceededError,
    UserBlockedError,
    VendRateLimitedError,
    EmergencyStopError,
)
_CREDENTIAL_FIELDS = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
)


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


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


# ---------------------------------------------------------------------------
# Minimal SigV4 signer for the broker's IAM-authenticated Function URL.
# ---------------------------------------------------------------------------


def signed_request(
    method: str,
    url: str,
    *,
    region: str,
    user_token: str,
    aws_session: boto3.Session,
    http_client: httpx.Client,
    timeout: httpx.Timeout | float | None = BROKER_TIMEOUT,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> httpx.Response:
    """Send one SigV4-signed broker request carrying the user token.

    SigV4 owns ``Authorization``; the end-user JWT travels in
    ``X-Quota-User-Token``. ``examples/sigv4_gateway.py`` has the general
    version with admin and emergency keys.
    """
    credentials = aws_session.get_credentials()
    if credentials is None:
        raise BrokerCredentialError(
            "AWS credentials are required to call the broker", terminal=True
        )
    request_headers = dict(headers or {})
    request_headers["X-Quota-User-Token"] = user_token
    request = http_client.build_request(
        method, url, headers=request_headers, content=content, timeout=timeout
    )
    request.headers.pop("Authorization", None)
    aws_request = AWSRequest(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        data=request.content,
    )
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", region).add_auth(
        aws_request
    )
    request.headers.update(dict(aws_request.headers))
    return http_client.send(request)


_shared_loader_lock = threading.Lock()
_shared_loader = None


def _shared_data_loader():
    """One botocore model loader for every client this module builds.

    Loading the service and endpoint models is what makes a fresh botocore
    session cost ~8 MiB and ~200 ms; the loader caches them, so sharing it
    across the per-user sessions brings a client down to ~0.25 MiB / ~30 ms.
    """
    global _shared_loader
    with _shared_loader_lock:
        if _shared_loader is None:
            _shared_loader = botocore.session.get_session().get_component(
                "data_loader"
            )
        return _shared_loader


class QuotaBrokerCredentialProvider:
    """Own one lazy, single-flight botocore credential cache per quota user.

    ``user_jwt`` is either the token or a zero-argument callable returning
    the token to present on the next vend. ``token_accepted`` /
    ``token_rejected`` are optional callbacks the factory uses to learn
    which token the broker last accepted (HTTP 2xx) or refused (HTTP 401).
    """

    def __init__(
        self,
        gateway_url: str,
        user_jwt: str | Callable[[], str],
        *,
        region: str = "us-east-1",
        aws_session: boto3.Session | None = None,
        http_client: httpx.Client | None = None,
        vend_callable: Callable[[str], dict | tuple[dict, dict]] | None = None,
        time_fetcher: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        refusal_cache_seconds: int = _MAX_REFUSAL_CACHE_SECONDS,
        token_accepted: Callable[[str], None] | None = None,
        token_rejected: Callable[[str], None] | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if refusal_cache_seconds < 0:
            raise ValueError("refusal_cache_seconds must not be negative")
        self.gateway_url = gateway_url.rstrip("/")
        self._user_token_supplier = (
            user_jwt if callable(user_jwt) else lambda: user_jwt
        )
        self.region = region
        self.aws_session = aws_session or boto3.Session(region_name=region)
        self.http_client = http_client
        self._owns_http_client = False
        self._vend_callable = vend_callable
        self._time = time_fetcher or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self._max_attempts = max_attempts
        self._refusal_cache_seconds = refusal_cache_seconds
        self._token_accepted = token_accepted
        self._token_rejected = token_rejected
        self._next_lease_id = str(uuid.uuid4())
        self._state_lock = threading.Lock()
        # Server clock minus local clock, estimated from the broker's Date
        # header. Zero until a response has been seen.
        self.clock_offset = timedelta(0)
        self._refusal: tuple[datetime, BrokerCredentialError] | None = None
        self._credentials_holder: dict[str, DeferredRefreshableCredentials] = {}
        credentials = DeferredRefreshableCredentials(
            refresh_using=self._refresh_metadata,
            method="bedrock-spend-controls-broker",
            time_fetcher=self._time,
        )
        self._credentials_holder["credentials"] = credentials
        self.credentials = credentials

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP client if this provider created it."""
        if self._owns_http_client and self.http_client is not None:
            self.http_client.close()
            self.http_client = None
            self._owns_http_client = False

    def _http(self) -> httpx.Client:
        if self.http_client is None:
            self.http_client = httpx.Client(timeout=BROKER_TIMEOUT)
            self._owns_http_client = True
        return self.http_client

    # -- clock --------------------------------------------------------------

    def _observe_server_clock(self, headers) -> None:
        server_now = _parse_http_date(headers.get("Date"))
        if server_now is None:
            return
        offset = server_now - self._time()
        if abs(offset) < timedelta(seconds=_SKEW_NOISE_SECONDS):
            offset = timedelta(0)
        self.clock_offset = offset

    def _to_local(self, server_time: datetime) -> datetime:
        return server_time - self.clock_offset

    # -- broker protocol ----------------------------------------------------

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

    def _current_token(self) -> str:
        user_token = self._user_token_supplier()
        if not isinstance(user_token, str) or not user_token:
            raise BrokerCredentialError(
                "user token supplier returned no JWT", terminal=True
            )
        return user_token

    def _request_once(self, lease_id: str) -> dict:
        user_token = self._current_token()
        if self._vend_callable is not None:
            # Test hook: a body, or (body, headers) to simulate the server's
            # Date header. Refusals are raised by the callable itself.
            result = self._vend_callable(lease_id)
            body, headers = result if isinstance(result, tuple) else (result, {})
            self._observe_server_clock(headers)
            self._check_vend_body(body)
            if self._token_accepted is not None:
                self._token_accepted(user_token)
            return body
        response = signed_request(
            "POST",
            self.gateway_url + "/v1/credentials",
            region=self.region,
            user_token=user_token,
            aws_session=self.aws_session,
            http_client=self._http(),
            timeout=BROKER_TIMEOUT,
            headers={"X-Quota-Lease-Id": lease_id},
            content=b"",
        )
        headers = getattr(response, "headers", None) or {}
        self._observe_server_clock(headers)
        try:
            body = response.json()
        except ValueError:
            body = {}
        status = response.status_code
        if status >= 400:
            error = self._broker_error(status, body, headers)
            if isinstance(error, AuthenticationError) and self._token_rejected:
                self._token_rejected(user_token)
            raise error
        self._check_vend_body(body)
        if self._token_accepted is not None:
            self._token_accepted(user_token)
        return body

    @staticmethod
    def _broker_error(status: int, body, headers) -> BrokerCredentialError:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        error = error if isinstance(error, dict) else {}
        error_type = str(error.get("type", "") or "")
        if not error_type:
            error_type = "broker_refused" if status < 500 else f"http_{status}"
        message = str(error.get("message", "") or f"broker returned HTTP {status}")
        if status >= 500:
            error_class = _TYPED_ERRORS.get(error_type, BrokerCredentialError)
            if error_class is not EmergencyStopError:
                error_class = BrokerCredentialError
            # Server trouble: retry inside the callback unless the broker
            # asked for a longer pause than one refresh should take.
            terminal = False
        else:
            fallback = AuthenticationError if status == 401 else BrokerCredentialError
            error_class = _TYPED_ERRORS.get(error_type, fallback)
            # Quota, block, auth, expired-ID, and premature-refresh responses
            # stop this callback immediately. Botocore can invoke a later
            # refresh; consuming the broker's whole per-minute budget in one
            # callback would turn small clock skew into self-throttling.
            terminal = True
        resets_at = _parse_optional_time(headers.get("X-Quota-Resets-At"))
        text = f"{error_type}: {message}"
        if resets_at is not None:
            text += f" (resets at {resets_at.isoformat()})"
        return error_class(
            text,
            terminal=terminal,
            error_type=error_type,
            status_code=status,
            retry_after=_parse_optional_int(headers.get("Retry-After")),
            refresh_after=_parse_optional_time(headers.get("X-Quota-Refresh-After")),
            resets_at=resets_at,
            breached_period=headers.get("X-Quota-Breached-Period", "") or "",
            breached_dimension=headers.get("X-Quota-Breached-Dimension", "") or "",
        )

    @staticmethod
    def _check_vend_body(body) -> None:
        if not isinstance(body, dict):
            raise BrokerCredentialError(
                "broker response must be a JSON object", terminal=True
            )
        missing = [
            field
            for field in _CREDENTIAL_FIELDS
            if not isinstance(body.get(field), str) or not body.get(field)
        ]
        if missing:
            raise BrokerCredentialError(
                "broker response is missing " + ", ".join(missing),
                terminal=True,
            )

    def _vend_with_retry(self, lease_id: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._request_once(lease_id)
            except (httpx.TransportError, BrokerCredentialError) as exc:
                last_error = exc
                retry_after = None
                if isinstance(exc, BrokerCredentialError):
                    if exc.terminal:
                        raise
                    retry_after = exc.retry_after
                    if (
                        retry_after is not None
                        and retry_after > _MAX_IN_CALL_RETRY_AFTER_SECONDS
                    ):
                        raise
                if attempt + 1 == self._max_attempts:
                    break
                self._sleep(max(0.25 * (2**attempt), retry_after or 0))
        assert last_error is not None
        if isinstance(last_error, BrokerCredentialError):
            raise last_error
        raise BrokerCredentialError(str(last_error), terminal=False)

    # -- refusal cache and retry hints ---------------------------------------

    def _raise_cached_refusal(self, now: datetime) -> None:
        if self._refusal is None:
            return
        deadline, error = self._refusal
        if now < deadline:
            raise error
        self._refusal = None

    def _remember_refusal(self, error: BrokerCredentialError, now: datetime) -> None:
        if not isinstance(error, _CACHED_REFUSALS):
            return
        ttl = float(self._refusal_cache_seconds)
        if error.retry_after is not None:
            ttl = min(ttl, float(error.retry_after))
        for server_time in (error.refresh_after, error.resets_at):
            if server_time is not None:
                remaining = (self._to_local(server_time) - now).total_seconds()
                ttl = min(ttl, remaining)
        if ttl > 0:
            self._refusal = (now + timedelta(seconds=ttl), error)

    def _apply_retry_hint(self, error: BrokerCredentialError, now: datetime) -> None:
        """Delay the next advisory refresh until the broker says it may succeed.

        A premature refresh (clock skew, or another process refreshing the
        shared lease) must not make every following Bedrock call try again.
        """
        if not isinstance(error, VendRateLimitedError):
            return
        credentials = self._credentials_holder["credentials"]
        expiry = getattr(credentials, "_expiry_time", None)
        if expiry is None:
            return
        retry_at = None
        if error.refresh_after is not None:
            retry_at = self._to_local(error.refresh_after)
        if error.retry_after is not None:
            candidate = now + timedelta(seconds=error.retry_after)
            retry_at = candidate if retry_at is None else max(retry_at, candidate)
        if retry_at is None:
            return
        remaining_at_retry = int((expiry - retry_at).total_seconds())
        mandatory = credentials._mandatory_refresh_timeout
        credentials._advisory_refresh_timeout = max(mandatory, remaining_at_retry)

    # -- refresh callback ----------------------------------------------------

    def _refresh_metadata(self) -> dict[str, str]:
        # Botocore serializes this callback with its refresh lock. The local
        # lock protects lease-ID rotation for direct tests and future callers.
        with self._state_lock:
            now = self._time()
            self._raise_cached_refusal(now)
            lease_id = self._next_lease_id
            try:
                try:
                    body = self._vend_with_retry(lease_id)
                except BrokerCredentialError as exc:
                    if exc.error_type != "lease_expired":
                        raise
                    # The broker requires a new ID after the fixed deadline.
                    # Rotate once and retry; keep all transport retries for
                    # either logical lease on their original ID.
                    lease_id = str(uuid.uuid4())
                    self._next_lease_id = lease_id
                    body = self._vend_with_retry(lease_id)
            except BrokerCredentialError as exc:
                self._remember_refusal(exc, now)
                self._apply_retry_hint(exc, now)
                raise
            expiration = self._to_local(
                self._parse_time(body.get("expiration"), "expiration")
            )
            refresh_raw = body.get("refresh_after")
            refresh_after = (
                self._to_local(self._parse_time(refresh_raw, "refresh_after"))
                if refresh_raw
                else expiration - timedelta(seconds=60)
            )
            now = self._time()
            if expiration <= now:
                raise BrokerCredentialError(
                    "broker returned credentials that are already expired",
                    terminal=True,
                )
            joined = body.get("lease_id") not in (None, lease_id)
            if joined and refresh_after <= now:
                # Another process holds this lease and the broker says its
                # refresh window is open "now" by our clock: that is residual
                # skew, so do not hammer the broker on every call.
                refresh_after = now + timedelta(seconds=_JOIN_MIN_BACKOFF_SECONDS)
            refresh_after = min(expiration, max(now, refresh_after))
            advisory_seconds = max(
                0, int((expiration - refresh_after).total_seconds())
            )
            credentials = self._credentials_holder["credentials"]
            credentials._advisory_refresh_timeout = advisory_seconds
            credentials._mandatory_refresh_timeout = min(
                5, advisory_seconds
            )
            self._refusal = None
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
        botocore_session = botocore.session.get_session()
        botocore_session.register_component("data_loader", _shared_data_loader())
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
    __slots__ = ("provider", "client", "jwt", "pending_jwt", "last_used")

    def __init__(self, provider: QuotaBrokerCredentialProvider, client, jwt: str, now: float):
        self.provider = provider
        self.client = client
        # ``jwt`` is the newest token the broker has accepted for this
        # identity (or the one that created the entry, until the first vend).
        # ``pending_jwt`` is a newer token not yet presented to the broker; a
        # rejected pending token is dropped instead of replacing ``jwt``.
        self.jwt = jwt
        self.pending_jwt: str | None = None
        self.last_used = now

    def current_jwt(self) -> str:
        return self.pending_jwt or self.jwt

    def offer(self, jwt: str) -> None:
        if jwt != self.jwt:
            self.pending_jwt = jwt

    def accepted(self, jwt: str) -> None:
        self.jwt = jwt
        if self.pending_jwt == jwt:
            self.pending_jwt = None

    def rejected(self, jwt: str) -> None:
        if self.pending_jwt == jwt:
            self.pending_jwt = None


class BedrockSpendControls:
    """Hand out a per-user ``bedrock-runtime`` client from a shared backend.

    Create **one** instance when the process starts and call
    ``client_for(user_jwt)`` on every request. The factory keeps one
    ``QuotaBrokerCredentialProvider`` (one broker lease) per quota identity,
    reuses the boto3 client built on top of it, renews the lease with the
    newest JWT the broker has accepted for that identity, and forgets
    identities that have been idle for ``idle_ttl_seconds`` or that overflow
    ``max_users`` (least recently used first).

    **Only pass verified tokens.** The cache key is read from the JWT
    without checking its signature; the broker re-verifies on every vend,
    but ``client_for`` returns the cached client for that identity before
    any broker call. Verify the token in your authentication middleware
    first, or pass the identity you verified via ``verified_identity=`` /
    ``client_for_identity`` so a mismatch is rejected here.

    ``identity_claim`` must match the deployment's ``jwt_user_claim`` (``sub``
    by default; a tenant/team claim for shared quotas) so that every JWT the
    broker maps to the same quota identity also maps to the same cached
    lease. Keying by a finer claim would still work but would spend one vend
    per user per refresh against a shared per-identity vend rate limit.

    Memory: each cached identity costs about 0.25 MiB (one botocore client
    over a shared model loader) plus one credential set, so the default
    ``max_users`` of 1,000 bounds the cache near 250 MiB. Raise it only
    with that budget in mind. One signing ``aws_session`` and one
    ``httpx.Client`` are shared by every identity.

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
        http_client: httpx.Client | None = None,
        idle_ttl_seconds: int = 3600,
        max_users: int = 1_000,
        client_kwargs: dict | None = None,
        provider_kwargs: dict | None = None,
        time_fetcher: Callable[[], float] | None = None,
    ):
        if idle_ttl_seconds <= 0 or max_users <= 0:
            raise ValueError("idle_ttl_seconds and max_users must be positive")
        self.gateway_url = gateway_url.rstrip("/")
        self.region = region
        self.identity_claim = identity_claim
        self.aws_session = aws_session or boto3.Session(region_name=region)
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.Client(timeout=BROKER_TIMEOUT)
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

    def client_for(
        self,
        user_jwt: str | None = None,
        *,
        verified_identity: str | None = None,
    ):
        """Return a ``bedrock-runtime`` client authenticated as this user.

        **``user_jwt`` must be a token your application has already
        verified.** The cache key (``identity_claim``) is read from it
        without signature verification; an unverified token naming another
        user would be handed that user's cached client. The broker verifies
        the token again when a lease is vended or renewed, and a token it
        rejects never replaces the identity's stored renewal token.

        ``verified_identity`` is the identity your middleware established
        (the same claim value); when given, a token whose claim differs is
        rejected with ``ValueError`` instead of being trusted.

        The first call for an identity creates its provider; credentials are
        vended lazily on the first signed Bedrock request and renewed before
        the permission lease ends. Later calls with the same identity return
        the same client; a newer JWT is offered for the next renewal and
        becomes the stored token once the broker accepts it.
        """
        return self._entry_for(user_jwt, verified_identity).client

    def client_for_identity(self, identity: str, user_jwt: str):
        """``client_for`` for callers that verified the token themselves.

        ``identity`` is the value of ``identity_claim`` your application
        validated; ``user_jwt`` is the matching token the broker will see.
        """
        if not identity:
            raise ValueError("identity must not be empty")
        return self._entry_for(user_jwt, identity).client

    def provider_for(
        self,
        user_jwt: str | None = None,
        *,
        verified_identity: str | None = None,
    ) -> QuotaBrokerCredentialProvider:
        """The underlying provider, for callers that build their own clients."""
        return self._entry_for(user_jwt, verified_identity).provider

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

    def close(self) -> None:
        """Drop every cached identity and close the HTTP client we created."""
        with self._lock:
            self._entries.clear()
        if self._owns_http_client:
            self.http_client.close()

    # -- internals ----------------------------------------------------------

    def _entry_for(self, user_jwt: str | None, verified_identity: str | None) -> _UserEntry:
        if user_jwt is None:
            user_jwt = _current_user_jwt.get()
        if not user_jwt:
            raise ValueError(
                "no user JWT: pass client_for(user_jwt) or call "
                "set_current_user(jwt) in your authentication middleware"
            )
        key = jwt_claim(user_jwt, self.identity_claim)
        if verified_identity is not None and verified_identity != key:
            raise ValueError(
                f"token claim {self.identity_claim!r} is {key!r} but the "
                f"verified identity is {verified_identity!r}"
            )
        now = self._time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.offer(user_jwt)
                entry.last_used = now
                return entry
        # Build outside the lock: a new identity costs a botocore client
        # (~30 ms) and must not stall every other caller.
        candidate = self._create_entry(key, user_jwt, now)
        with self._lock:
            entry = self._entries.setdefault(key, candidate)
            if entry is candidate:
                self._evict_locked(now, keep=key)
            else:
                entry.offer(user_jwt)
                entry.last_used = now
            return entry

    def _create_entry(self, key: str, user_jwt: str, now: float) -> _UserEntry:
        holder: dict[str, _UserEntry] = {}

        def latest_jwt() -> str:
            entry = holder.get("entry")
            return entry.current_jwt() if entry is not None else user_jwt

        def accepted(token: str) -> None:
            entry = holder.get("entry")
            if entry is not None:
                entry.accepted(token)

        def rejected(token: str) -> None:
            entry = holder.get("entry")
            if entry is not None:
                entry.rejected(token)

        provider_kwargs = {
            "region": self.region,
            "aws_session": self.aws_session,
            "http_client": self.http_client,
            **self._provider_kwargs,
        }
        provider = QuotaBrokerCredentialProvider(
            self.gateway_url,
            latest_jwt,
            token_accepted=accepted,
            token_rejected=rejected,
            **provider_kwargs,
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
