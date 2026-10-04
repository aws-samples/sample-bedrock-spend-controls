"""Control plane for Bedrock Spend Controls: runtime-only Bedrock quotas.

The application is deliberately not an inference proxy. It authenticates an
OIDC identity, checks the latest event-driven usage aggregate, and vends a
short-lived STS session that calls ``bedrock-runtime`` directly. The same API
provides administrative quota management.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from bedrock_spend_controls.quota_periods import (
    PERIODS,
    RATE_DIMENSIONS,
    calendar_window,
    normalize_thresholds,
    period_for_start,
    quota_reason,
    thresholds_public,
    validate_model_id,
)

from . import emf
from .auth import (
    AdminPrincipal,
    Identity,
    JwtError,
    JwtVerifier,
    extract_bearer,
    extract_user_token,
)
from .broker import BrokerError, CredentialBroker
from .config import settings
from .quota import (
    MICRO,
    VALID_PERMISSION_LEASE_SECONDS,
    WORKLOAD_USER_ID_PREFIX,
    EmergencyVersionConflict,
    EnforcementVersionConflict,
    IdempotencyConflict,
    LeaseExpired,
    LeaseNotRefreshable,
    LeaseRateLimited,
    LeaseReservation,
    QuotaStore,
    UserAlreadyExists,
    UserRecord,
    VersionConflict,
    configured_default_limits,
    validate_user_id,
)

app = FastAPI(
    title="Amazon Bedrock Runtime quota broker",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_store: QuotaStore | None = None
_verifier: JwtVerifier | None = None
_broker: CredentialBroker | None = None
_admin_key: str | None = None
_emergency_key: str | None = None
_cloudwatch = None
_ssm = None
# (roster dict, monotonic fetch time, source label). See _workload_registry.
_workload_roster_cache: tuple[dict, float, str] | None = None

logger = logging.getLogger("bedrock_spend_controls.gateway")


def store() -> QuotaStore:
    global _store
    if _store is None:
        _store = QuotaStore()
    return _store


def ssm_client():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=settings.aws_region)
    return _ssm


def verifier() -> JwtVerifier:
    global _verifier
    if _verifier is None:
        _verifier = JwtVerifier()
    return _verifier


def broker() -> CredentialBroker:
    global _broker
    if _broker is None:
        _broker = CredentialBroker()
    return _broker


def cloudwatch_client():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client(
            "cloudwatch", region_name=settings.aws_region
        )
    return _cloudwatch


def admin_key() -> str:
    global _admin_key
    if _admin_key is None:
        import os

        secret_arn = os.environ.get("ADMIN_KEY_SECRET_ARN")
        if secret_arn:
            secrets = boto3.client(
                "secretsmanager", region_name=settings.aws_region
            )
            _admin_key = secrets.get_secret_value(
                SecretId=secret_arn
            )["SecretString"]
        else:
            _admin_key = os.environ.get("ADMIN_API_KEY", "")
    return _admin_key


def emergency_key() -> str:
    global _emergency_key
    if _emergency_key is None:
        import os

        secret_arn = os.environ.get("EMERGENCY_KEY_SECRET_ARN")
        if secret_arn:
            secrets = boto3.client(
                "secretsmanager", region_name=settings.aws_region
            )
            _emergency_key = secrets.get_secret_value(
                SecretId=secret_arn
            )["SecretString"]
        else:
            _emergency_key = os.environ.get("EMERGENCY_ADMIN_KEY", "")
    return _emergency_key


def _error(
    status: int,
    message: str,
    error_type: str,
    headers: dict | None = None,
    details: dict | None = None,
) -> JSONResponse:
    error = {
        "message": message,
        "type": error_type,
        # `code` mirrors `type` for clients that prefer that name.
        "code": error_type,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status,
        content={"error": error},
        headers=headers or {},
    )


def _authenticate(
    request: Request,
) -> tuple[UserRecord | None, Identity | None, str]:
    token = extract_user_token(request.headers)
    if not token:
        return None, None, "Missing bearer token."
    try:
        identity: Identity = verifier().verify(token)
    except JwtError as exc:
        return None, None, exc.reason
    try:
        validate_user_id(identity.user_id)
    except ValueError as exc:
        return None, None, str(exc)
    if identity.user_id.startswith(WORKLOAD_USER_ID_PREFIX):
        # Workload budgets are attributed by inference profile and enforced
        # on the workload's own IAM principal; a JWT claiming the namespace
        # must never vend credentials against a workload's budget.
        return (
            None,
            None,
            "The 'workload:' namespace is reserved for workload-mode "
            "subjects and cannot authenticate through the vend path.",
        )

    user = store().get_user(identity.user_id)
    if user is None:
        if not settings.auto_provision_users:
            return (
                None,
                None,
                f"User '{identity.user_id}' is not provisioned on this broker.",
            )
        display_name = str(
            identity.claims.get("email")
            or identity.claims.get("username")
            or identity.claims.get("cognito:username")
            or identity.user_id
        )
        user = store().get_or_provision_user(
            identity.user_id, name=display_name
        )
    return user, identity, ""


def _quota_headers(user: UserRecord, evaluation=None) -> dict[str, str]:
    # Clients discover enabled periods via X-Quota-Enabled-Periods and, on
    # 429 responses, the binding period/dimension and its reset time via the
    # X-Quota-Breached-* and X-Quota-Resets-At headers below.
    headers = {
        "X-Quota-Window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "X-Quota-Enabled-Periods": ",".join(
            period
            for period, limits in user.period_limits.items()
            if limits is not None
        ),
    }
    if evaluation is not None and evaluation.breaches:
        first = evaluation.breaches[0]
        headers["X-Quota-Breached-Period"] = first.period
        headers["X-Quota-Breached-Dimension"] = first.dimension
        headers["X-Quota-Resets-At"] = first.window.end.isoformat()
    return headers


def _blocked_response(user: UserRecord) -> JSONResponse:
    """403 ``quota_blocked`` for a subject whose status is not active.

    An automatic block carries the same ``X-Quota-Breached-*`` and
    ``X-Quota-Resets-At`` headers as the 429 that created it, so a client
    refused on a later vend can still tell when the window reopens. An
    admin block has no window to report, so those headers are omitted.
    """
    evaluation = None
    if user.status_origin == "automatic":
        evaluation = store().evaluate_user_quota(user)
    return _error(
        403,
        f"User '{user.user_id}' is {user.status}.",
        "quota_blocked",
        headers=_quota_headers(user, evaluation),
    )


def _lease_retry_headers(retry_after: datetime) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    seconds = max(0, math.ceil((retry_after - now).total_seconds()))
    return {
        "Retry-After": str(seconds),
        "X-Quota-Refresh-After": retry_after.isoformat(),
    }


def _reserve_permission_lease(
    request: Request,
    user: UserRecord,
    identity: Identity,
    lease_seconds: int,
) -> tuple[LeaseReservation | None, JSONResponse | None]:
    requested_id = request.headers.get("x-quota-lease-id") or str(
        uuid.uuid4()
    )
    raw_expiration = identity.claims.get("exp")
    if (
        isinstance(raw_expiration, bool)
        or not isinstance(raw_expiration, (int, float))
    ):
        return None, _error(
            401,
            "Token has no valid expiration claim.",
            "authentication_error",
        )
    jwt_expiration = datetime.fromtimestamp(
        raw_expiration, tz=timezone.utc
    )
    try:
        reservation = store().reserve_lease(
            user.user_id,
            requested_id,
            expires_no_later_than=jwt_expiration,
            lease_seconds=lease_seconds,
        )
    except LeaseRateLimited as exc:
        emf.record_throttle(user.user_id, "vend-rate-limit")
        return None, _error(
            429,
            "Credential vend rate limit exceeded.",
            "lease_rate_limited",
            headers=_lease_retry_headers(exc.retry_after),
        )
    except LeaseNotRefreshable as exc:
        return None, _error(
            429,
            "The current permission lease is not ready for refresh.",
            "lease_not_refreshable",
            headers=_lease_retry_headers(exc.retry_after),
        )
    except LeaseExpired:
        return None, _error(
            409,
            "The supplied lease ID has expired; use a new lease ID.",
            "lease_expired",
        )
    except ValueError as exc:
        return None, _error(400, str(exc), "invalid_lease")
    return reservation, None


@app.post("/v1/credentials")
async def vend_credentials(request: Request) -> Response:
    user, identity, auth_error = _authenticate(request)
    if user is None or identity is None:
        return _error(401, auth_error, "authentication_error")
    if store().emergency_stop_active():
        return _error(
            503,
            "Credential vending is disabled by the emergency stop.",
            "emergency_stop",
            headers={"Retry-After": "60"},
        )

    user = store().refresh_auto_status(user)
    if not user.active:
        return _blocked_response(user)
    evaluation = None
    for _ in range(3):
        evaluation = store().evaluate_user_quota(user)
        if not user.active or not evaluation.over_budget:
            break
        changed = store().set_user_status(
            user.user_id,
            "blocked",
            quota_reason(evaluation),
            origin="automatic",
            expected_version=user.version,
            expected_status=user.status,
            expected_reason=user.status_reason,
        )
        latest = store().get_user(user.user_id)
        if latest is None:
            return _error(401, "User is no longer provisioned.", "authentication_error")
        user = latest
        if changed:
            emf.record_throttle(user.user_id, "over-budget-at-vend")
            return _error(
                429,
                "A quota limit is exhausted; credentials were not issued.",
                "quota_exceeded",
                headers=_quota_headers(user, evaluation),
            )
    if not user.active:
        return _blocked_response(user)
    evaluation = store().evaluate_user_quota(user)
    if evaluation.over_budget:
        return _error(
            503,
            "Quota state changed concurrently; retry credential vending.",
            "quota_state_conflict",
            headers={"Retry-After": "1", **_quota_headers(user, evaluation)},
        )

    reservation, lease_error = _reserve_permission_lease(
        request, user, identity, store().effective_permission_lease_seconds()
    )
    if lease_error is not None:
        return lease_error
    if reservation is not None:
        # Re-check strongly consistent gates after the conditional lease write
        # and immediately before STS. This narrows block/emergency races; any
        # final IAM authorization race remains bounded by the fixed deadline.
        if store().emergency_stop_active():
            return _error(
                503,
                "Credential vending is disabled by the emergency stop.",
                "emergency_stop",
                headers={"Retry-After": "60"},
            )
        latest_user = store().get_user(user.user_id)
        if latest_user is None:
            return _error(
                403,
                f"User '{user.user_id}' is blocked.",
                "quota_blocked",
            )
        if not latest_user.active:
            return _blocked_response(latest_user)
        evaluation = store().evaluate_user_quota(latest_user)
        if evaluation.over_budget:
            return _error(
                429,
                "A quota limit is exhausted; credentials were not issued.",
                "quota_exceeded",
                headers=_quota_headers(latest_user, evaluation),
            )

    try:
        credentials = broker().vend(
            identity,
            permission_deadline=(
                reservation.expires_at if reservation else None
            ),
        )
    except BrokerError as exc:
        # Keep a newly reserved logical lease after STS failure. A same-ID
        # retry retains its fixed deadline; deleting it here can race with a
        # concurrent successful retry and allow an early extending lease.
        return _error(exc.status, exc.reason, "broker_error")

    store().record_session(credentials.session_name, user.user_id)
    emf.record_credentials_vended(user.user_id)
    if reservation is not None:
        if not reservation.created:
            # A join re-issues the current lease to another process; like a
            # same-ID retry it never extends the deadline, so both share the
            # LeaseRetried metric (no extra per-user metric stream).
            lease_event = "LeaseRetried"
        elif reservation.generation == 1:
            lease_event = "LeaseStarted"
        else:
            lease_event = "LeaseRefreshed"
        emf.record_lease_event(
            user.user_id,
            lease_event,
            reservation.generation,
            joined=reservation.joined,
        )
    payload = {
        "aws_access_key_id": credentials.access_key_id,
        "aws_secret_access_key": credentials.secret_access_key,
        "aws_session_token": credentials.session_token,
        # `expiration` is a no-later-than Bedrock permission deadline.
        # Targeted or emergency denies can terminate access earlier; the
        # underlying STS keys may live longer.
        "expiration": credentials.expiration,
        "sts_expiration": (
            credentials.sts_expiration or credentials.expiration
        ),
        "region": settings.aws_region,
        "user_id": credentials.user_id,
        "endpoint": (
            f"https://bedrock-runtime.{settings.aws_region}.amazonaws.com"
        ),
    }
    if reservation is not None:
        payload.update(
            {
                "lease_id": reservation.lease_id,
                "refresh_after": reservation.refresh_after.isoformat(),
                "lease_generation": reservation.generation,
            }
        )
    return JSONResponse(payload, headers=_quota_headers(user))


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "inference_endpoint": "bedrock-runtime",
        "metering": "cloudwatch-logs-subscription",
    }


def _jwt_admin_principal(token: str) -> AdminPrincipal | None:
    """Verify one admin JWT once and derive a safe server-side actor."""
    claim = settings.admin_jwt_claim
    if not claim:
        return None
    try:
        # Admin scope: the console's client id (second JWT_AUDIENCE entry)
        # is honoured here and nowhere else.
        identity = verifier().verify(token, scope="admin")
    except JwtError:
        return None
    value = identity.claims.get(claim)
    required = settings.admin_jwt_value
    granted = value == required if isinstance(value, str) else (
        required in value if isinstance(value, (list, tuple)) else False
    )
    if not granted:
        return None
    subject = identity.claims.get("sub")
    actor = (
        subject
        if isinstance(subject, str) and subject.strip()
        else identity.user_id
    )
    return AdminPrincipal(actor=actor, auth_method="jwt")


def _shared_key_matches(provided: str | None, expected: str | None) -> bool:
    """Constant-time shared-key comparison for any header text.

    ``secrets.compare_digest`` raises TypeError (a 500) for ``str`` values
    with non-ASCII characters, which a header can carry. Compare the UTF-8
    bytes instead: still constant-time, and a stray character is a plain 403.
    """
    if not provided or not expected:
        return False
    return secrets.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


def _require_admin(request: Request) -> JSONResponse | None:
    provided = extract_bearer(request.headers.get("x-quota-admin-key"))
    if not provided:
        authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            provided = extract_bearer(authorization)
    principal = None
    if _shared_key_matches(provided, admin_key()):
        principal = AdminPrincipal(
            actor="admin-shared-key", auth_method="shared-key"
        )
    if principal is None:
        token = extract_user_token(request.headers)
        if token:
            principal = _jwt_admin_principal(token)
    if principal is None:
        return _error(403, "Admin authorization required.", "forbidden")
    request.state.admin_principal = principal
    return None


def _require_emergency_admin(
    request: Request,
) -> tuple[JSONResponse | None, str]:
    provided = extract_bearer(request.headers.get("x-quota-emergency-key"))
    # Constant-time compare, same as the routine admin key: the break-glass
    # key must not leak a prefix match through response timing.
    if _shared_key_matches(provided, emergency_key()):
        actor = "emergency-shared-key"
        # The shared key is what authorizes the action; it says nothing about
        # who held it. When the caller also presents a verified admin JWT
        # (the console sends its login alongside the typed key), name that
        # principal in the audit trail. A missing or non-admin token changes
        # nothing: the key alone still suffices and is recorded as such.
        token = extract_user_token(request.headers)
        principal = _jwt_admin_principal(token) if token else None
        if principal is not None:
            actor = f"{principal.actor} ({actor})"
        return None, actor
    return (
        _error(
            403,
            "Break-glass emergency authorization required.",
            "forbidden",
        ),
        "",
    )


async def _admin_json_object(
    request: Request,
) -> tuple[dict | None, JSONResponse | None]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None, _error(
            400,
            "Request body must be a JSON object.",
            "invalid_request_error",
        )
    if not isinstance(body, dict):
        return None, _error(
            400,
            "Request body must be a JSON object.",
            "invalid_request_error",
        )
    return body, None


def _limits_json(user: UserRecord) -> dict:
    limits: dict[str, dict | None] = {}
    for period, value in user.period_limits.items():
        limits[period] = (
            {
                "usd": int(value.get("usd_micro", 0)) / MICRO,
                "input_tokens": int(value.get("input_tokens", 0)),
                "output_tokens": int(value.get("output_tokens", 0)),
                # Ordered warn/block list in API form ({at: ratio, action}).
                # Rows without their own list report the deployment default.
                "thresholds": thresholds_public(value.get("thresholds") or []),
            }
            if value is not None
            else None
        )
    return limits


def _rate_json(user: UserRecord) -> dict | None:
    """Subject-level per-minute limits; null when neither is configured."""
    return dict(user.rate_limits) if user.rate_limited else None


def _period_limits_public(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {
        "usd": int(value.get("usd_micro", 0)) / MICRO,
        "input_tokens": int(value.get("input_tokens", 0)),
        "output_tokens": int(value.get("output_tokens", 0)),
        "thresholds": thresholds_public(value.get("thresholds") or []),
    }


def _model_budgets_json(user: UserRecord) -> dict[str, dict]:
    """``{model_id: {daily|weekly|monthly: limits | null}}`` (API form)."""
    return {
        model_id: {
            period: _period_limits_public(periods.get(period))
            for period in PERIODS
        }
        for model_id, periods in sorted(user.model_budget_limits.items())
    }


def _allowed_model_ids() -> set[str] | None:
    """Model/profile IDs the vended role may call; None means ``*``.

    Derived from ``allowed_model_arns`` by taking the trailing resource ID
    (``foundation-model/<id>`` or ``inference-profile/<id>``). A budget for
    a model outside this set could never accrue usage, so it is rejected.
    """
    try:
        arns = json.loads(settings.allowed_model_arns_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(arns, list) or "*" in arns:
        return None
    ids: set[str] = set()
    for arn in arns:
        if not isinstance(arn, str):
            continue
        tail = arn.rsplit("/", 1)[-1]
        if tail and tail != "*":
            ids.add(tail)
        if tail.endswith("*") and len(tail) > 1:
            # Prefix wildcard (e.g. anthropic.*): keep the prefix for a
            # startswith check below.
            ids.add(tail)
    return ids


def _model_allowed(model_id: str) -> bool:
    allowed = _allowed_model_ids()
    if allowed is None:
        return True
    if model_id in allowed:
        return True
    return any(
        entry.endswith("*") and model_id.startswith(entry[:-1])
        for entry in allowed
    )


def _period_usage_json(value: dict[str, object]) -> dict:
    return {
        "period": str(value["period"]),
        "window": str(value["window"]),
        "window_start": str(value["window_start"]),
        "window_end": str(value["window_end"]),
        "resets_at": str(value["resets_at"]),
        "cost_usd": int(value.get("cost_micro", 0)) / MICRO,
        "input_tokens": int(value.get("input_tokens", 0)),
        "output_tokens": int(value.get("output_tokens", 0)),
        "requests": int(value.get("requests", 0)),
        # Non-limited priced dimensions.
        "cache_read_tokens": int(value.get("cache_read_tokens", 0)),
        "cache_write_tokens": int(value.get("cache_write_tokens", 0)),
        "images": int(value.get("images", 0)),
        # Requests whose USD is known to be incomplete (a dimension had no
        # catalog rate). Non-zero means "repair candidate", never silent.
        "unpriced_requests": int(value.get("unpriced_requests", 0)),
    }


def _current_usage_json(user_id: str) -> dict[str, dict]:
    return {
        period: _period_usage_json(value)
        for period, value in store().get_current_usage(user_id).items()
    }


def _lease_json(user: UserRecord) -> dict | None:
    """Lease timing for the admin UI. The lease_id itself is the renewal
    token and is deliberately never exposed on admin read paths."""
    if (
        user.lease_expires_at_epoch is None
        or user.lease_generation is None
        or user.lease_duration_seconds is None
    ):
        return None
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(user.lease_expires_at_epoch, tz=timezone.utc)
    refresh_after = (
        datetime.fromtimestamp(user.lease_refresh_after_epoch, tz=timezone.utc)
        if user.lease_refresh_after_epoch is not None
        else None
    )
    lease_seconds = user.lease_duration_seconds
    return {
        "active": now < expires_at,
        "expires_at": expires_at.isoformat(),
        "refresh_after": refresh_after.isoformat() if refresh_after else None,
        "generation": user.lease_generation,
        "granted_at": datetime.fromtimestamp(
            user.lease_expires_at_epoch - lease_seconds, tz=timezone.utc
        ).isoformat(),
        "lease_seconds": lease_seconds,
    }


def _workload_registry() -> dict:
    """Workload roster ``{workload_id: {name, model, profile_arn, role_arn,
    enforcement_ready}}``.

    Read from the Parameter Store parameter the stack writes (cached for
    ``workload_roster_cache_seconds``); the inline ``WORKLOAD_ROSTER_JSON``
    is the local/dev fallback and the last-known-good value if Parameter
    Store is unreadable. The roster is static deploy configuration, so a
    stale cache can only lag a redeploy by the cache window.
    """
    global _workload_roster_cache
    fallback = _json_object(settings.workload_roster_json)
    parameter_name = settings.workload_roster_parameter_name
    if not parameter_name:
        return fallback
    now = time.monotonic()
    cached = _workload_roster_cache
    if cached is not None and (
        now - cached[1] < settings.workload_roster_cache_seconds
    ):
        return cached[0]
    try:
        response = ssm_client().get_parameter(Name=parameter_name)
        roster = _json_object(response["Parameter"]["Value"])
        _workload_roster_cache = (roster, now, "parameter_store")
        return roster
    except (BotoCoreError, ClientError, KeyError) as exc:
        logger.warning(
            "workload roster parameter %s unreadable (%s); using %s",
            parameter_name,
            type(exc).__name__,
            "cached roster" if cached is not None else "inline fallback",
        )
        if cached is not None:
            # Keep serving the stale copy rather than flapping to the
            # fallback; refresh attempts continue every call until it works.
            return cached[0]
        return fallback


def _workload_roster_source() -> str:
    if not settings.workload_roster_parameter_name:
        return "environment"
    cached = _workload_roster_cache
    return cached[2] if cached is not None else "fallback"


def _is_workload_id(user_id: str) -> bool:
    return user_id.startswith(WORKLOAD_USER_ID_PREFIX)


def _workload_json(workload_id: str, entry: dict | None) -> dict:
    """Identity block for a ``workload:`` subject.

    ``registered`` is False for rows whose id is not in the deployed roster
    (a workload removed from the config, or a row created out of band):
    they keep metering history but nothing enforces them.
    """
    registered = isinstance(entry, dict)
    entry = entry if registered else {}
    name = str(entry.get("name") or workload_id[len(WORKLOAD_USER_ID_PREFIX):])
    role_arn = entry.get("role_arn") or None
    return {
        "workload_id": workload_id,
        "name": name,
        "model": entry.get("model") or None,
        "profile_arn": entry.get("profile_arn") or None,
        "role_arn": role_arn,
        "enforcement_ready": bool(entry.get("enforcement_ready")),
        "registered": registered,
        "tag": {"key": settings.workload_tag_key, "value": name},
    }


def _user_json(user: UserRecord) -> dict:
    payload = {
        "user_id": user.user_id,
        "name": user.name,
        "status": user.status,
        "status_reason": user.status_reason,
        "status_origin": user.status_origin,
        "version": user.version,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "limits": _limits_json(user),
        "rate": _rate_json(user),
        # Optional second axis; empty object when none are configured. A
        # breach on any model budget blocks the whole subject.
        "model_budgets": _model_budgets_json(user),
        "lease": _lease_json(user),
    }
    if _is_workload_id(user.user_id):
        workload = _workload_json(
            user.user_id, _workload_registry().get(user.user_id)
        )
        payload["granularity"] = "workload"
        payload["enforcement_ready"] = workload["enforcement_ready"]
        payload["workload"] = workload
    else:
        payload["granularity"] = "user"
    return payload


def _etag(user: UserRecord) -> str:
    return f'"{user.version}"'


def _idempotency_key(request: Request) -> tuple[str | None, JSONResponse | None]:
    key = request.headers.get("idempotency-key")
    if key is None:
        return str(uuid.uuid4()), None
    key = key.strip()
    if not key or len(key) > 256:
        return None, _error(
            400,
            "Idempotency-Key must contain 1 to 256 characters.",
            "invalid_request_error",
        )
    return key, None


def _request_hash(
    request: Request, body: dict, principal: AdminPrincipal
) -> str:
    request_shape = {
        "method": request.method,
        "path": request.url.path,
        "body": body,
        "if_match": request.headers.get("if-match"),
        "actor": principal.actor,
        "auth_method": principal.auth_method,
    }
    query = sorted(request.query_params.multi_items())
    if query:
        request_shape["query"] = query
    canonical = json.dumps(
        request_shape,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_version(
    request: Request, observed: UserRecord
) -> tuple[int | None, JSONResponse | None]:
    raw = request.headers.get("if-match")
    if raw is None:
        return observed.version, None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        version = int(value)
    except (TypeError, ValueError):
        version = -1
    if version < 0:
        return None, _error(
            400,
            "If-Match must contain a non-negative user version.",
            "invalid_request_error",
        )
    return version, None


def _mutation_response(
    payload: dict, user: UserRecord, request_id: str
) -> JSONResponse:
    return JSONResponse(
        {**payload, "user": _user_json(user)},
        headers={"ETag": _etag(user), "X-Request-Id": request_id},
    )


def _version_conflict(error: VersionConflict) -> JSONResponse:
    current = error.current_user
    return _error(
        409,
        "The user configuration changed; refresh and retry.",
        "version_conflict",
        headers={"ETag": _etag(current)},
        details={"current_user": _user_json(current)},
    )


def _transaction_unavailable() -> JSONResponse:
    return _error(
        503,
        "The admin mutation could not be committed; retry the request.",
        "transaction_unavailable",
        headers={"Retry-After": "1"},
    )


def _parse_rate(body: dict) -> tuple[dict | None, bool, str]:
    """Return (rate, present, error). ``rate`` is ``{rpm, tpm}`` or None
    (disable both); ``present`` is False when the body did not mention it."""
    if "rate" not in body:
        return None, False, ""
    raw = body["rate"]
    if raw is None:
        return None, True, ""
    if not isinstance(raw, dict):
        return None, True, "rate must be an object or null."
    unknown = sorted(set(raw) - set(RATE_DIMENSIONS))
    if unknown:
        return None, True, "Unknown rate fields: " + ", ".join(unknown)
    rate: dict[str, int] = {}
    for dimension in RATE_DIMENSIONS:
        value = raw.get(dimension, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None, True, f"rate.{dimension} must be a non-negative integer."
        rate[dimension] = value
    return rate, True, ""


def _parse_limits(
    body: dict, *, with_defaults: bool
) -> tuple[dict, str]:
    if "limits" not in body:
        if with_defaults:
            return configured_default_limits(), ""
        # A rate-only update is a valid limits mutation.
        return ({}, "") if "rate" in body else ({}, "limits is required.")
    raw_limits = body["limits"]
    if not isinstance(raw_limits, dict):
        return {}, "limits must be an object."
    unknown_periods = sorted(set(raw_limits) - set(PERIODS))
    if unknown_periods:
        return {}, "Unknown quota periods: " + ", ".join(unknown_periods)
    values: dict[str, dict | None] = (
        configured_default_limits() if with_defaults else {}
    )
    values.pop("rate", None)
    expected_fields = {"usd", "input_tokens", "output_tokens"}
    optional_fields = {"thresholds"}
    for period in PERIODS:
        if period not in raw_limits:
            continue
        raw_period = raw_limits[period]
        if raw_period is None:
            values[period] = None
            continue
        if not isinstance(raw_period, dict):
            return {}, f"limits.{period} must be an object or null."
        unknown_fields = sorted(set(raw_period) - expected_fields - optional_fields)
        missing_fields = sorted(expected_fields - set(raw_period))
        if unknown_fields or missing_fields:
            details = []
            if missing_fields:
                details.append("missing " + ", ".join(missing_fields))
            if unknown_fields:
                details.append("unknown " + ", ".join(unknown_fields))
            return {}, f"Invalid limits.{period}: " + "; ".join(details)
        period_values: dict[str, object] = {}
        for field_name in ("usd", "input_tokens", "output_tokens"):
            raw = raw_period[field_name]
            qualified = f"limits.{period}.{field_name}"
            if field_name == "usd":
                if isinstance(raw, bool):
                    return {}, f"{qualified} must be a non-negative number."
                try:
                    parsed: float | int = float(raw)
                except (TypeError, ValueError):
                    return {}, f"{qualified} must be a non-negative number."
                if not math.isfinite(parsed) or parsed < 0:
                    return {}, f"{qualified} must be a non-negative number."
            else:
                if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                    return {}, f"{qualified} must be a non-negative integer."
                parsed = raw
            period_values[field_name] = parsed
        if raw_period.get("thresholds") is not None:
            try:
                period_values["thresholds"] = normalize_thresholds(
                    raw_period["thresholds"], name=f"limits.{period}.thresholds"
                )
            except ValueError as exc:
                return {}, str(exc)
        values[period] = period_values
    if with_defaults and not any(value is not None for value in values.values()):
        return {}, "At least one quota period must be enabled."
    return values, ""


@app.post("/admin/users")
async def create_user(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    user_id = body.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return _error(
            400,
            "user_id is required (the configured JWT claim value).",
            "invalid_request_error",
        )
    user_id = user_id.strip()
    try:
        validate_user_id(user_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    if _is_workload_id(user_id):
        # Workload rows are created by metering from the deployed roster; a
        # hand-made one would be an unregistered orphan nothing enforces.
        return _error(
            400,
            "The 'workload:' namespace is reserved for workload-mode "
            "subjects. Declare workloads in the deployment config "
            "(workloads.json) and redeploy; their rows appear on first "
            "metered invocation.",
            "invalid_request_error",
        )
    name = body.get("name", user_id)
    if not isinstance(name, str) or not name.strip():
        return _error(
            400, "name must be a non-empty string.", "invalid_request_error"
        )
    limits, limit_error = _parse_limits(body, with_defaults=True)
    if limit_error:
        return _error(400, limit_error, "invalid_request_error")
    rate, rate_present, rate_error = _parse_rate(body)
    if rate_error:
        return _error(400, rate_error, "invalid_request_error")
    if rate_present:
        limits["rate"] = rate
    elif configured_default_limits().get("rate") is not None:
        limits["rate"] = configured_default_limits()["rate"]
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert request_id is not None
    try:
        result = store().create_admin_user(
            user_id=user_id,
            name=name.strip(),
            limits=limits,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=_request_hash(request, body, principal),
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except UserAlreadyExists as exc:
        return _error(
            409,
            f"User '{user_id}' already exists.",
            "user_already_exists",
            headers={"ETag": _etag(exc.current_user)},
            details={"current_user": _user_json(exc.current_user)},
        )
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {
            "user_id": user_id,
            "provisioned": True,
            "limits": _limits_json(user),
        },
        user,
        request_id,
    )


@app.get("/admin/users")
async def list_users(
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
    status: str | None = None,
    query: str | None = None,
    granularity: str | None = None,
    include_usage: bool = True,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if limit < 1 or limit > 1000:
        return _error(
            400, "limit must be between 1 and 1000.", "invalid_request_error"
        )
    if status is not None and status not in {"active", "blocked"}:
        return _error(
            400,
            "status must be 'active' or 'blocked'.",
            "invalid_request_error",
        )
    if granularity is not None and granularity not in {"user", "workload"}:
        return _error(
            400,
            "granularity must be 'user' or 'workload'.",
            "invalid_request_error",
        )
    try:
        users, next_cursor = store().list_users_page(
            limit=limit,
            cursor=cursor,
            status=status,
            query=query,
            granularity=granularity,
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    if not include_usage:
        return JSONResponse(
            {
                "users": [_user_json(user) for user in users],
                "next_cursor": next_cursor,
            }
        )
    rows = [_usage_row(user) for user in users]
    return JSONResponse({"users": rows, "next_cursor": next_cursor})


def _usage_row(user: UserRecord) -> dict:
    current_usage = _current_usage_json(user.user_id)
    return {
        **_user_json(user),
        "today": {
            key: current_usage["daily"][key]
            for key in ("cost_usd", "input_tokens", "output_tokens", "requests")
        },
        "current_usage": current_usage,
    }


@app.get("/admin/workloads")
async def list_workloads(request: Request) -> Response:
    """Deployed roster joined with the metered ``workload:`` rows.

    A roster entry without a row is a workload configured at deploy time
    that has not invoked yet (its row is created by the first metered
    invocation); a row without a roster entry is unregistered. Workload
    counts are bounded by the deployment config, so no pagination.
    """
    if (denied := _require_admin(request)) is not None:
        return denied
    roster = _workload_registry()
    rows = {
        user.user_id: user
        for user in store().list_users()
        if _is_workload_id(user.user_id)
    }
    workloads = []
    for workload_id in sorted(set(roster) | set(rows)):
        identity = _workload_json(workload_id, roster.get(workload_id))
        user = rows.get(workload_id)
        workloads.append(
            {
                **identity,
                "subject": _usage_row(user) if user is not None else None,
            }
        )
    return JSONResponse(
        {
            "workloads": workloads,
            "roster_source": _workload_roster_source(),
            "tag_key": settings.workload_tag_key,
        }
    )


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iso_timestamp(value) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value else None


def _metric_points(result: dict) -> list[tuple[datetime, float]]:
    points = []
    for timestamp, value in zip(
        result.get("Timestamps", []), result.get("Values", []), strict=False
    ):
        if isinstance(timestamp, datetime):
            points.append((timestamp.astimezone(timezone.utc), float(value)))
    return sorted(points, key=lambda point: point[0], reverse=True)


AUTO_BLOCK_SWEEP_SCHEDULE = "00:05 UTC daily"


def _auto_block_sweep_state() -> dict:
    """Last nightly auto-block sweep for the operations page.

    ``last_run`` is None until the sweeper has completed one real pass; the
    UI then says "never ran" rather than showing zeros that look like a
    healthy pass. Failures on the last pass are surfaced as ``status:
    failed`` alongside the alarm chip.
    """
    last_run = store().get_auto_block_sweep_state()
    if last_run is None:
        status = "never_ran"
    elif last_run.get("failures"):
        status = "failed"
    else:
        status = "ok"
    return {
        "schedule": AUTO_BLOCK_SWEEP_SCHEDULE,
        "status": status,
        "last_run": last_run,
    }


def _safe_emergency_state() -> dict:
    raw = store().get_emergency_state()
    state = str(raw.get("state", "inactive"))
    desired_active = bool(raw.get("desired_active", False))
    generation = int(raw.get("generation", 0))
    applied_generation = int(raw.get("applied_generation", 0))
    stable_state = "active" if desired_active else "inactive"
    converged = (
        generation == 0 and state == "inactive" and not desired_active
    ) or (state == stable_state and applied_generation == generation)
    return {
        "state": state,
        "desired_active": desired_active,
        "generation": generation,
        "applied_generation": applied_generation,
        "requested_at": _iso_timestamp(raw.get("requested_at")),
        "applied_at": _iso_timestamp(raw.get("applied_at")),
        "converged": converged,
    }


def _empty_operations_metrics(reconciliation_status: str) -> dict:
    return {
        "namespace": settings.metrics_namespace,
        "detection_lag_metric": "DetectionLagMilliseconds",
        "detection_lag_p95_ms": None,
        "detection_lag_timestamp": None,
        "telemetry_status": "unknown",
        "last_reconciliation_at": None,
        "reconciliation_status": reconciliation_status,
        "revoked_identities_desired": None,
        "recent_sync_failure_count": None,
        "recent_overflow_count": None,
        "recent_emergency_failure_count": None,
        "window_minutes": 15,
    }


def _read_operations_cloudwatch(
    now: datetime,
    *,
    alarm_names: dict[str, str],
) -> tuple[dict, list[dict], dict]:
    metrics = _empty_operations_metrics("unknown")
    alarms = [
        {"key": key, "state": "UNAVAILABLE", "updated_at": None}
        for key in alarm_names
    ]
    try:
        client = cloudwatch_client()
        metric_specs = (
            ("detectionlag", "DetectionLagMilliseconds", "p95"),
            ("revsyncsuccess", "RevocationSyncSuccess", "Sum"),
            ("revsyncfailure", "RevocationSyncFailure", "Sum"),
            ("revoverflow", "RevocationPolicyOverflow", "Sum"),
            ("revokeddesired", "RevokedIdentitiesDesired", "Maximum"),
            ("emergencyfailure", "EmergencyStopFailure", "Sum"),
        )
        response = client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": settings.metrics_namespace,
                            "MetricName": metric_name,
                        },
                        "Period": 300,
                        "Stat": statistic,
                    },
                    "ReturnData": True,
                }
                for query_id, metric_name, statistic in metric_specs
            ],
            StartTime=now - timedelta(hours=24),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        result_by_id = {
            str(result.get("Id", "")): result
            for result in response.get("MetricDataResults", [])
        }
        expected_ids = {query_id for query_id, _, _ in metric_specs}
        complete_ids = {
            query_id
            for query_id, result in result_by_id.items()
            if result.get("StatusCode") == "Complete"
        }
        incomplete_ids = expected_ids - complete_ids
        results = {
            query_id: _metric_points(result)
            for query_id, result in result_by_id.items()
            if query_id in complete_ids
        }
        recent_cutoff = now - timedelta(minutes=15)
        detection = results.get("detectionlag", [])
        success = results.get("revsyncsuccess", [])
        revoked = results.get("revokeddesired", [])
        recent_failures = sum(
            value
            for timestamp, value in results.get("revsyncfailure", [])
            if timestamp >= recent_cutoff
        )
        recent_overflow = sum(
            value
            for timestamp, value in results.get("revoverflow", [])
            if timestamp >= recent_cutoff
        )
        recent_emergency_failures = sum(
            value
            for timestamp, value in results.get("emergencyfailure", [])
            if timestamp >= recent_cutoff
        )
        metrics["telemetry_status"] = (
            "partial"
            if "detectionlag" in incomplete_ids
            else ("complete" if detection else "no_samples")
        )
        if detection:
            metrics["detection_lag_p95_ms"] = detection[0][1]
            metrics["detection_lag_timestamp"] = detection[0][0].isoformat()
        if success:
            metrics["last_reconciliation_at"] = success[0][0].isoformat()
        if revoked:
            metrics["revoked_identities_desired"] = int(revoked[0][1])
        metrics["recent_sync_failure_count"] = int(recent_failures)
        metrics["recent_overflow_count"] = int(recent_overflow)
        metrics["recent_emergency_failure_count"] = int(
            recent_emergency_failures
        )

        described = client.describe_alarms(
            AlarmNames=list(alarm_names.values())
        ) if alarm_names else {"MetricAlarms": []}
        alarm_by_name = {
            alarm.get("AlarmName"): alarm
            for alarm in described.get("MetricAlarms", [])
        }
        alarms = []
        for key, alarm_name in alarm_names.items():
            alarm = alarm_by_name.get(alarm_name, {})
            alarms.append(
                {
                    "key": key,
                    "state": str(
                        alarm.get("StateValue", "INSUFFICIENT_DATA")
                    ),
                    "updated_at": _iso_timestamp(
                        alarm.get("StateUpdatedTimestamp")
                    ),
                }
            )

        # The keys the stack registers in OPERATIONS_ALARM_NAMES_JSON for the
        # revocation path: the processor's own alarms plus the dispatcher
        # that feeds it (its DLQ and stream iterator age).
        revocation_alarm_keys = {
            "revocation_failure",
            "revocation_overflow",
            "enforcement_dispatch_dlq",
            "enforcement_dispatch_iterator_age",
        }
        revocation_alarm_states = {
            alarm["key"]: alarm["state"]
            for alarm in alarms
            if alarm["key"] in revocation_alarm_keys
        }
        revocation_alarm = any(
            state == "ALARM"
            for state in revocation_alarm_states.values()
        )
        revocation_alarms_complete = (
            set(revocation_alarm_states) == revocation_alarm_keys
            and all(
                state == "OK"
                for state in revocation_alarm_states.values()
            )
        )
        revocation_queries_complete = {
            "revsyncsuccess",
            "revsyncfailure",
            "revoverflow",
            "revokeddesired",
        }.issubset(complete_ids)
        if recent_failures or recent_overflow or revocation_alarm:
            metrics["reconciliation_status"] = "degraded"
        elif not revocation_queries_complete or not revocation_alarms_complete:
            metrics["reconciliation_status"] = "unknown"
        elif not success:
            metrics["reconciliation_status"] = "unknown"
        else:
            stale_seconds = max(
                900, settings.revocation_reconcile_minutes * 180
            )
            metrics["reconciliation_status"] = (
                "stale"
                if (now - success[0][0]).total_seconds() > stale_seconds
                else "current"
            )
        cloudwatch_state = "partial" if incomplete_ids else "available"
        return metrics, alarms, {"status": cloudwatch_state}
    except (BotoCoreError, ClientError) as exc:
        error_code = (
            exc.response.get("Error", {}).get("Code", "ClientError")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        return metrics, alarms, {
            "status": "unavailable",
            "error_code": error_code,
        }


# Usage graphs read the EMF metrics the usage processor emits, because only
# CloudWatch carries the per-Model dimension; the DynamoDB daily ledger stays
# the canonical quota source and has no model breakdown.
USAGE_METRIC_SPECS = (
    ("cost_usd", "EstimatedCostUSD"),
    ("requests", "Requests"),
    ("input_tokens", "InputTokens"),
    ("output_tokens", "OutputTokens"),
)
USAGE_METRICS_MAX_DAYS = 30
USAGE_METRICS_MAX_MODELS = 20
# Safety cap on identities ranked for top_users. ListMetrics is paginated to
# the end and every identity is queried (in GetMetricData batches), so the
# ranking is over all metered identities, not an alphabetical prefix. Beyond
# the cap the response is marked ``partial``. A Metrics Insights
# ``GROUP BY UserId`` would be cheaper but only spans two weeks of data,
# short of this endpoint's 30-day range.
USAGE_METRICS_MAX_USERS = 2_000
USAGE_METRICS_TOP_USERS = 5
# GetMetricData accepts at most 500 MetricDataQueries per call.
GET_METRIC_DATA_MAX_QUERIES = 500


def _list_dimension_values(
    client, dimension: str, limit: int
) -> tuple[list[str], bool]:
    """Distinct values of one EMF dimension via ListMetrics, sorted.

    Pages to the end of the listing; the boolean reports whether ``limit``
    truncated the result. CloudWatch only lists metrics that received data
    points in roughly the last two weeks, so a 30-day range can omit
    identities idle since then.
    """
    values: list[str] = []
    seen: set[str] = set()
    kwargs = {
        "Namespace": settings.metrics_namespace,
        "MetricName": "Requests",
        "Dimensions": [{"Name": dimension}],
    }
    while True:
        response = client.list_metrics(**kwargs)
        for metric in response.get("Metrics", []):
            for dim in metric.get("Dimensions", []):
                if dim.get("Name") != dimension:
                    continue
                value = str(dim.get("Value", ""))
                if value and value not in seen:
                    seen.add(value)
                    values.append(value)
        token = response.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    values.sort()
    return values[:limit], len(values) > limit


def _get_metric_data_batched(
    client, queries: list[dict], start_time: datetime, end_time: datetime
) -> tuple[dict[str, dict], bool]:
    """Run ``queries`` in GetMetricData batches of 500, keyed by query Id.

    The boolean reports whether any result came back other than Complete.
    """
    results: dict[str, dict] = {}
    incomplete = False
    for offset in range(0, len(queries), GET_METRIC_DATA_MAX_QUERIES):
        response = client.get_metric_data(
            MetricDataQueries=queries[
                offset : offset + GET_METRIC_DATA_MAX_QUERIES
            ],
            StartTime=start_time,
            EndTime=end_time,
            ScanBy="TimestampDescending",
        )
        for result in response.get("MetricDataResults", []):
            query_id = str(result.get("Id", ""))
            results[query_id] = result
            if result.get("StatusCode") != "Complete":
                incomplete = True
    return results, incomplete


def _usage_metric_query(query_id: str, metric_name: str,
                        dimensions: list[dict]) -> dict:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": settings.metrics_namespace,
                "MetricName": metric_name,
                "Dimensions": dimensions,
            },
            # Daily buckets; CloudWatch aligns 86400-second periods to UTC
            # midnight, matching the quota calendar windows.
            "Period": 86400,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


def _usage_series(result: dict, day_index: dict[str, int],
                  bucket_count: int) -> list[float]:
    series = [0.0] * bucket_count
    for timestamp, value in _metric_points(result):
        index = day_index.get(timestamp.date().isoformat())
        if index is not None:
            series[index] = float(value)
    return series


@app.get("/admin/usage/metrics")
async def admin_usage_metrics(request: Request, days: int = 14) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if days < 1 or days > USAGE_METRICS_MAX_DAYS:
        return _error(
            400,
            f"days must be between 1 and {USAGE_METRICS_MAX_DAYS}.",
            "invalid_request_error",
        )
    now = datetime.now(timezone.utc)
    start_date = now.date() - timedelta(days=days - 1)
    day_keys = [
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range(days)
    ]
    day_index = {key: index for index, key in enumerate(day_keys)}
    start_time = datetime.combine(
        start_date, datetime.min.time(), tzinfo=timezone.utc
    )
    payload: dict = {
        "as_of": now.isoformat(),
        "start": day_keys[0],
        "end": day_keys[-1],
        "period": "daily",
        "days": day_keys,
        "models": [],
        "totals": {key: 0.0 for key, _ in USAGE_METRIC_SPECS},
        "top_users": [],
    }
    try:
        client = cloudwatch_client()
        models, _ = _list_dimension_values(
            client, "Model", USAGE_METRICS_MAX_MODELS
        )
        user_ids, users_truncated = _list_dimension_values(
            client, "UserId", USAGE_METRICS_MAX_USERS
        )
        queries = [
            _usage_metric_query(f"t_{key}", metric_name, [])
            for key, metric_name in USAGE_METRIC_SPECS
        ]
        for model_index, model in enumerate(models):
            dimensions = [{"Name": "Model", "Value": model}]
            queries.extend(
                _usage_metric_query(
                    f"m{model_index}_{key}", metric_name, dimensions
                )
                for key, metric_name in USAGE_METRIC_SPECS
            )
        for user_index, user_id in enumerate(user_ids):
            dimensions = [{"Name": "UserId", "Value": user_id}]
            queries.append(
                _usage_metric_query(
                    f"u{user_index}_cost", "EstimatedCostUSD", dimensions
                )
            )
            queries.append(
                _usage_metric_query(
                    f"u{user_index}_requests", "Requests", dimensions
                )
            )
        results, incomplete = _get_metric_data_batched(
            client, queries, start_time, now
        )
        # Identities beyond the cap were never queried, so the ranking
        # could be missing a heavy spender: say so rather than imply
        # completeness.
        incomplete = incomplete or users_truncated
        for key, _ in USAGE_METRIC_SPECS:
            series = _usage_series(
                results.get(f"t_{key}", {}), day_index, days
            )
            payload["totals"][key] = round(sum(series), 6)
        for model_index, model in enumerate(models):
            series = {
                key: _usage_series(
                    results.get(f"m{model_index}_{key}", {}),
                    day_index,
                    days,
                )
                for key, _ in USAGE_METRIC_SPECS
            }
            payload["models"].append(
                {
                    "model": model,
                    "series": series,
                    "totals": {
                        key: round(sum(values), 6)
                        for key, values in series.items()
                    },
                }
            )
        user_totals = []
        for user_index, user_id in enumerate(user_ids):
            cost = sum(
                value
                for _, value in _metric_points(
                    results.get(f"u{user_index}_cost", {})
                )
            )
            requests = sum(
                value
                for _, value in _metric_points(
                    results.get(f"u{user_index}_requests", {})
                )
            )
            if cost or requests:
                user_totals.append(
                    {
                        "user_id": user_id,
                        "cost_usd": round(cost, 6),
                        "requests": int(requests),
                    }
                )
        user_totals.sort(
            key=lambda item: (-item["cost_usd"], -item["requests"])
        )
        top_users = user_totals[:USAGE_METRICS_TOP_USERS]
        # CloudWatch dimensions carry only the raw quota key. Resolve the
        # display name from the users table for the handful of entries shown;
        # identities deleted since they metered fall back to the raw key.
        for entry in top_users:
            try:
                user = store().get_user(entry["user_id"])
            except (ValueError, ClientError):
                user = None
            entry["name"] = (
                user.name if user is not None and user.name
                else entry["user_id"]
            )
            entry["granularity"] = (
                "workload" if _is_workload_id(entry["user_id"]) else "user"
            )
        payload["top_users"] = top_users
        payload["status"] = "partial" if incomplete else "available"
    except (BotoCoreError, ClientError) as exc:
        payload["status"] = "unavailable"
        payload["error_code"] = (
            exc.response.get("Error", {}).get("Code", "ClientError")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
    return JSONResponse(payload)


@app.get("/admin/audit")
async def admin_audit(
    request: Request,
    user_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    if user_id is not None:
        try:
            validate_user_id(user_id)
        except ValueError as exc:
            return _error(400, str(exc), "invalid_request_error")
    try:
        events, next_cursor = store().list_admin_audit_page(
            user_id=user_id, limit=limit, cursor=cursor
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse({"events": events, "next_cursor": next_cursor})


@app.get("/admin/operations")
async def admin_operations(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    now = datetime.now(timezone.utc)
    enforcement_config = store().get_enforcement_config()
    effective_lease = int(enforcement_config["permission_lease_seconds"])
    alarm_names = {
        str(key): str(value)
        for key, value in _json_object(
            settings.operations_alarm_names_json
        ).items()
        if key and value
    }
    metrics, alarms, cloudwatch_status = _read_operations_cloudwatch(
        now, alarm_names=alarm_names
    )
    return JSONResponse(
        {
            "as_of": now.isoformat(),
            "configuration": {
                "mode": "layered",
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": effective_lease,
                "permission_lease_source": enforcement_config["source"],
                "permission_lease_default_seconds": (
                    settings.permission_lease_seconds
                ),
                "refresh_overlap_seconds": settings.refresh_overlap_seconds,
                "refresh_jitter_seconds": settings.refresh_jitter_seconds,
                "vend_rate_limit_per_minute": (
                    settings.vend_rate_limit_per_minute
                ),
                "revocation_policy_shards": (
                    settings.revocation_policy_shards
                ),
                "revocation_policy_max_characters": (
                    settings.revocation_policy_max_characters
                ),
                "revocation_reconcile_minutes": (
                    settings.revocation_reconcile_minutes
                ),
            },
            "emergency": _safe_emergency_state(),
            "auto_block_sweep": _auto_block_sweep_state(),
            "metrics": metrics,
            "alarms": alarms,
            "cloudwatch": cloudwatch_status,
        }
    )


@app.get("/admin/emergency-stop")
async def get_emergency_stop(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    return JSONResponse(store().get_emergency_state())


@app.get("/admin/reconciliation")
async def admin_reconciliation(request: Request, limit: int = 14) -> Response:
    """Recent daily ledger-vs-Cost-Explorer reconciliation runs.

    Served from stored ``RECONCILE#`` rows only; the broker never queries
    Cost Explorer. When the feature is off at deploy time the payload says so
    explicitly instead of returning an empty list that looks like "no drift".
    """
    if (denied := _require_admin(request)) is not None:
        return denied
    limit = max(1, min(limit, 90))
    if not settings.reconciliation_enabled:
        return JSONResponse(
            {
                "enabled": False,
                "runs": [],
                "message": (
                    "Reconciliation is disabled for this deployment. Set "
                    "reconciliation_enabled=true in the deployment config to "
                    "compare the ledger against Cost Explorer daily."
                ),
            }
        )
    runs = store().list_reconciliation_runs(limit=limit)
    return JSONResponse(
        {
            "enabled": True,
            "lag_days": settings.reconcile_lag_days,
            "runs": runs,
            "latest": runs[0] if runs else None,
        }
    )


@app.get("/admin/enforcement")
async def get_enforcement(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    config = store().get_enforcement_config()
    return JSONResponse(
        {
            **config,
            "valid_permission_lease_seconds": sorted(
                VALID_PERMISSION_LEASE_SECONDS
            ),
            "default_permission_lease_seconds": (
                settings.permission_lease_seconds
            ),
        },
        headers={"ETag": f'"{int(config["generation"])}"'},
    )


@app.put("/admin/enforcement")
async def set_enforcement(request: Request) -> Response:
    """Runtime enforcement dial: change the permission-lease window.

    Applies to new vends immediately (the vend path reads the row with a
    strongly consistent get); outstanding credentials keep their issued
    deadline, and the revocation layer keeps cutting blocked identities
    regardless of the dial. No redeploy involved.
    """
    if (denied := _require_admin(request)) is not None:
        return denied
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    raw_seconds = body.get("permission_lease_seconds")
    if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, int):
        return _error(
            400,
            "permission_lease_seconds must be an integer.",
            "invalid_request_error",
        )
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    current = store().get_enforcement_config()
    raw_match = request.headers.get("if-match")
    if raw_match is None:
        expected_generation = int(current["generation"])
    else:
        normalized_match = raw_match.strip().removeprefix("W/").strip('"')
        try:
            expected_generation = int(normalized_match)
        except ValueError:
            return _error(
                400,
                "If-Match must contain the enforcement generation.",
                "invalid_request_error",
            )
        if expected_generation < 0:
            return _error(
                400,
                "If-Match must contain the enforcement generation.",
                "invalid_request_error",
            )
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert request_id is not None
    try:
        config = store().set_permission_lease_seconds(
            raw_seconds,
            actor=principal.actor,
            reason=reason,
            expected_generation=expected_generation,
            idempotency_key=request_id,
        )
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except EnforcementVersionConflict as exc:
        return _error(
            409,
            "The enforcement dial changed; refresh and retry.",
            "version_conflict",
            headers={"ETag": f'"{int(exc.current["generation"])}"'},
            details={"current_enforcement": exc.current},
        )
    except ClientError:
        return _transaction_unavailable()
    replayed = bool(config.pop("_replayed", False))
    if not replayed:
        emf.record_enforcement_dial(principal.actor, raw_seconds)
    return JSONResponse(
        config, headers={"ETag": f'"{int(config["generation"])}"'}
    )


@app.post("/admin/emergency-stop", status_code=202)
async def set_emergency_stop(request: Request) -> Response:
    denied, emergency_actor = _require_emergency_admin(request)
    if denied is not None:
        return denied
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    action = body.get("action")
    confirmations = {
        "activate": "STOP_ALL_BEDROCK_SESSIONS",
        "recover": "RESTORE_ALL_BEDROCK_SESSIONS",
    }
    if action not in confirmations:
        return _error(
            400,
            "action must be 'activate' or 'recover'.",
            "invalid_request_error",
        )
    if body.get("confirmation") != confirmations[action]:
        return _error(
            400,
            f"confirmation must be {confirmations[action]!r}.",
            "confirmation_required",
        )
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error(
            400,
            "reason must be a non-empty string.",
            "invalid_request_error",
        )
    desired_active = action == "activate"
    current = store().get_emergency_state()
    stable_state = "active" if desired_active else "inactive"
    if (
        bool(current.get("desired_active")) == desired_active
        and current.get("state") == stable_state
    ):
        return JSONResponse(
            {**current, "idempotent": True, "retry": False},
            status_code=202,
        )
    retry = bool(current.get("desired_active")) == desired_active
    try:
        state = store().set_emergency_desired(
            active=desired_active,
            actor=emergency_actor,
            reason=reason.strip(),
        )
    except EmergencyVersionConflict as exc:
        # Two operators raced (activate against recover, typically). Neither
        # request is silently dropped or overwritten: the loser gets the
        # state the winner wrote and decides whether to re-issue.
        return _error(
            409,
            "The emergency-stop state changed concurrently; re-read and retry.",
            "version_conflict",
            details={"current_emergency": exc.current},
        )
    return JSONResponse(
        {**state, "idempotent": False, "retry": retry},
        status_code=202,
    )


@app.get("/admin/summary")
async def admin_summary(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    now = datetime.now(timezone.utc)
    users = store().list_users()
    blocked = [user.user_id for user in users if not user.active]

    def _empty_usage() -> dict:
        return {
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 0,
        }

    aggregate = _empty_usage()
    # Per-kind breakdown: JWT identities vend credentials through the
    # broker; workloads are apps on their own IAM principals metered by
    # application inference profile. They share a ledger but not a control
    # path, so the overview reports them side by side.
    subjects = {
        kind: {"total": 0, "blocked": 0, "today": _empty_usage()}
        for kind in ("users", "workloads")
    }
    roster = _workload_registry()
    subjects["workloads"]["configured"] = len(roster)
    subjects["workloads"]["metering_only"] = 0
    subjects["workloads"]["unregistered"] = 0
    seen_workloads: set[str] = set()
    for user in users:
        usage = store().get_window_usage(user.user_id)
        kind = "workloads" if _is_workload_id(user.user_id) else "users"
        bucket = subjects[kind]
        bucket["total"] += 1
        if not user.active:
            bucket["blocked"] += 1
        for key in aggregate:
            aggregate[key] += usage.get(key, 0)
            bucket["today"][key] += usage.get(key, 0)
        if kind == "workloads":
            seen_workloads.add(user.user_id)
            entry = roster.get(user.user_id)
            if not isinstance(entry, dict):
                bucket["unregistered"] += 1
            elif not entry.get("enforcement_ready"):
                bucket["metering_only"] += 1
    # Roster entries whose row has not been created yet (no invocation
    # since deploy) still count as configured-but-silent for the operator.
    subjects["workloads"]["awaiting_traffic"] = len(
        [wid for wid in roster if wid not in seen_workloads]
    )
    enforcement_config = store().get_enforcement_config()
    effective_lease = int(enforcement_config["permission_lease_seconds"])
    return JSONResponse(
        {
            "enforcement": {
                # Layered enforcement: the permission lease bounds every
                # vend, SourceIdentity revocation cuts blocked identities,
                # and the emergency stop halts everything.
                "mode": "layered",
                "source": "dynamodb",
                "as_of": now.isoformat(),
                "window": now.strftime("%Y-%m-%d"),
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": effective_lease,
                "permission_lease_source": enforcement_config["source"],
                "refresh_overlap_seconds": (
                    settings.refresh_overlap_seconds
                ),
                "refresh_jitter_seconds": settings.refresh_jitter_seconds,
                "vend_rate_limit_per_minute": (
                    settings.vend_rate_limit_per_minute
                ),
                "revocation_policy_shards": (
                    settings.revocation_policy_shards
                ),
                "revocation_reconcile_minutes": (
                    settings.revocation_reconcile_minutes
                ),
                "total_users": len(users),
                "blocked_users": len(blocked),
                "blocked_user_ids": blocked,
                "today": aggregate,
                # total_users / blocked_users / today above stay as the
                # all-subjects figures; this splits them by control path.
                "subjects": subjects,
            },
            "observability": {
                "source": "bedrock_model_invocation_logs",
                "delivery": "cloudwatch_logs_subscription",
                "metrics_namespace": settings.metrics_namespace,
                "detection_lag_metric": "DetectionLagMilliseconds",
            },
        }
    )


def _usage_history_range(
    start: str | None, end: str | None
) -> tuple[tuple[str, str] | None, JSONResponse | None]:
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=settings.usage_retention_days)

    def parse(value: str | None, default: date) -> date:
        return default if value is None else date.fromisoformat(value)

    try:
        start_date = parse(start, oldest)
        end_date = parse(end, today)
    except (TypeError, ValueError):
        return None, _error(
            400,
            "start and end must be ISO dates (YYYY-MM-DD).",
            "invalid_date_range",
        )
    if start_date > end_date:
        return None, _error(
            400, "start must not be after end.", "invalid_date_range"
        )
    if start_date < oldest or end_date > today:
        return None, _error(
            400,
            "Requested usage range is outside configured retention.",
            "usage_range_outside_retention",
            details={
                "oldest_available_date": oldest.isoformat(),
                "latest_available_date": today.isoformat(),
            },
        )
    return (start_date.isoformat(), end_date.isoformat()), None


# Stored as the audit reason when an admin mutation omits or blanks it.
DEFAULT_ADMIN_REASON = "not provided"


def _canonical_user_id_error(
    request: Request, user_id: str | None
) -> JSONResponse | None:
    values = request.query_params.getlist("user_id")
    if user_id is None or len(values) != 1 or values[0] != user_id:
        return _error(
            400,
            "user_id must be provided exactly once.",
            "invalid_request_error",
        )
    try:
        validate_user_id(user_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    return None


def _admin_reason(body: dict) -> tuple[str | None, JSONResponse | None]:
    reason = body.get("reason", DEFAULT_ADMIN_REASON)
    if not isinstance(reason, str):
        return None, _error(
            400, "reason must be a string.", "invalid_request_error"
        )
    return reason.strip() or DEFAULT_ADMIN_REASON, None


def _user_usage_history_response(
    user_id: str,
    period: str,
    start: str | None,
    end: str | None,
    limit: int,
    cursor: str | None,
) -> Response:
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    user = store().get_user(user_id)
    if user is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    if period not in PERIODS:
        return _error(
            400,
            "period must be 'daily', 'weekly', or 'monthly'.",
            "invalid_request_error",
        )
    if period != "daily":
        today = datetime.now(timezone.utc)
        oldest = today.date() - timedelta(days=settings.usage_retention_days)
        if start is None:
            first = calendar_window(
                period,
                datetime.combine(oldest, datetime.min.time(), tzinfo=timezone.utc),
            )
            if first.start.date() < oldest:
                first = calendar_window(period, first.end)
            start = first.key
        if end is None:
            end = calendar_window(period, today).key
        try:
            period_for_start(period, start)
            period_for_start(period, end)
        except ValueError as exc:
            return _error(400, str(exc), "invalid_date_range")
    date_range, range_error = _usage_history_range(start, end)
    if range_error is not None:
        return range_error
    assert date_range is not None
    try:
        history, next_cursor = store().get_period_usage_history_page(
            user_id,
            period=period,
            start=date_range[0],
            end=date_range[1],
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {
            "user_id": user_id,
            "period": period,
            "start": date_range[0],
            "end": date_range[1],
            "usage": history,
            "next_cursor": next_cursor,
        }
    )


@app.get("/admin/user/usage-history")
async def canonical_user_usage_history(
    request: Request,
    user_id: str | None = None,
    period: str = "daily",
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_usage_history_response(
        user_id, period, start, end, limit, cursor
    )


def _user_admin_audit_response(
    user_id: str, limit: int, cursor: str | None
) -> Response:
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    try:
        events, next_cursor = store().list_admin_audit_page(
            user_id=user_id, limit=limit, cursor=cursor
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {"user_id": user_id, "events": events, "next_cursor": next_cursor}
    )


@app.get("/admin/user/audit")
async def canonical_user_admin_audit(
    request: Request,
    user_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_admin_audit_response(user_id, limit, cursor)


def _user_usage_response(
    user_id: str, period: str, window: str | None
) -> Response:
    if period not in PERIODS:
        return _error(
            400,
            "period must be 'daily', 'weekly', or 'monthly'.",
            "invalid_request_error",
        )
    try:
        usage = store().get_period_usage(user_id, period, window)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    return JSONResponse({"user_id": user_id, **_period_usage_json(usage)})


@app.get("/admin/user/usage")
async def canonical_user_usage(
    request: Request,
    user_id: str | None = None,
    period: str = "daily",
    window: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_usage_response(user_id, period, window)


def _user_detail_response(user_id: str) -> Response:
    user = store().get_user(user_id)
    if user is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    return JSONResponse(
        {
            "user": _user_json(user),
            "current_usage": _current_usage_json(user.user_id),
        },
        headers={"ETag": _etag(user)},
    )


@app.get("/admin/user")
async def canonical_user_detail(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_detail_response(user_id)


async def _set_limits_response(user_id: str, request: Request) -> Response:
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    current = store().get_user(user_id)
    if current is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    limits, limit_error = _parse_limits(body, with_defaults=False)
    if limit_error:
        return _error(400, limit_error, "invalid_request_error")
    rate, rate_present, rate_error = _parse_rate(body)
    if rate_error:
        return _error(400, rate_error, "invalid_request_error")
    if not limits and not rate_present:
        return _error(
            400,
            "At least one quota period or rate update is required.",
            "invalid_request_error",
        )
    # Merge: untouched periods keep their stored thresholds (the public
    # form round-trips through normalize_thresholds unchanged).
    merged: dict = {}
    for period, existing in _limits_json(current).items():
        merged[period] = (
            {**existing, "thresholds": existing["thresholds"]}
            if existing is not None
            else None
        )
    for period, submitted in limits.items():
        if submitted is None:
            merged[period] = None
            continue
        previous = merged.get(period)
        if "thresholds" not in submitted and previous is not None:
            # Period re-submitted without thresholds: keep the current list
            # rather than resetting it to the deployment default.
            submitted = {**submitted, "thresholds": previous["thresholds"]}
        merged[period] = submitted
    limits = merged
    if rate_present:
        limits["rate"] = rate
    if not any(
        value is not None for key, value in limits.items() if key != "rate"
    ):
        return _error(
            400,
            "At least one quota period must be enabled.",
            "invalid_request_error",
        )
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    canonical_body = (
        {**body, "reason": reason} if "reason" in body else body
    )
    expected, match_error = _expected_version(request, current)
    if match_error is not None:
        return match_error
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert expected is not None and request_id is not None
    request_hash = _request_hash(request, canonical_body, principal)
    reconciled_status = store().status_after_limit_change(current, limits)
    try:
        result = store().update_admin_limits(
            user_id,
            limits,
            reason=reason,
            expected_version=expected,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=request_hash,
            reconciled_status=reconciled_status,
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except VersionConflict as exc:
        return _version_conflict(exc)
    except KeyError:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {
            "user_id": user_id,
            "updated": True,
            "limits": _limits_json(user),
        },
        user,
        request_id,
    )


@app.put("/admin/user/limits")
async def canonical_set_limits(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _set_limits_response(user_id, request)


async def _set_status_response(user_id: str, request: Request) -> Response:
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    current = store().get_user(user_id)
    if current is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    status = body.get("status")
    if status not in {"active", "blocked"}:
        return _error(
            400,
            "status must be 'active' or 'blocked'.",
            "invalid_request_error",
        )
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    canonical_body = {**body, "reason": reason}
    expected, match_error = _expected_version(request, current)
    if match_error is not None:
        return match_error
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert expected is not None and request_id is not None
    try:
        result = store().update_admin_status(
            user_id,
            status,
            reason,
            expected_version=expected,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=_request_hash(request, canonical_body, principal),
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except VersionConflict as exc:
        return _version_conflict(exc)
    except KeyError:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {"user_id": user_id, "status": status, "reason": reason},
        user,
        request_id,
    )


@app.put("/admin/user/status")
async def canonical_set_status(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _set_status_response(user_id, request)


# ---------------------------------------------------------------------------
# Model-scoped budgets (optional second axis on a subject)
# ---------------------------------------------------------------------------
#
# A subject may carry zero or more budgets keyed by model ID, evaluated
# against per-model daily ledger rows the usage processor writes in the same
# transaction as the subject row. Enforcement stays subject-wide: the deny
# primitives operate on aws:SourceIdentity / the workload role, not on a
# model resource, so ANY model budget breach blocks the subject entirely.
# Making the deny model-selective would need per-identity resource lists in
# the 19 revocation shards and blow the 6,144-character shard cap; it is a
# documented limitation, not a roadmap item. The credential-vend pre-flight
# (vend_credentials -> QuotaStore.evaluate_user_quota) evaluates model
# budgets too: a model ledger already at its cap refuses the vend like a
# subject-level breach does. What the pre-flight cannot do is predict which
# model the session will call, so a budget that is merely close to its cap
# never withholds credentials.


def _parse_model_budget(body: dict) -> tuple[dict | None, str]:
    """``{"limits": {...}}`` -> per-period limits (same shape as the subject)."""
    if "limits" not in body:
        return None, "limits is required."
    raw_limits = body["limits"]
    if not isinstance(raw_limits, dict):
        return None, "limits must be an object."
    if "rate" in raw_limits:
        return None, "Model budgets do not support rate limits."
    limits, error = _parse_limits({"limits": raw_limits}, with_defaults=False)
    if error:
        return None, error
    for period in PERIODS:
        limits.setdefault(period, None)
    if not any(value is not None for value in limits.values()):
        return None, "A model budget must enable at least one quota period."
    return limits, ""


async def _model_budget_mutation(
    user_id: str, request: Request, *, remove: bool
) -> Response:
    principal: AdminPrincipal = request.state.admin_principal
    model_id = request.query_params.get("model_id")
    if (
        model_id is None
        or len(request.query_params.getlist("model_id")) != 1
    ):
        return _error(
            400,
            "model_id must be provided exactly once.",
            "invalid_request_error",
        )
    try:
        model_id = validate_model_id(model_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    current = store().get_user(user_id)
    if current is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    if remove:
        body: dict = {}
        if await request.body():
            body, error = await _admin_json_object(request)
            if error is not None:
                return error
            assert body is not None
        limits = None
        if model_id not in (current.model_budgets or {}):
            return _error(
                404,
                f"User '{user_id}' has no budget for model '{model_id}'.",
                "not_found",
            )
    else:
        body, error = await _admin_json_object(request)
        if error is not None:
            return error
        assert body is not None
        if not _model_allowed(model_id):
            return _error(
                400,
                f"Model '{model_id}' is not in this deployment's "
                "allowed_model_arns; the subject could never accrue usage "
                "against this budget.",
                "invalid_request_error",
            )
        limits, limit_error = _parse_model_budget(body)
        if limit_error:
            return _error(400, limit_error, "invalid_request_error")
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    canonical_body = {**body, "reason": reason} if "reason" in body else body
    expected, match_error = _expected_version(request, current)
    if match_error is not None:
        return match_error
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert expected is not None and request_id is not None
    request_hash = _request_hash(request, canonical_body, principal)
    # Reconcile the automatic status against the NEW budget set so a budget
    # below current model usage blocks immediately and removing the binding
    # budget lifts an automatic block.
    next_budgets = dict(current.model_budget_limits)
    if limits is None:
        next_budgets.pop(model_id, None)
    else:
        from .quota import limits_from_item, model_budget_attributes

        next_budgets[model_id] = limits_from_item(model_budget_attributes(limits))
    reconciled_status = store().status_after_limit_change(
        current, _limits_json(current), model_budgets=next_budgets
    )
    try:
        result = store().update_admin_model_budget(
            user_id,
            model_id,
            limits,
            reason=reason,
            expected_version=expected,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=request_hash,
            reconciled_status=reconciled_status,
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except VersionConflict as exc:
        return _version_conflict(exc)
    except KeyError as exc:
        if str(exc).strip("'") == model_id:
            return _error(
                404,
                f"User '{user_id}' has no budget for model '{model_id}'.",
                "not_found",
            )
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {
            "user_id": user_id,
            "model_id": model_id,
            "removed" if remove else "updated": True,
            "model_budgets": _model_budgets_json(user),
        },
        user,
        request_id,
    )


@app.put("/admin/user/model-budget")
async def canonical_set_model_budget(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _model_budget_mutation(user_id, request, remove=False)


@app.delete("/admin/user/model-budget")
async def canonical_delete_model_budget(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _model_budget_mutation(user_id, request, remove=True)


@app.get("/admin/user/model-usage")
async def canonical_user_model_usage(
    request: Request,
    user_id: str | None = None,
    model_id: str | None = None,
) -> Response:
    """Current calendar usage for one subject × model ledger."""
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    if model_id is None:
        return _error(400, "model_id is required.", "invalid_request_error")
    try:
        model_id = validate_model_id(model_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    usage = store().get_model_usage(user_id, model_id)
    return JSONResponse(
        {
            "user_id": user_id,
            "model_id": model_id,
            "current_usage": {
                period: _period_usage_json(value)
                for period, value in usage.items()
            },
        }
    )
