"""DynamoDB state used by the runtime credential broker and admin API.

Inference never passes through this application; DynamoDB holds only
configuration, the event-driven ledger, and bookkeeping. Three tables:

Users table (partition key ``user_id``):
  * One row per subject (JWT identity or ``workload:<id>``): name, status,
    ``status_reason``, ``status_origin`` (``admin`` | ``automatic``),
    per-period limits with thresholds, ``rpm``/``tpm``, the
    ``model_budgets`` map, an optimistic ``version``, the current
    permission lease (``lease_id``, generation, expiry/refresh epochs,
    duration) and the last ``source_identity`` stamped at vend.
  * ``SESSION#<name>``: RoleSessionName -> subject reverse map (TTL).
  * ``VEND#<user>#<minute>``: per-minute credential-vend counters (TTL).
  * ``REVOCATION#<user>``: desired-status sentinel the revocation
    processor turns into SourceIdentity deny statements.
  * ``CONFIG#ENFORCEMENT`` + ``CONFIG#ENFORCEMENT_AUDIT#<key>``: the
    runtime permission-lease dial and its immutable change log.
  * ``CONFIG#EMERGENCY_STOP`` + ``EMERGENCY_AUDIT#<ts>#<id>``: break-glass
    desired/applied state and its audit trail.
  * ``CONFIG#AUTO_BLOCK_SWEEP``: last nightly stale-block sweep result.

Usage table (``user_id`` + ``window``):
  * Daily ledger rows per subject (``window`` = ``YYYY-MM-DD``) with cost,
    token, request and other priced-dimension counters (TTL).
  * ``<user>#model#<model_id>``: per-model daily ledger for model budgets.
  * ``RATE#<user>`` (``window`` = minute): per-minute request/token
    counters read by the rate-limit evaluation.
  * ``RECONCILE#<day>``: stored Cost Explorer reconciliation results.

Admin-audit table (``subject_id`` + ``event_key``):
  * Routine admin audit events with before/after subject snapshots.
  * ``IDEMPOTENCY#<key>``: request-hash markers that make admin mutations
    replay-safe.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from bedrock_spend_controls.quota_periods import (
    MODEL_BUDGETS_ATTRIBUTE,
    PERIODS,
    RATE_DIMENSIONS,
    QuotaEvaluation,
    aggregate_daily_rows,
    calendar_window,
    calendar_windows,
    default_thresholds,
    evaluate_limits,
    evaluate_model_budgets,
    limit_attributes as _layer_limit_attributes,
    limits_from_item as _layer_limits_from_item,
    merge_evaluations,
    model_budget_attributes as _layer_model_budget_attributes,
    model_budgets_from_item as _layer_model_budgets_from_item,
    model_ledger_subject,
    period_for_start,
    quota_reason,
    rate_limits_enabled,
    rate_limits_from_item,
    rate_row_key,
    rate_usage_from_item,
    thresholds_from_storage,
    validate_model_id,
)
from bedrock_spend_controls.row_enforcement import (
    RESERVED_USER_ID_PREFIXES as _RESERVED_USER_ID_PREFIXES,
    WORKLOAD_USER_ID_PREFIX as _WORKLOAD_USER_ID_PREFIX,
)

from .config import settings

MICRO = 1_000_000
_SERIALIZER = TypeSerializer()
ADMIN_AUDIT_SCOPE = "routine-admin"
IDEMPOTENCY_EVENT_KEY = "REQUEST"
# Reserved bookkeeping prefixes and the workload namespace live in the shared
# layer so the scheduled enforcers filter scans with the exact same tuple.
# Re-exported here because the admin API and tests import them from quota.
RESERVED_USER_ID_PREFIXES = _RESERVED_USER_ID_PREFIXES
WORKLOAD_USER_ID_PREFIX = _WORKLOAD_USER_ID_PREFIX

# Runtime-adjustable permission-lease windows (seconds). The dial is a
# CONFIG#ENFORCEMENT row; the deployment context only sets the default.
VALID_PERMISSION_LEASE_SECONDS = (60, 300, 900)


def validate_user_id(user_id: str) -> str:
    if not user_id or any(
        user_id.startswith(prefix) for prefix in RESERVED_USER_ID_PREFIXES
    ):
        raise ValueError("user identity uses a reserved internal prefix")
    return user_id


def deployment_default_thresholds() -> list[dict]:
    """The thresholds list that rows without one resolve to.

    Built from the deployment ``warn_threshold``: a row without a stored
    thresholds list uses the deployment default (warn once, block at 100 %).
    """
    return default_thresholds(settings.warn_threshold)


def limits_from_item(item: dict) -> dict[str, dict | None]:
    return _layer_limits_from_item(
        item, default_thresholds_list=deployment_default_thresholds()
    )


def configured_default_limits() -> dict[str, dict | None]:
    raw = json.loads(settings.default_limits_json)
    if not isinstance(raw, dict):
        raise ValueError("DEFAULT_LIMITS_JSON must contain an object")
    result: dict[str, dict | None] = {}
    for period in PERIODS:
        value = raw.get(period)
        if value is None:
            result[period] = None
            continue
        if not isinstance(value, dict):
            raise ValueError(f"DEFAULT_LIMITS_JSON.{period} must be an object")
        entry: dict = {
            "usd": float(value.get("usd", 0)),
            "input_tokens": int(value.get("input_tokens", 0)),
            "output_tokens": int(value.get("output_tokens", 0)),
        }
        if value.get("thresholds") is not None:
            entry["thresholds"] = value["thresholds"]
        result[period] = entry
    if result["daily"] is None:
        raise ValueError("DEFAULT_LIMITS_JSON.daily must be enabled")
    rate = raw.get("rate")
    if rate is not None:
        result["rate"] = {
            dimension: int(rate.get(dimension, 0)) for dimension in RATE_DIMENSIONS
        }
    return result


def _limit_attributes(limits: dict[str, dict | None]) -> dict[str, object]:
    return _layer_limit_attributes(
        limits, default_thresholds_list=deployment_default_thresholds()
    )


def model_budgets_from_item(item: dict) -> dict[str, dict[str, dict | None]]:
    return _layer_model_budgets_from_item(
        item, default_thresholds_list=deployment_default_thresholds()
    )


def model_budget_attributes(limits: dict) -> dict[str, object]:
    return _layer_model_budget_attributes(
        limits, default_thresholds_list=deployment_default_thresholds()
    )


def current_window(now: datetime | None = None) -> str:
    return calendar_window("daily", now).key


def window_ttl_epoch(
    now: datetime | None = None, keep_days: int | None = None
) -> int:
    now = now or datetime.now(timezone.utc)
    keep_days = keep_days or settings.usage_retention_days
    return int(now.timestamp()) + keep_days * 86400


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    name: str
    status: str
    status_reason: str
    daily_usd_micro: int
    daily_input_tokens: int
    daily_output_tokens: int
    daily_limits_enabled: bool = True
    weekly_usd_micro: int = 0
    weekly_input_tokens: int = 0
    weekly_output_tokens: int = 0
    weekly_limits_enabled: bool = False
    monthly_usd_micro: int = 0
    monthly_input_tokens: int = 0
    monthly_output_tokens: int = 0
    monthly_limits_enabled: bool = False
    version: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    # Who owns the current status: "admin" (operators; machines never lift
    # it) or "automatic" (quota evaluation; lifted when usage recovers).
    status_origin: str = "admin"
    lease_expires_at_epoch: int | None = None
    lease_refresh_after_epoch: int | None = None
    lease_generation: int | None = None
    lease_duration_seconds: int | None = None
    # Per-period thresholds in storage form ([{at_bps, action}]); None when
    # the row has no stored list and resolves to the deployment default.
    daily_thresholds: tuple[dict, ...] | None = None
    weekly_thresholds: tuple[dict, ...] | None = None
    monthly_thresholds: tuple[dict, ...] | None = None
    # Subject-level rate limits; 0 = unlimited.
    rpm: int = 0
    tpm: int = 0
    # Model-scoped budgets in storage form: {model_id: {<period attrs>}}.
    # Evaluated against per-model daily ledger rows; any block breach blocks
    # the whole subject (enforcement is subject-wide).
    model_budgets: dict[str, dict] | None = None

    @property
    def period_limits(self) -> dict[str, dict | None]:
        item: dict[str, object] = {}
        for period in PERIODS:
            item[f"{period}_limits_enabled"] = getattr(
                self, f"{period}_limits_enabled"
            )
            item[f"{period}_usd_micro"] = getattr(self, f"{period}_usd_micro")
            item[f"{period}_input_tokens"] = getattr(
                self, f"{period}_input_tokens"
            )
            item[f"{period}_output_tokens"] = getattr(
                self, f"{period}_output_tokens"
            )
            thresholds = getattr(self, f"{period}_thresholds")
            if thresholds is not None:
                item[f"{period}_thresholds"] = list(thresholds)
        return limits_from_item(item)

    @property
    def rate_limits(self) -> dict[str, int]:
        return {"rpm": self.rpm, "tpm": self.tpm}

    @property
    def rate_limited(self) -> bool:
        return rate_limits_enabled(self.rate_limits)

    @property
    def model_budget_limits(self) -> dict[str, dict[str, dict | None]]:
        """``{model_id: {period: limits | None}}`` ready for evaluation."""
        return model_budgets_from_item(
            {MODEL_BUDGETS_ATTRIBUTE: self.model_budgets or {}}
        )

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class AdminMutationResult:
    user: UserRecord
    replayed: bool = False


class UserAlreadyExists(Exception):
    def __init__(self, current_user: UserRecord):
        super().__init__(f"user {current_user.user_id!r} already exists")
        self.current_user = current_user


class VersionConflict(Exception):
    def __init__(self, current_user: UserRecord):
        super().__init__(f"user {current_user.user_id!r} has changed")
        self.current_user = current_user


class IdempotencyConflict(Exception):
    """An idempotency key was already used for a different request."""


class EnforcementVersionConflict(Exception):
    def __init__(self, current: dict):
        super().__init__("enforcement configuration has changed")
        self.current = current


@dataclass(frozen=True)
class LeaseReservation:
    lease_id: str
    generation: int
    expires_at: datetime
    refresh_after: datetime
    created: bool
    # True when a caller presenting a different lease ID was attached to the
    # identity's current lease (another process already holds it) instead of
    # starting a new one. Joined credentials carry the current deadline.
    joined: bool = False


class LeaseNotRefreshable(Exception):
    def __init__(self, retry_after: datetime):
        super().__init__("logical lease is not in its refresh window")
        self.retry_after = retry_after


class LeaseRateLimited(Exception):
    def __init__(self, retry_after: datetime):
        super().__init__("credential vend rate limit exceeded")
        self.retry_after = retry_after


class LeaseExpired(Exception):
    """A retry used a lease ID whose fixed deadline has already passed."""


class QuotaStore:
    """Read/write surface over quota state and isolated admin audit data."""

    def __init__(
        self,
        dynamodb=None,
        *,
        lease_seconds: int | None = None,
        refresh_overlap_seconds: int | None = None,
        refresh_jitter_seconds: int | None = None,
        vend_rate_limit_per_minute: int | None = None,
        jitter_fn: Callable[[int], int] | None = None,
    ):
        self._dynamodb = dynamodb or boto3.resource(
            "dynamodb", region_name=settings.aws_region
        )
        self._users = self._dynamodb.Table(settings.users_table)
        self._usage = self._dynamodb.Table(settings.usage_table)
        self._admin_audit = self._dynamodb.Table(settings.admin_audit_table)
        # Transactions send TypeSerializer-encoded items and therefore need a
        # genuine low-level client. A boto3 *resource* meta client carries the
        # document-interface transform, which would re-serialize the already
        # encoded attribute values into nested maps ({"S": ...} -> {"M":
        # {"S": {"S": ...}}}) and make DynamoDB reject the item keys.
        self._client = (
            dynamodb.meta.client
            if dynamodb is not None
            else boto3.client("dynamodb", region_name=settings.aws_region)
        )
        self._lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else settings.permission_lease_seconds
        )
        self._refresh_overlap_seconds = (
            refresh_overlap_seconds
            if refresh_overlap_seconds is not None
            else settings.refresh_overlap_seconds
        )
        self._refresh_jitter_seconds = (
            refresh_jitter_seconds
            if refresh_jitter_seconds is not None
            else settings.refresh_jitter_seconds
        )
        self._vend_rate_limit_per_minute = (
            vend_rate_limit_per_minute
            if vend_rate_limit_per_minute is not None
            else settings.vend_rate_limit_per_minute
        )
        self._jitter = jitter_fn or (
            lambda maximum: secrets.randbelow(maximum + 1)
            if maximum > 0
            else 0
        )

    @staticmethod
    def _is_conditional_failure(exc: ClientError) -> bool:
        return exc.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }

    @staticmethod
    def _serialize(values: dict) -> dict:
        return {key: _SERIALIZER.serialize(value) for key, value in values.items()}

    @staticmethod
    def _encode_cursor(key: dict | None, context: dict) -> str | None:
        if not key:
            return None
        return json.dumps(
            {"version": 1, "key": key, "context": context},
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        *,
        key_fields: set[str],
        context: dict,
    ) -> dict | None:
        if not cursor:
            return None
        try:
            envelope = json.loads(cursor)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid cursor") from exc
        if not isinstance(envelope, dict):
            raise ValueError("invalid cursor")
        if set(envelope) != {"version", "key", "context"}:
            raise ValueError("invalid cursor")
        key = envelope.get("key")
        if (
            envelope.get("version") != 1
            or envelope.get("context") != context
            or not isinstance(key, dict)
            or set(key) != key_fields
            or not all(isinstance(value, str) for value in key.values())
        ):
            raise ValueError("invalid cursor")
        return key

    def _get_user_item(self, user_id: str) -> dict | None:
        validate_user_id(user_id)
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        return dict(item) if item else None

    def get_user(self, user_id: str) -> UserRecord | None:
        validate_user_id(user_id)
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        return self._to_user(item) if item else None

    def get_or_provision_user(
        self, user_id: str, name: str = ""
    ) -> UserRecord:
        user = self.get_user(user_id)
        if user is not None:
            return user
        try:
            self.put_user(
                user_id=user_id,
                name=name or user_id,
                limits=configured_default_limits(),
            )
        except UserAlreadyExists:
            # A concurrent admin create is authoritative. Never replace it
            # with auto-provision defaults; return the winning row instead.
            pass
        user = self.get_user(user_id)
        assert user is not None
        return user

    def put_user(
        self,
        user_id: str,
        name: str,
        *,
        limits: dict[str, dict[str, float | int] | None],
    ) -> None:
        """Create an active subject row with the given API-shaped limits."""
        validate_user_id(user_id)
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "user_id": user_id,
            "name": name,
            "status": "active",
            "status_reason": "",
            **_limit_attributes(limits),
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "status_origin": "automatic",
        }
        try:
            self._users.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            current = self.get_user(user_id)
            if current is None:
                raise
            raise UserAlreadyExists(current) from exc

    def record_session(self, session_name: str, user_id: str) -> None:
        validate_user_id(user_id)
        now = datetime.now(timezone.utc).isoformat()
        self._users.put_item(
            Item={
                "user_id": f"SESSION#{session_name}",
                "maps_to": user_id,
                "expires_at": window_ttl_epoch(),
                "updated_at": now,
            }
        )
        # Persist the exact value stamped into SourceIdentity. The revocation
        # processor reads this value and never re-derives it, so its deny
        # statements cannot drift from the identity on live sessions.
        self._users.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET source_identity = :s, session_updated_at = :t",
            ExpressionAttributeValues={":s": session_name, ":t": now},
        )

    def resolve_session(self, session_name: str) -> str | None:
        item = self._users.get_item(
            Key={"user_id": f"SESSION#{session_name}"},
            ConsistentRead=True,
        ).get("Item")
        return str(item["maps_to"]) if item else None

    @staticmethod
    def _lease_from_item(item: dict, *, created: bool) -> LeaseReservation | None:
        lease_id = item.get("lease_id")
        expires_epoch = item.get("lease_expires_at_epoch")
        refresh_epoch = item.get("lease_refresh_after_epoch")
        generation = item.get("lease_generation")
        if not all(
            value is not None
            for value in (lease_id, expires_epoch, refresh_epoch, generation)
        ):
            return None
        return LeaseReservation(
            lease_id=str(lease_id),
            generation=int(generation),
            expires_at=datetime.fromtimestamp(
                int(expires_epoch), tz=timezone.utc
            ),
            refresh_after=datetime.fromtimestamp(
                int(refresh_epoch), tz=timezone.utc
            ),
            created=created,
        )

    def get_active_lease(self, user_id: str) -> LeaseReservation | None:
        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item", {})
        return self._lease_from_item(item, created=False)

    def _consume_vend_rate(self, user_id: str, now: datetime) -> None:
        minute = now.strftime("%Y%m%dT%H%M")
        retry_after = now.replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        try:
            self._users.update_item(
                Key={"user_id": f"VEND#{user_id}#{minute}"},
                UpdateExpression=(
                    "ADD vend_count :one SET expires_at = :ttl"
                ),
                ConditionExpression=(
                    "attribute_not_exists(vend_count) OR vend_count < :limit"
                ),
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": self._vend_rate_limit_per_minute,
                    ":ttl": int(retry_after.timestamp()) + 120,
                },
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise
            raise LeaseRateLimited(retry_after) from exc

    def reserve_lease(
        self,
        user_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
        expires_no_later_than: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> LeaseReservation:
        """Reserve, retry, or join the identity's one logical permission lease.

        Each identity has at most one logical lease; every credential vended
        for it expires no later than that lease's deadline.

        * Same lease ID as the current lease: a non-extending retry that
          returns the original deadline.
        * Different lease ID before the current lease's ``refresh_after``:
          another process (a second pod or worker serving the same identity)
          is **joined** to the current lease. It receives credentials with
          the current deadline, so no caller can extend access early.
        * Different lease ID once the refresh window is open (or no lease
          exists): a new generation starts with a new deadline. Concurrent
          starters race on a conditional write; losers join the winner.
        """
        now = now or datetime.now(timezone.utc)
        validate_user_id(user_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("lease clock must be timezone-aware")
        if not lease_id or len(lease_id) > 128:
            raise ValueError("lease ID must contain 1 to 128 characters")
        self._consume_vend_rate(user_id, now)

        item = self._users.get_item(
            Key={"user_id": user_id}, ConsistentRead=True
        ).get("Item")
        if not item:
            raise KeyError(f"user {user_id!r} was not found")
        current = self._lease_from_item(item, created=False)
        if current and current.lease_id == lease_id:
            if now >= current.expires_at:
                raise LeaseExpired("logical lease has expired; use a new ID")
            return current
        if current and now < current.refresh_after:
            # Another process already holds this identity's lease. Join it
            # rather than refusing: the deadline stays the current one.
            return replace(current, joined=True)

        duration = lease_seconds or self._lease_seconds
        expires_at = now + timedelta(seconds=duration)
        if expires_no_later_than is not None:
            if (
                expires_no_later_than.tzinfo is None
                or expires_no_later_than.utcoffset() is None
            ):
                raise ValueError("lease expiration cap must be timezone-aware")
            expires_at = min(expires_at, expires_no_later_than)
        if expires_at <= now:
            raise LeaseExpired("logical lease deadline is not in the future")
        jitter = self._jitter(self._refresh_jitter_seconds)
        if not 0 <= jitter <= self._refresh_jitter_seconds:
            raise ValueError("lease jitter source returned an invalid value")
        refresh_after = max(
            now,
            expires_at
            - timedelta(seconds=self._refresh_overlap_seconds)
            + timedelta(seconds=jitter),
        )
        previous_generation = int(item.get("lease_generation", 0))
        generation = previous_generation + 1
        condition = "attribute_not_exists(lease_id)"
        if current is not None:
            condition = (
                "lease_refresh_after_epoch <= :now AND "
                "lease_generation = :previous_generation"
            )
        values = {
            ":lease_id": lease_id,
            ":generation": generation,
            ":expires": int(expires_at.timestamp()),
            ":refresh": int(refresh_after.timestamp()),
            ":updated": now.isoformat(),
            ":duration": int(duration),
        }
        if current is not None:
            values.update(
                {
                    ":now": int(now.timestamp()),
                    ":previous_generation": previous_generation,
                }
            )
        try:
            response = self._users.update_item(
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET lease_id = :lease_id, "
                    "lease_generation = :generation, "
                    "lease_expires_at_epoch = :expires, "
                    "lease_refresh_after_epoch = :refresh, "
                    "lease_updated_at = :updated, "
                    "lease_duration_seconds = :duration"
                ),
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if (
                exc.response.get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise
            winner = self.get_active_lease(user_id)
            if winner and winner.lease_id == lease_id:
                return winner
            if winner and now < winner.refresh_after:
                # A concurrent caller started the next generation first.
                return replace(winner, joined=True)
            raise LeaseNotRefreshable(
                winner.refresh_after if winner else now
            ) from exc
        reservation = self._lease_from_item(
            response["Attributes"], created=True
        )
        assert reservation is not None
        return reservation

    def get_emergency_state(self) -> dict:
        item = self._users.get_item(
            Key={"user_id": "CONFIG#EMERGENCY_STOP"},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return {
                "state": "inactive",
                "desired_active": False,
                "generation": 0,
                "actor": "",
                "reason": "",
            }
        # DynamoDB returns Number attributes as Decimal. Normalize at the
        # store boundary because this state is returned directly by the admin
        # API, including the idempotent activate/recover acknowledgement.
        return self._json_safe(dict(item))

    def get_auto_block_sweep_state(self) -> dict | None:
        """Last nightly auto-block sweep, as written by the sweeper Lambda.

        Operator visibility only (the console shows "did the job run and
        what did it do"); never an input to enforcement. ``None`` until the
        first real pass has completed.
        """
        item = self._users.get_item(
            Key={"user_id": "CONFIG#AUTO_BLOCK_SWEEP"},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return None
        state = self._json_safe(dict(item))
        state.pop("user_id", None)
        return state

    def get_enforcement_config(self) -> dict:
        """Runtime enforcement dial: the effective permission-lease window.

        Falls back to the deployment default when no runtime override has
        been written. Strongly consistent so a dial change applies to the
        very next vend.
        """
        item = self._users.get_item(
            Key={"user_id": "CONFIG#ENFORCEMENT"},
            ConsistentRead=True,
        ).get("Item")
        if not item:
            return {
                "permission_lease_seconds": settings.permission_lease_seconds,
                "source": "deployment_default",
                "generation": 0,
                "actor": "",
                "reason": "",
                "updated_at": None,
            }
        return {
            "permission_lease_seconds": int(
                item.get(
                    "permission_lease_seconds",
                    settings.permission_lease_seconds,
                )
            ),
            "source": "runtime",
            "generation": int(item.get("generation", 0)),
            "actor": str(item.get("actor", "")),
            "reason": str(item.get("reason", "")),
            "updated_at": (
                str(item["updated_at"]) if item.get("updated_at") else None
            ),
        }

    def effective_permission_lease_seconds(self) -> int:
        return int(
            self.get_enforcement_config()["permission_lease_seconds"]
        )

    def set_permission_lease_seconds(
        self,
        seconds: int,
        *,
        actor: str,
        reason: str,
        expected_generation: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Change the lease window at runtime (audited, no redeploy).

        Applies to NEW vends only: outstanding credentials keep the
        deadline they were issued with, and the revocation layer keeps
        cutting blocked identities regardless of the dial.
        """
        if seconds not in VALID_PERMISSION_LEASE_SECONDS:
            raise ValueError(
                "permission_lease_seconds must be one of "
                f"{sorted(VALID_PERMISSION_LEASE_SECONDS)}; got {seconds}"
            )
        floor = (
            settings.refresh_overlap_seconds
            + settings.refresh_jitter_seconds
        )
        if seconds <= floor:
            raise ValueError(
                "permission_lease_seconds must exceed refresh_overlap"
                f"+jitter ({floor}s) or leases could never refresh"
            )
        now = now or datetime.now(timezone.utc)
        current = self.get_enforcement_config()
        observed_generation = int(current.get("generation", 0))
        expected_generation = (
            observed_generation
            if expected_generation is None
            else expected_generation
        )
        idempotency_key = idempotency_key or str(uuid.uuid4())
        audit_key = f"CONFIG#ENFORCEMENT_AUDIT#{idempotency_key}"
        existing = self._users.get_item(
            Key={"user_id": audit_key}, ConsistentRead=True
        ).get("Item")
        if existing:
            if (
                int(existing.get("permission_lease_seconds", -1)) != seconds
                or str(existing.get("actor", "")) != actor
                or str(existing.get("reason", "")) != reason
            ):
                raise IdempotencyConflict("enforcement idempotency conflict")
            replay_result = self._json_safe(dict(existing["result"]))
            replay_result["_replayed"] = True
            return replay_result
        if expected_generation != observed_generation:
            raise EnforcementVersionConflict(current)
        generation = expected_generation + 1
        result = {
            "permission_lease_seconds": seconds,
            "source": "runtime",
            "generation": generation,
            "actor": actor,
            "reason": reason,
            "updated_at": now.isoformat(),
        }
        values = {
            ":seconds": seconds,
            ":generation": generation,
            ":actor": actor,
            ":reason": reason,
            ":updated": now.isoformat(),
            ":expected": expected_generation,
        }
        condition = (
            "attribute_not_exists(generation)"
            if expected_generation == 0
            else "generation = :expected"
        )
        if expected_generation == 0:
            values.pop(":expected")
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._users.name,
                            "Item": self._serialize(
                                {
                                    "user_id": audit_key,
                                    "action": "set_permission_lease_seconds",
                                    "permission_lease_seconds": seconds,
                                    "previous_permission_lease_seconds": int(
                                        current["permission_lease_seconds"]
                                    ),
                                    "actor": actor,
                                    "reason": reason,
                                    "generation": generation,
                                    "requested_at": now.isoformat(),
                                    "expires_at": window_ttl_epoch(now),
                                    "result": result,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(user_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._users.name,
                            "Key": self._serialize(
                                {"user_id": "CONFIG#ENFORCEMENT"}
                            ),
                            "UpdateExpression": (
                                "SET permission_lease_seconds = :seconds, "
                                "generation = :generation, actor = :actor, "
                                "reason = :reason, updated_at = :updated"
                            ),
                            "ConditionExpression": condition,
                            "ExpressionAttributeValues": self._serialize(values),
                        }
                    },
                ]
            )
        except ClientError:
            replay = self._users.get_item(
                Key={"user_id": audit_key}, ConsistentRead=True
            ).get("Item")
            if replay:
                if (
                    int(replay.get("permission_lease_seconds", -1)) == seconds
                    and str(replay.get("actor", "")) == actor
                    and str(replay.get("reason", "")) == reason
                ):
                    replay_result = self._json_safe(dict(replay["result"]))
                    replay_result["_replayed"] = True
                    return replay_result
                raise IdempotencyConflict("enforcement idempotency conflict")
            latest = self.get_enforcement_config()
            if int(latest.get("generation", 0)) != expected_generation:
                raise EnforcementVersionConflict(latest)
            raise
        return result

    def emergency_stop_active(self) -> bool:
        state = self.get_emergency_state()
        return bool(state.get("desired_active")) or state.get("state") != "inactive"

    def set_emergency_desired(
        self,
        *,
        active: bool,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(timezone.utc)
        current = self.get_emergency_state()
        generation = int(current.get("generation", 0)) + 1
        request_id = str(uuid.uuid4())
        state = "activating" if active else "recovering"
        item = {
            "user_id": "CONFIG#EMERGENCY_STOP",
            "state": state,
            "desired_active": active,
            "generation": generation,
            "actor": actor,
            "reason": reason,
            "request_id": request_id,
            "requested_at": now.isoformat(),
        }
        # Write the immutable audit row first. A later state-write failure can
        # leave a harmless unapplied request record, never an unaudited action.
        self._users.put_item(
            Item={
                "user_id": f"EMERGENCY_AUDIT#{now.isoformat()}#{request_id}",
                "action": "activate" if active else "recover",
                "actor": actor,
                "reason": reason,
                "generation": generation,
                "requested_at": now.isoformat(),
                "expires_at": window_ttl_epoch(now),
            }
        )
        self._users.put_item(Item=item)
        return dict(item)

    def mark_emergency_applied(
        self,
        *,
        active: bool,
        generation: int | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if generation is None:
            generation = int(self.get_emergency_state().get("generation", 0))
        self._users.update_item(
            Key={"user_id": "CONFIG#EMERGENCY_STOP"},
            UpdateExpression=(
                "SET #s = :s, applied_at = :t, "
                "applied_generation = :generation"
            ),
            ConditionExpression=(
                "desired_active = :desired AND generation = :generation"
            ),
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":s": "active" if active else "inactive",
                ":t": now.isoformat(),
                ":desired": active,
                ":generation": generation,
            },
        )

    def set_user_status(
        self,
        user_id: str,
        status: str,
        reason: str = "",
        *,
        origin: str,
        expected_version: int | None = None,
        expected_status: str | None = None,
        expected_reason: str | None = None,
    ) -> bool:
        """Write a status transition and advance the config version.

        ``origin`` names the owner of the new status: ``"automatic"`` for
        quota evaluation (may be lifted again by machines) or ``"admin"``
        for operator decisions (never lifted automatically). Callers can
        bind the write to their observed configuration. Session, lease,
        warning, and vend-rate bookkeeping deliberately use separate writes
        and do not change the user configuration version.
        """
        validate_user_id(user_id)
        if origin not in ("admin", "automatic"):
            raise ValueError("origin must be 'admin' or 'automatic'")
        changed_at = datetime.now(timezone.utc).isoformat()
        values = {
            ":s": status,
            ":r": reason,
            ":t": changed_at,
            ":origin": origin,
            ":zero": 0,
            ":one": 1,
        }
        conditions: list[str] = []
        if expected_version is not None:
            values[":expected_version"] = expected_version
            conditions.append(
                "(attribute_not_exists(#version) OR "
                "#version = :expected_version)"
                if expected_version == 0
                else "#version = :expected_version"
            )
        if expected_status is not None:
            values[":expected_status"] = expected_status
            conditions.append("#s = :expected_status")
        if expected_reason is not None:
            values[":expected_reason"] = expected_reason
            conditions.append(
                "(attribute_not_exists(status_reason) OR "
                "status_reason = :expected_reason)"
                if expected_reason == ""
                else "status_reason = :expected_reason"
            )
        observed = self._get_user_item(user_id) or {}
        update = {
            "TableName": self._users.name,
            "Key": self._serialize({"user_id": user_id}),
            "UpdateExpression": (
                "SET #s = :s, status_reason = :r, status_changed_at = :t, "
                "updated_at = :t, status_origin = :origin, "
                "#version = if_not_exists(#version, :zero) + :one"
            ),
            "ExpressionAttributeNames": {
                "#s": "status",
                "#version": "version",
            },
            "ExpressionAttributeValues": self._serialize(values),
        }
        if conditions:
            update["ConditionExpression"] = " AND ".join(conditions)
        revocation = {
            "user_id": f"REVOCATION#{user_id}",
            "maps_to": user_id,
            "desired_status": status,
            "source_identity": str(observed.get("source_identity", "")),
            "updated_at": changed_at,
            "expires_at": window_ttl_epoch(),
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"Update": update},
                    {
                        "Put": {
                            "TableName": self._users.name,
                            "Item": self._serialize(revocation),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            return False
        return True

    @staticmethod
    def _user_snapshot(user: UserRecord) -> dict:
        limits = user.period_limits
        snapshot = {
            "user_id": user.user_id,
            "name": user.name,
            "status": user.status,
            "status_reason": user.status_reason,
            "limits": limits,
            "rate": user.rate_limits,
            "version": user.version,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "status_origin": user.status_origin,
        }
        if user.model_budgets:
            snapshot["model_budgets"] = user.model_budget_limits
        return snapshot

    @staticmethod
    def _snapshot_to_user(snapshot: dict) -> UserRecord:
        limits = snapshot.get("limits", {})
        limit_item: dict[str, object] = {}
        for period in PERIODS:
            value = limits.get(period)
            limit_item[f"{period}_limits_enabled"] = value is not None
            limit_item[f"{period}_usd_micro"] = (
                int(value.get("usd_micro", 0)) if value else 0
            )
            limit_item[f"{period}_input_tokens"] = (
                int(value.get("input_tokens", 0)) if value else 0
            )
            limit_item[f"{period}_output_tokens"] = (
                int(value.get("output_tokens", 0)) if value else 0
            )
            if value and value.get("thresholds") is not None:
                limit_item[f"{period}_thresholds"] = value["thresholds"]
        rate = snapshot.get("rate") or {}
        item: dict = {
            "user_id": str(snapshot["user_id"]),
            "name": str(snapshot.get("name", "")),
            "status": str(snapshot.get("status", "active")),
            "status_reason": str(snapshot.get("status_reason", "")),
            **limit_item,
            **{
                dimension: int(rate.get(dimension, 0) or 0)
                for dimension in RATE_DIMENSIONS
            },
            "version": int(snapshot.get("version", 0)),
            "created_at": snapshot.get("created_at"),
            "updated_at": snapshot.get("updated_at"),
            "status_origin": str(snapshot.get("status_origin", "admin")),
        }
        model_budgets = snapshot.get("model_budgets")
        if isinstance(model_budgets, dict) and model_budgets:
            # Snapshot form is {model: {period: limits|None}}; rebuild the
            # flattened storage map so _to_user reads it like a live row.
            item[MODEL_BUDGETS_ATTRIBUTE] = {}
            for model_id, periods in model_budgets.items():
                attributes: dict[str, object] = {}
                for period in PERIODS:
                    value = (periods or {}).get(period)
                    attributes[f"{period}_limits_enabled"] = value is not None
                    attributes[f"{period}_usd_micro"] = (
                        int(value.get("usd_micro", 0)) if value else 0
                    )
                    attributes[f"{period}_input_tokens"] = (
                        int(value.get("input_tokens", 0)) if value else 0
                    )
                    attributes[f"{period}_output_tokens"] = (
                        int(value.get("output_tokens", 0)) if value else 0
                    )
                    attributes[f"{period}_thresholds"] = (
                        list(value.get("thresholds") or []) if value else []
                    )
                item[MODEL_BUDGETS_ATTRIBUTE][str(model_id)] = attributes
        return QuotaStore._to_user(item)

    def _idempotency_result(
        self, idempotency_key: str, request_hash: str
    ) -> AdminMutationResult | None:
        marker = self._admin_audit.get_item(
            Key={
                "subject_id": f"IDEMPOTENCY#{idempotency_key}",
                "event_key": IDEMPOTENCY_EVENT_KEY,
            },
            ConsistentRead=True,
        ).get("Item")
        if not marker:
            return None
        if marker.get("request_hash") != request_hash:
            raise IdempotencyConflict(
                "idempotency key was already used for a different request"
            )
        snapshot = marker.get("result_user")
        if not isinstance(snapshot, dict):
            raise IdempotencyConflict("idempotency marker is incomplete")
        return AdminMutationResult(
            user=self._snapshot_to_user(snapshot), replayed=True
        )

    def _admin_metadata_items(
        self,
        *,
        user: UserRecord,
        before: UserRecord | None,
        event_type: str,
        actor: str,
        auth_method: str,
        reason: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> list[dict]:
        created_at = now.isoformat()
        expires_at = int(now.timestamp()) + (
            settings.admin_audit_retention_days * 86400
        )
        event_key = f"{created_at}#{uuid.uuid4()}"
        audit_item = {
            "subject_id": user.user_id,
            "event_key": event_key,
            "scope": ADMIN_AUDIT_SCOPE,
            "event_type": event_type,
            "actor": actor,
            "auth_method": auth_method,
            "reason": reason,
            "request_id": idempotency_key,
            "created_at": created_at,
            "expires_at": expires_at,
            "before": self._user_snapshot(before) if before else None,
            "after": self._user_snapshot(user),
        }
        marker_item = {
            "subject_id": f"IDEMPOTENCY#{idempotency_key}",
            "event_key": IDEMPOTENCY_EVENT_KEY,
            "scope": "idempotency",
            "request_hash": request_hash,
            "result_user": self._user_snapshot(user),
            "created_at": created_at,
            "expires_at": expires_at,
        }
        return [
            {
                "Put": {
                    "TableName": self._admin_audit.name,
                    "Item": self._serialize(audit_item),
                    "ConditionExpression": "attribute_not_exists(subject_id)",
                }
            },
            {
                "Put": {
                    "TableName": self._admin_audit.name,
                    "Item": self._serialize(marker_item),
                    "ConditionExpression": "attribute_not_exists(subject_id)",
                }
            },
        ]

    def create_admin_user(
        self,
        *,
        user_id: str,
        name: str,
        limits: dict[str, dict[str, float | int] | None],
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        validate_user_id(user_id)
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        item = {
            "user_id": user_id,
            "name": name,
            "status": "active",
            "status_reason": "",
            **_limit_attributes(limits),
            "version": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "status_origin": "admin",
        }
        user = self._to_user(item)
        transaction = [
            {
                "Put": {
                    "TableName": self._users.name,
                    "Item": self._serialize(item),
                    "ConditionExpression": "attribute_not_exists(user_id)",
                }
            },
            *self._admin_metadata_items(
                user=user,
                before=None,
                event_type="user.created",
                actor=actor,
                auth_method=auth_method,
                reason="admin user creation",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            ),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            current = self.get_user(user_id)
            if current is not None:
                raise UserAlreadyExists(current) from exc
            raise
        return AdminMutationResult(user=user)

    @staticmethod
    def _version_condition(expected_version: int) -> str:
        if expected_version == 0:
            return (
                "attribute_exists(user_id) AND "
                "(attribute_not_exists(#version) OR #version = :expected)"
            )
        return "attribute_exists(user_id) AND #version = :expected"

    def update_admin_limits(
        self,
        user_id: str,
        limits: dict,
        *,
        reason: str,
        expected_version: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        reconciled_status: tuple[str, str, str] | None = None,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        current_item = self._get_user_item(user_id)
        if current_item is None:
            raise KeyError(user_id)
        current = self._to_user(current_item)
        now = now or datetime.now(timezone.utc)
        values = {
            ":expected": expected_version,
            ":next": expected_version + 1,
            ":updated": now.isoformat(),
        }
        sets = ["#version = :next", "updated_at = :updated"]
        next_item = dict(current_item)
        for attribute, value in _limit_attributes(limits).items():
            placeholder = f":{attribute}"
            values[placeholder] = value
            sets.append(f"{attribute} = {placeholder}")
            next_item[attribute] = value
        status_changed = False
        if reconciled_status is not None:
            desired_status, desired_reason, desired_origin = reconciled_status
            status_changed = (
                desired_status != current.status
                or desired_reason != current.status_reason
                or desired_origin != current.status_origin
            )
            if status_changed:
                values.update(
                    {
                        ":status": desired_status,
                        ":status_reason": desired_reason,
                        ":status_origin": desired_origin,
                    }
                )
                sets.extend(
                    [
                        "#status = :status",
                        "status_reason = :status_reason",
                        "status_origin = :status_origin",
                        "status_changed_at = :updated",
                    ]
                )
                next_item.update(
                    {
                        "status": desired_status,
                        "status_reason": desired_reason,
                        "status_origin": desired_origin,
                        "status_changed_at": now.isoformat(),
                    }
                )
        next_item.update(
            {"version": expected_version + 1, "updated_at": now.isoformat()}
        )
        updated = self._to_user(next_item)
        names = {"#version": "version"}
        if status_changed:
            names["#status"] = "status"
        transaction = [
            {
                "Update": {
                    "TableName": self._users.name,
                    "Key": self._serialize({"user_id": user_id}),
                    "UpdateExpression": "SET " + ", ".join(sets),
                    "ConditionExpression": self._version_condition(
                        expected_version
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": self._serialize(values),
                }
            },
        ]
        if status_changed:
            transaction.append(
                {
                    "Put": {
                        "TableName": self._users.name,
                        "Item": self._serialize(
                            {
                                "user_id": f"REVOCATION#{user_id}",
                                "maps_to": user_id,
                                "desired_status": updated.status,
                                "source_identity": str(
                                    current_item.get("source_identity", "")
                                ),
                                "updated_at": now.isoformat(),
                                "expires_at": window_ttl_epoch(now),
                            }
                        ),
                    }
                }
            )
        transaction.extend(
            self._admin_metadata_items(
                user=updated,
                before=current,
                event_type="user.limits.updated",
                actor=actor,
                auth_method=auth_method,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            )
        )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            latest = self.get_user(user_id)
            if latest is None:
                raise KeyError(user_id) from exc
            if latest.version != expected_version:
                raise VersionConflict(latest) from exc
            raise
        return AdminMutationResult(user=updated)

    def update_admin_model_budget(
        self,
        user_id: str,
        model_id: str,
        limits: dict | None,
        *,
        reason: str,
        expected_version: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        reconciled_status: tuple[str, str, str] | None = None,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        """Set (``limits``) or remove (``limits is None``) one model budget.

        Same versioned, idempotent, audited transaction as
        ``update_admin_limits``: the users row's ``model_budgets`` map is
        replaced wholesale under the observed version (DynamoDB map
        attributes are written atomically), the routine audit event carries
        before/after snapshots including every model budget, and an
        optional reconciled status transition rides in the same write.
        """
        model_id = validate_model_id(model_id)
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        current_item = self._get_user_item(user_id)
        if current_item is None:
            raise KeyError(user_id)
        current = self._to_user(current_item)
        now = now or datetime.now(timezone.utc)
        budgets = dict(current.model_budgets or {})
        if limits is None:
            if model_id not in budgets:
                raise KeyError(model_id)
            budgets.pop(model_id)
        else:
            budgets[model_id] = model_budget_attributes(limits)
        values: dict = {
            ":expected": expected_version,
            ":next": expected_version + 1,
            ":updated": now.isoformat(),
        }
        sets = ["#version = :next", "updated_at = :updated"]
        removes: list[str] = []
        next_item = dict(current_item)
        if budgets:
            values[":budgets"] = budgets
            sets.append("#budgets = :budgets")
            next_item[MODEL_BUDGETS_ATTRIBUTE] = budgets
        else:
            removes.append("#budgets")
            next_item.pop(MODEL_BUDGETS_ATTRIBUTE, None)
        status_changed = False
        if reconciled_status is not None:
            desired_status, desired_reason, desired_origin = reconciled_status
            status_changed = (
                desired_status != current.status
                or desired_reason != current.status_reason
                or desired_origin != current.status_origin
            )
            if status_changed:
                values.update(
                    {
                        ":status": desired_status,
                        ":status_reason": desired_reason,
                        ":status_origin": desired_origin,
                    }
                )
                sets.extend(
                    [
                        "#status = :status",
                        "status_reason = :status_reason",
                        "status_origin = :status_origin",
                        "status_changed_at = :updated",
                    ]
                )
                next_item.update(
                    {
                        "status": desired_status,
                        "status_reason": desired_reason,
                        "status_origin": desired_origin,
                        "status_changed_at": now.isoformat(),
                    }
                )
        next_item.update(
            {"version": expected_version + 1, "updated_at": now.isoformat()}
        )
        updated = self._to_user(next_item)
        names = {"#version": "version", "#budgets": MODEL_BUDGETS_ATTRIBUTE}
        if status_changed:
            names["#status"] = "status"
        update_expression = "SET " + ", ".join(sets)
        if removes:
            update_expression += " REMOVE " + ", ".join(removes)
        transaction = [
            {
                "Update": {
                    "TableName": self._users.name,
                    "Key": self._serialize({"user_id": user_id}),
                    "UpdateExpression": update_expression,
                    "ConditionExpression": self._version_condition(
                        expected_version
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": self._serialize(values),
                }
            },
        ]
        if status_changed:
            transaction.append(
                {
                    "Put": {
                        "TableName": self._users.name,
                        "Item": self._serialize(
                            {
                                "user_id": f"REVOCATION#{user_id}",
                                "maps_to": user_id,
                                "desired_status": updated.status,
                                "source_identity": str(
                                    current_item.get("source_identity", "")
                                ),
                                "updated_at": now.isoformat(),
                                "expires_at": window_ttl_epoch(now),
                            }
                        ),
                    }
                }
            )
        transaction.extend(
            self._admin_metadata_items(
                user=updated,
                before=current,
                event_type=(
                    "user.model_budget.removed"
                    if limits is None
                    else "user.model_budget.updated"
                ),
                actor=actor,
                auth_method=auth_method,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            )
        )
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            latest = self.get_user(user_id)
            if latest is None:
                raise KeyError(user_id) from exc
            if latest.version != expected_version:
                raise VersionConflict(latest) from exc
            raise
        return AdminMutationResult(user=updated)

    def update_admin_status(
        self,
        user_id: str,
        status: str,
        reason: str,
        *,
        expected_version: int,
        actor: str,
        auth_method: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> AdminMutationResult:
        replay = self._idempotency_result(idempotency_key, request_hash)
        if replay is not None:
            return replay
        current_item = self._get_user_item(user_id)
        if current_item is None:
            raise KeyError(user_id)
        current = self._to_user(current_item)
        now = now or datetime.now(timezone.utc)
        timestamp = now.isoformat()
        next_item = dict(current_item)
        next_item.update(
            {
                "status": status,
                "status_reason": reason,
                "status_changed_at": timestamp,
                "status_origin": "admin",
                "updated_at": timestamp,
                "version": expected_version + 1,
            }
        )
        updated = self._to_user(next_item)
        values = {
            ":status": status,
            ":reason": reason,
            ":updated": timestamp,
            ":origin": "admin",
            ":expected": expected_version,
            ":next": expected_version + 1,
        }
        revocation = {
            "user_id": f"REVOCATION#{user_id}",
            "maps_to": user_id,
            "desired_status": status,
            "source_identity": str(current_item.get("source_identity", "")),
            "updated_at": timestamp,
            "expires_at": window_ttl_epoch(now),
        }
        transaction = [
            {
                "Update": {
                    "TableName": self._users.name,
                    "Key": self._serialize({"user_id": user_id}),
                    "UpdateExpression": (
                        "SET #status = :status, status_reason = :reason, "
                        "status_changed_at = :updated, updated_at = :updated, "
                        "status_origin = :origin, #version = :next"
                    ),
                    "ConditionExpression": self._version_condition(
                        expected_version
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#version": "version",
                    },
                    "ExpressionAttributeValues": self._serialize(values),
                }
            },
            {
                "Put": {
                    "TableName": self._users.name,
                    "Item": self._serialize(revocation),
                }
            },
            *self._admin_metadata_items(
                user=updated,
                before=current,
                event_type="user.status.updated",
                actor=actor,
                auth_method=auth_method,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now,
            ),
        ]
        try:
            self._client.transact_write_items(TransactItems=transaction)
        except ClientError as exc:
            if not self._is_conditional_failure(exc):
                raise
            replay = self._idempotency_result(
                idempotency_key, request_hash
            )
            if replay is not None:
                return replay
            latest = self.get_user(user_id)
            if latest is None:
                raise KeyError(user_id) from exc
            if latest.version != expected_version:
                raise VersionConflict(latest) from exc
            raise
        return AdminMutationResult(user=updated)

    @staticmethod
    def _to_user(item: dict) -> UserRecord:
        def _thresholds(period: str) -> tuple[dict, ...] | None:
            stored = thresholds_from_storage(item.get(f"{period}_thresholds"))
            return tuple(stored) if stored is not None else None

        rate = rate_limits_from_item(item)
        return UserRecord(
            user_id=str(item["user_id"]),
            name=str(item.get("name", "")),
            status=str(item.get("status", "active")),
            status_reason=str(item.get("status_reason", "")),
            daily_usd_micro=int(item.get("daily_usd_micro", 0)),
            daily_input_tokens=int(item.get("daily_input_tokens", 0)),
            daily_output_tokens=int(item.get("daily_output_tokens", 0)),
            daily_limits_enabled=bool(
                item.get("daily_limits_enabled", True)
            ),
            weekly_usd_micro=int(item.get("weekly_usd_micro", 0)),
            weekly_input_tokens=int(item.get("weekly_input_tokens", 0)),
            weekly_output_tokens=int(item.get("weekly_output_tokens", 0)),
            weekly_limits_enabled=bool(
                item.get("weekly_limits_enabled", False)
            ),
            monthly_usd_micro=int(item.get("monthly_usd_micro", 0)),
            monthly_input_tokens=int(item.get("monthly_input_tokens", 0)),
            monthly_output_tokens=int(item.get("monthly_output_tokens", 0)),
            monthly_limits_enabled=bool(
                item.get("monthly_limits_enabled", False)
            ),
            version=int(item.get("version", 0)),
            created_at=(
                str(item["created_at"]) if item.get("created_at") else None
            ),
            updated_at=(
                str(item["updated_at"]) if item.get("updated_at") else None
            ),
            status_origin=str(item.get("status_origin", "admin")),
            lease_expires_at_epoch=(
                int(item["lease_expires_at_epoch"])
                if item.get("lease_expires_at_epoch") is not None
                else None
            ),
            lease_refresh_after_epoch=(
                int(item["lease_refresh_after_epoch"])
                if item.get("lease_refresh_after_epoch") is not None
                else None
            ),
            lease_generation=(
                int(item["lease_generation"])
                if item.get("lease_generation") is not None
                else None
            ),
            lease_duration_seconds=(
                int(item["lease_duration_seconds"])
                if item.get("lease_duration_seconds") is not None
                else None
            ),
            daily_thresholds=_thresholds("daily"),
            weekly_thresholds=_thresholds("weekly"),
            monthly_thresholds=_thresholds("monthly"),
            rpm=rate["rpm"],
            tpm=rate["tpm"],
            model_budgets=(
                {
                    str(model_id): dict(attributes)
                    for model_id, attributes in item[MODEL_BUDGETS_ATTRIBUTE].items()
                    if isinstance(attributes, dict)
                }
                if isinstance(item.get(MODEL_BUDGETS_ATTRIBUTE), dict)
                and item[MODEL_BUDGETS_ATTRIBUTE]
                else None
            ),
        )

    def get_window_usage(
        self, user_id: str, window: str | None = None
    ) -> dict:
        window = window or current_window()
        item = self._usage.get_item(
            Key={"user_id": user_id, "window": window},
            ConsistentRead=True,
        ).get("Item", {})
        return {
            "user_id": user_id,
            "window": window,
            "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
            "input_tokens": int(item.get("input_tokens", 0)),
            "output_tokens": int(item.get("output_tokens", 0)),
            "requests": int(item.get("requests", 0)),
            "cache_read_tokens": int(item.get("cache_read_tokens", 0)),
            "cache_write_tokens": int(item.get("cache_write_tokens", 0)),
            "images": int(item.get("images", 0)),
            "unpriced_requests": int(item.get("unpriced_requests", 0)),
            "missing_dimensions": sorted(
                str(value) for value in item.get("missing_dimensions", ())
            ),
        }

    def _daily_usage_rows(
        self, user_id: str, start: str, end: str
    ) -> list[dict]:
        response = self._usage.query(
            KeyConditionExpression=(
                "user_id = :user_id AND #window BETWEEN :start AND :end"
            ),
            ExpressionAttributeNames={"#window": "window"},
            ExpressionAttributeValues={
                ":user_id": user_id,
                ":start": start,
                ":end": end,
            },
            ConsistentRead=True,
        )
        rows = list(response.get("Items", []))
        while response.get("LastEvaluatedKey"):
            response = self._usage.query(
                KeyConditionExpression=(
                    "user_id = :user_id AND #window BETWEEN :start AND :end"
                ),
                ExpressionAttributeNames={"#window": "window"},
                ExpressionAttributeValues={
                    ":user_id": user_id,
                    ":start": start,
                    ":end": end,
                },
                ConsistentRead=True,
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            rows.extend(response.get("Items", []))
        return rows

    def get_current_usage(
        self, user_id: str, now: datetime | None = None
    ) -> dict[str, dict[str, object]]:
        windows = calendar_windows(now)
        start = min(window.start for window in windows.values())
        end = windows["daily"].start
        rows = self._daily_usage_rows(
            user_id, start.date().isoformat(), end.date().isoformat()
        )
        return aggregate_daily_rows(rows, now)

    def get_period_usage(
        self,
        user_id: str,
        period: str,
        window: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        bounds = (
            period_for_start(period, window)
            if window is not None
            else calendar_window(period, now)
        )
        end_date = bounds.end.date() - timedelta(days=1)
        rows = self._daily_usage_rows(
            user_id,
            bounds.start.date().isoformat(),
            end_date.isoformat(),
        )
        return aggregate_daily_rows(rows, bounds.start)[period]

    def get_rate_usage(
        self, user_id: str, now: datetime | None = None
    ) -> dict[str, object]:
        """Current-minute request/token counters written by the metering
        processor. Strongly consistent so a vend right after a burst sees
        the breach."""
        item = self._usage.get_item(
            Key=rate_row_key(user_id, now), ConsistentRead=True
        ).get("Item")
        return rate_usage_from_item(item, now)

    def get_model_usage(
        self, user_id: str, model_id: str, now: datetime | None = None
    ) -> dict[str, dict[str, object]]:
        """Current calendar totals for one subject × model ledger."""
        return self.get_current_usage(model_ledger_subject(user_id, model_id), now)

    def list_reconciliation_runs(self, limit: int = 14) -> list[dict]:
        """Stored daily reconciliation results, newest first.

        The reconciliation Lambda writes one ``RECONCILE#<day>`` row per run
        into the usage table (see reconciliation_processor/handler.py for why
        that table). The broker never calls Cost Explorer itself; it only
        reads these rows so the Operations page stays free of CE charges.
        """
        from boto3.dynamodb.conditions import Key

        prefix = "RECONCILE#"
        kwargs: dict = {
            "FilterExpression": Key("user_id").begins_with(prefix),
        }
        items: list[dict] = []
        while True:
            response = self._usage.scan(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        items.sort(key=lambda item: str(item.get("window", "")), reverse=True)
        runs = []
        for item in items[:limit]:
            result = self._json_safe(item.get("result", {}))
            runs.append(
                {
                    "day": str(item.get("window", "")),
                    "run_at": str(item.get("run_at", "")),
                    **(result if isinstance(result, dict) else {}),
                }
            )
        return runs

    def _model_budget_evaluation(
        self,
        user_id: str,
        budgets: dict[str, dict[str, dict | None]],
        now: datetime | None = None,
    ) -> QuotaEvaluation:
        if not budgets:
            return evaluate_model_budgets({}, {}, now)
        usage_by_model = {
            model_id: self.get_model_usage(user_id, model_id, now)
            for model_id in budgets
        }
        return evaluate_model_budgets(budgets, usage_by_model, now)

    def evaluate_user_quota(
        self, user: UserRecord, now: datetime | None = None
    ) -> QuotaEvaluation:
        usage = self.get_current_usage(user.user_id, now)
        rate_usage = (
            self.get_rate_usage(user.user_id, now) if user.rate_limited else None
        )
        return merge_evaluations(
            evaluate_limits(
                user.period_limits,
                usage,
                now,
                rate_limits=user.rate_limits,
                rate_usage=rate_usage,
            ),
            self._model_budget_evaluation(
                user.user_id, user.model_budget_limits, now
            ),
        )

    def status_after_limit_change(
        self,
        user: UserRecord,
        limits: dict[str, dict | None],
        now: datetime | None = None,
        *,
        model_budgets: dict[str, dict[str, dict | None]] | None = None,
    ) -> tuple[str, str, str]:
        attributes = _limit_attributes(limits)
        internal_limits = limits_from_item(attributes)
        rate_limits = (
            rate_limits_from_item(attributes)
            if "rate" in limits
            else user.rate_limits
        )
        evaluation = merge_evaluations(
            evaluate_limits(
                internal_limits,
                self.get_current_usage(user.user_id, now),
                now,
                rate_limits=rate_limits,
                rate_usage=(
                    self.get_rate_usage(user.user_id, now)
                    if rate_limits_enabled(rate_limits)
                    else None
                ),
            ),
            self._model_budget_evaluation(
                user.user_id,
                model_budgets
                if model_budgets is not None
                else user.model_budget_limits,
                now,
            ),
        )
        automatic = self._automatic_status_owned(user)
        if not user.active and not automatic:
            return user.status, user.status_reason, user.status_origin
        if evaluation.over_budget:
            return "blocked", quota_reason(evaluation), "automatic"
        if not user.active and automatic:
            return (
                "active",
                "auto: current calendar periods are under quota",
                "automatic",
            )
        return user.status, user.status_reason, user.status_origin

    def is_over_budget(self, user: UserRecord) -> bool:
        return self.evaluate_user_quota(user).over_budget

    @staticmethod
    def _automatic_status_owned(user: UserRecord) -> bool:
        return user.status_origin == "automatic"

    def refresh_auto_status(self, user: UserRecord) -> UserRecord:
        """Reactivate an owned automatic block after reset/limit increase."""
        current = user
        for _ in range(3):
            if (
                current.active
                or not self._automatic_status_owned(current)
                or self.is_over_budget(current)
            ):
                return current
            changed = self.set_user_status(
                current.user_id,
                "active",
                "auto: current calendar periods are under quota",
                origin="automatic",
                expected_version=current.version,
                expected_status=current.status,
                expected_reason=current.status_reason,
            )
            refreshed = self.get_user(current.user_id)
            assert refreshed is not None
            if changed:
                return refreshed
            current = refreshed
        return current

    @staticmethod
    def _is_sentinel(item: dict) -> bool:
        user_id = str(item.get("user_id", ""))
        return user_id.startswith(RESERVED_USER_ID_PREFIXES)

    def list_users(self) -> list[UserRecord]:
        users: list[UserRecord] = []
        response = self._users.scan()
        users.extend(
            self._to_user(item)
            for item in response.get("Items", [])
            if not self._is_sentinel(item)
        )
        while "LastEvaluatedKey" in response:
            response = self._users.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"]
            )
            users.extend(
                self._to_user(item)
                for item in response.get("Items", [])
                if not self._is_sentinel(item)
            )
        return users

    def list_users_page(
        self,
        limit: int = 50,
        cursor: str | None = None,
        *,
        status: str | None = None,
        query: str | None = None,
        granularity: str | None = None,
    ) -> tuple[list[UserRecord], str | None]:
        normalized_query = (query or "").strip().casefold()
        cursor_context = {
            "kind": "users",
            "status": status or "",
            "query": normalized_query,
            "granularity": granularity or "",
        }
        exclusive_key = self._decode_cursor(
            cursor,
            key_fields={"user_id"},
            context=cursor_context,
        )
        users: list[UserRecord] = []
        evaluated = 0
        max_evaluated = min(1000, max(limit * 10, 100))
        next_key = exclusive_key
        while len(users) < limit and evaluated < max_evaluated:
            chunk = min(max(limit - len(users), 1), max_evaluated - evaluated)
            scan_kwargs: dict = {"Limit": chunk}
            if next_key:
                scan_kwargs["ExclusiveStartKey"] = next_key
            try:
                response = self._users.scan(**scan_kwargs)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != (
                    "ValidationException"
                ):
                    raise
                raise ValueError("invalid cursor") from exc
            items = response.get("Items", [])
            evaluated += len(items)
            for item in items:
                if self._is_sentinel(item):
                    continue
                user = self._to_user(item)
                if status and user.status != status:
                    continue
                if granularity:
                    is_workload = user.user_id.startswith(
                        WORKLOAD_USER_ID_PREFIX
                    )
                    if granularity == "workload" and not is_workload:
                        continue
                    if granularity == "user" and is_workload:
                        continue
                if normalized_query and normalized_query not in (
                    f"{user.user_id}\n{user.name}".casefold()
                ):
                    continue
                users.append(user)
                if len(users) == limit:
                    break
            next_key = response.get("LastEvaluatedKey")
            if not next_key:
                break
        return users, self._encode_cursor(next_key, cursor_context)

    def get_usage_history_page(
        self,
        user_id: str,
        *,
        start: str,
        end: str,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        query_kwargs: dict = {
            "KeyConditionExpression": (
                "user_id = :user_id AND #window BETWEEN :start AND :end"
            ),
            "ExpressionAttributeNames": {"#window": "window"},
            "ExpressionAttributeValues": {
                ":user_id": user_id,
                ":start": start,
                ":end": end,
            },
            "ScanIndexForward": False,
            "Limit": limit,
        }
        cursor_context = {
            "kind": "usage-history",
            "user_id": user_id,
            "start": start,
            "end": end,
        }
        key = self._decode_cursor(
            cursor,
            key_fields={"user_id", "window"},
            context=cursor_context,
        )
        if key is not None:
            if (
                key["user_id"] != user_id
                or not start <= key["window"] <= end
            ):
                raise ValueError("invalid cursor")
            query_kwargs["ExclusiveStartKey"] = key
        try:
            response = self._usage.query(**query_kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ValidationException"
            ):
                raise
            raise ValueError("invalid cursor") from exc
        history = [
            {
                "user_id": user_id,
                "period": "daily",
                "window": str(item["window"]),
                "window_start": calendar_window(
                    "daily",
                    datetime.combine(
                        datetime.fromisoformat(str(item["window"])).date(),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                ).start.isoformat(),
                "window_end": calendar_window(
                    "daily",
                    datetime.combine(
                        datetime.fromisoformat(str(item["window"])).date(),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                ).end.isoformat(),
                "resets_at": calendar_window(
                    "daily",
                    datetime.combine(
                        datetime.fromisoformat(str(item["window"])).date(),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                ).end.isoformat(),
                "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
                "input_tokens": int(item.get("input_tokens", 0)),
                "output_tokens": int(item.get("output_tokens", 0)),
                "requests": int(item.get("requests", 0)),
            }
            for item in response.get("Items", [])
        ]
        last = response.get("LastEvaluatedKey")
        return history, self._encode_cursor(last, cursor_context)

    def get_period_usage_history_page(
        self,
        user_id: str,
        *,
        period: str,
        start: str,
        end: str,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        if period == "daily":
            return self.get_usage_history_page(
                user_id,
                start=start,
                end=end,
                limit=limit,
                cursor=cursor,
            )
        start_bounds = period_for_start(period, start)
        end_bounds = period_for_start(period, end)
        rows = self._daily_usage_rows(
            user_id,
            start_bounds.start.date().isoformat(),
            (end_bounds.end.date() - timedelta(days=1)).isoformat(),
        )
        grouped: dict[str, dict] = {}
        for item in rows:
            raw_window = str(item.get("window", ""))
            try:
                occurred = datetime.combine(
                    datetime.fromisoformat(raw_window).date(),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                continue
            bounds = calendar_window(period, occurred)
            group = grouped.setdefault(
                bounds.key,
                {
                    "user_id": user_id,
                    "period": period,
                    "window": bounds.key,
                    "window_start": bounds.start.isoformat(),
                    "window_end": bounds.end.isoformat(),
                    "resets_at": bounds.end.isoformat(),
                    "cost_micro": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "requests": 0,
                },
            )
            for field in (
                "cost_micro",
                "input_tokens",
                "output_tokens",
                "requests",
            ):
                group[field] += int(item.get(field, 0))
        ordered = [grouped[key] for key in sorted(grouped, reverse=True)]
        cursor_context = {
            "kind": "period-usage-history",
            "user_id": user_id,
            "period": period,
            "start": start,
            "end": end,
        }
        key = self._decode_cursor(
            cursor,
            key_fields={"window"},
            context=cursor_context,
        )
        offset = 0
        if key is not None:
            windows = [item["window"] for item in ordered]
            if key["window"] not in windows:
                raise ValueError("invalid cursor")
            offset = windows.index(key["window"]) + 1
        page = ordered[offset : offset + limit]
        has_more = offset + limit < len(ordered)
        next_cursor = self._encode_cursor(
            {"window": page[-1]["window"]}
            if page and has_more
            else None,
            cursor_context,
        )
        public = []
        for value in page:
            item = dict(value)
            cost_micro = int(item.pop("cost_micro"))
            public.append({**item, "cost_usd": cost_micro / MICRO})
        return public, next_cursor

    @staticmethod
    def _json_safe(value):
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {
                key: QuotaStore._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [QuotaStore._json_safe(item) for item in value]
        return value

    @staticmethod
    def _public_audit_event(item: dict) -> dict:
        before = item.get("before")
        after = item.get("after")
        return {
            "user_id": str(item.get("subject_id", "")),
            "event_key": str(item.get("event_key", "")),
            "event_type": str(item.get("event_type", "")),
            "actor": str(item.get("actor", "")),
            "auth_method": str(item.get("auth_method", "")),
            "reason": str(item.get("reason", "")),
            "request_id": str(item.get("request_id", "")),
            "created_at": str(item.get("created_at", "")),
            "before": (
                QuotaStore._user_snapshot(
                    QuotaStore._snapshot_to_user(before)
                )
                if isinstance(before, dict)
                else None
            ),
            "after": (
                QuotaStore._user_snapshot(
                    QuotaStore._snapshot_to_user(after)
                )
                if isinstance(after, dict)
                else None
            ),
        }

    def list_admin_audit_page(
        self,
        *,
        user_id: str | None,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        if user_id:
            query_kwargs: dict = {
                "KeyConditionExpression": "subject_id = :subject_id",
                "ExpressionAttributeValues": {":subject_id": user_id},
                "ScanIndexForward": False,
                "Limit": limit,
            }
        else:
            query_kwargs = {
                "IndexName": "scope-event-key-index",
                "KeyConditionExpression": "#scope = :scope",
                "ExpressionAttributeNames": {"#scope": "scope"},
                "ExpressionAttributeValues": {":scope": ADMIN_AUDIT_SCOPE},
                "ScanIndexForward": False,
                "Limit": limit,
            }
        cursor_context = {
            "kind": "admin-audit",
            "user_id": user_id or "",
        }
        key_fields = (
            {"subject_id", "event_key"}
            if user_id
            else {"subject_id", "event_key", "scope"}
        )
        key = self._decode_cursor(
            cursor,
            key_fields=key_fields,
            context=cursor_context,
        )
        if key is not None:
            if user_id and key["subject_id"] != user_id:
                raise ValueError("invalid cursor")
            if not user_id and key["scope"] != ADMIN_AUDIT_SCOPE:
                raise ValueError("invalid cursor")
            query_kwargs["ExclusiveStartKey"] = key
        try:
            response = self._admin_audit.query(**query_kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ValidationException"
            ):
                raise
            raise ValueError("invalid cursor") from exc
        events = [
            self._public_audit_event(item)
            for item in response.get("Items", [])
            if item.get("scope") == ADMIN_AUDIT_SCOPE
        ]
        last = response.get("LastEvaluatedKey")
        return events, self._encode_cursor(last, cursor_context)
