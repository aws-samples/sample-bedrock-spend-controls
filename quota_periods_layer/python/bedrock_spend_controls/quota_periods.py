"""Pure UTC calendar-period and quota evaluation helpers.

Daily DynamoDB rows remain the canonical usage ledger. Weekly and monthly
totals are derived from those retained rows so enabling a longer-period limit
mid-period includes usage that occurred before the limit was enabled.

Thresholds and rate limits
--------------------------
Each enabled period carries an ordered ``thresholds`` list of
``{"at_bps": int, "action": "warn" | "block"}``. ``at_bps`` is the
utilization in basis points (10_000 = 100 %), kept as an integer so the
comparison ``usage * 10_000 >= at_bps * limit`` is exact. At most one
``block`` entry is allowed and it must be last; a list with no ``block``
entry is an *alert-only* budget that warns but never blocks. Rows without a
stored thresholds list use the deployment default
``[{warn_threshold}, {100 % block}]`` until an operator configures one.

Rate limits (``rpm`` requests per minute, ``tpm`` uncached input + output
tokens per minute) are subject-level rather than per period. Usage is
counted per UTC minute; reaching either limit is a breach in the synthetic
``minute`` period, blocked through the same automatic path as a calendar
breach and lifted automatically once the current minute is under the limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping


PERIODS = ("daily", "weekly", "monthly")
DIMENSIONS = ("usd", "input_tokens", "output_tokens")
USAGE_FIELDS = {
    "usd": "cost_micro",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
}
# Every additive counter carried by a daily ledger row. The first four are
# the quota-bearing counters; the rest are priced usage dimensions that are
# reported but never limited (input_tokens deliberately excludes cached
# tokens: token quotas count uncached input and output tokens).
LEDGER_COUNTERS = (
    "cost_micro",
    "input_tokens",
    "output_tokens",
    "requests",
    "cache_read_tokens",
    "cache_write_tokens",
    "images",
    "unpriced_requests",
)
_DAILY_WINDOW = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MICRO = 1_000_000
BPS = 10_000
THRESHOLD_ACTIONS = ("warn", "block")
DEFAULT_WARN_RATIO = 0.8
MAX_THRESHOLD_RATIO = 10.0  # 1000 %
RATE_DIMENSIONS = ("rpm", "tpm")
RATE_PERIOD = "minute"
# Usage-table partition-key prefix for per-minute rate counters. The sort
# key is the UTC minute (``YYYY-MM-DDTHH:MM``). Kept in the usage table so
# the users-table stream (two consumers) never sees high-frequency writes.
RATE_ROW_PREFIX = "RATE#"


@dataclass(frozen=True)
class CalendarWindow:
    period: str
    start: datetime
    end: datetime

    @property
    def key(self) -> str:
        if self.period == RATE_PERIOD:
            return self.start.strftime("%Y-%m-%dT%H:%M")
        return self.start.date().isoformat()


@dataclass(frozen=True)
class QuotaBreach:
    period: str
    dimension: str
    usage: int
    limit: int
    window: CalendarWindow
    # Utilization at which this period blocks (basis points); 10_000 is the
    # default block ratio when no thresholds are stored. Present so
    # notifications can say "blocked at 150 %".
    at_bps: int = BPS
    # Set when the breach belongs to a model-scoped budget.
    model_id: str | None = None


@dataclass(frozen=True)
class ThresholdCrossing:
    """A warn threshold the period's peak utilization has reached."""

    period: str
    at_bps: int
    ratio: float
    window: CalendarWindow
    model_id: str | None = None

    @property
    def marker(self) -> str:
        """Users-table attribute recording the window this warning covered."""
        if self.model_id is not None:
            return (
                f"warning_sent_model_{self.model_id}_{self.period}_"
                f"{self.at_bps}_window"
            )
        return f"warning_sent_{self.period}_{self.at_bps}_window"


@dataclass(frozen=True)
class QuotaEvaluation:
    breaches: tuple[QuotaBreach, ...]
    ratios: dict[str, float]
    warnings: tuple[ThresholdCrossing, ...] = ()

    @property
    def over_budget(self) -> bool:
        return bool(self.breaches)

    @property
    def maximum_ratio(self) -> float:
        return max(self.ratios.values(), default=0.0)


def as_utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def calendar_window(period: str, value: datetime | None = None) -> CalendarWindow:
    current = as_utc(value)
    current_date = current.date()
    if period == RATE_PERIOD:
        start = current.replace(second=0, microsecond=0)
        return CalendarWindow(
            period=RATE_PERIOD, start=start, end=start + timedelta(minutes=1)
        )
    if period == "daily":
        start_date = current_date
        end_date = start_date + timedelta(days=1)
    elif period == "weekly":
        start_date = current_date - timedelta(days=current_date.weekday())
        end_date = start_date + timedelta(days=7)
    elif period == "monthly":
        start_date = current_date.replace(day=1)
        if start_date.month == 12:
            end_date = date(start_date.year + 1, 1, 1)
        else:
            end_date = date(start_date.year, start_date.month + 1, 1)
    else:
        raise ValueError(f"unsupported quota period: {period}")
    return CalendarWindow(
        period=period,
        start=datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        end=datetime.combine(end_date, time.min, tzinfo=timezone.utc),
    )


def calendar_windows(value: datetime | None = None) -> dict[str, CalendarWindow]:
    current = as_utc(value)
    return {period: calendar_window(period, current) for period in PERIODS}


def minute_window(value: datetime | None = None) -> CalendarWindow:
    return calendar_window(RATE_PERIOD, value)


def period_for_start(period: str, start: str) -> CalendarWindow:
    try:
        parsed = date.fromisoformat(start)
    except (TypeError, ValueError) as exc:
        raise ValueError("window must be an ISO date") from exc
    window = calendar_window(
        period, datetime.combine(parsed, time.min, tzinfo=timezone.utc)
    )
    if window.start.date() != parsed:
        raise ValueError(f"window is not a {period} period start")
    return window


def aggregate_daily_rows(
    rows: Iterable[Mapping[str, object]],
    value: datetime | None = None,
) -> dict[str, dict[str, object]]:
    windows = calendar_windows(value)
    totals: dict[str, dict[str, object]] = {
        period: {
            "period": period,
            "window": window.key,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "resets_at": window.end.isoformat(),
            **{counter: 0 for counter in LEDGER_COUNTERS},
        }
        for period, window in windows.items()
    }
    for row in rows:
        raw_window = str(row.get("window", ""))
        if not _DAILY_WINDOW.fullmatch(raw_window):
            continue
        try:
            row_date = date.fromisoformat(raw_window)
        except ValueError:
            continue
        for period, window in windows.items():
            if window.start.date() <= row_date < window.end.date():
                target = totals[period]
                for field in LEDGER_COUNTERS:
                    target[field] = int(target[field]) + int(row.get(field, 0))
    return totals


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def ratio_to_bps(ratio: float) -> int:
    return int(round(float(ratio) * BPS))


def default_thresholds(
    warn_ratio: float = DEFAULT_WARN_RATIO,
) -> list[dict[str, Any]]:
    """The deployment default: one warning, then block at 100 %."""
    return [
        {"at_bps": ratio_to_bps(warn_ratio), "action": "warn"},
        {"at_bps": BPS, "action": "block"},
    ]


def normalize_thresholds(
    raw: Any, *, name: str = "thresholds"
) -> list[dict[str, Any]]:
    """Validate an API/config thresholds list into storage form.

    Accepts entries with either ``at`` (utilization ratio, ``0 < at <= 10``)
    or ``at_bps`` (already in basis points). Enforces: non-empty list,
    strictly increasing ``at``, actions in {warn, block}, at most one
    ``block`` and only as the final entry. Returns ``[{at_bps, action}]``.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{name} must be a non-empty list")
    if len(raw) > 20:
        raise ValueError(f"{name} may contain at most 20 entries")
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        label = f"{name}[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{label} must be an object")
        unknown = sorted(set(entry) - {"at", "at_bps", "action"})
        if unknown:
            raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")
        if "at_bps" in entry:
            at_bps_raw = entry["at_bps"]
            if isinstance(at_bps_raw, bool) or not isinstance(
                at_bps_raw, (int, float)
            ) or int(at_bps_raw) != at_bps_raw:
                raise ValueError(f"{label}.at_bps must be an integer")
            at_bps = int(at_bps_raw)
        elif "at" in entry:
            at_raw = entry["at"]
            if isinstance(at_raw, bool) or not isinstance(at_raw, (int, float)):
                raise ValueError(f"{label}.at must be a number")
            at_bps = ratio_to_bps(at_raw)
        else:
            raise ValueError(f"{label} needs an 'at' utilization ratio")
        if not 0 < at_bps <= ratio_to_bps(MAX_THRESHOLD_RATIO):
            raise ValueError(
                f"{label}.at must be greater than 0 and at most "
                f"{MAX_THRESHOLD_RATIO:g} (1000 %)"
            )
        action = entry.get("action")
        if action not in THRESHOLD_ACTIONS:
            raise ValueError(
                f"{label}.action must be one of {', '.join(THRESHOLD_ACTIONS)}"
            )
        if result and at_bps <= result[-1]["at_bps"]:
            raise ValueError(f"{name} entries must be strictly increasing in at")
        if result and result[-1]["action"] == "block":
            raise ValueError(
                f"{name}: a 'block' entry must be the last threshold"
            )
        result.append({"at_bps": at_bps, "action": str(action)})
    return result


def thresholds_from_storage(raw: Any) -> list[dict[str, Any]] | None:
    """Read a stored thresholds list; None when absent or unusable."""
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return normalize_thresholds(
            [
                {"at_bps": int(entry["at_bps"]), "action": str(entry["action"])}
                for entry in raw
                if isinstance(entry, Mapping)
            ]
        )
    except (KeyError, TypeError, ValueError):
        return None


def block_threshold_bps(thresholds: Iterable[Mapping[str, Any]]) -> int | None:
    for entry in thresholds:
        if entry.get("action") == "block":
            return int(entry["at_bps"])
    return None


def warn_thresholds_bps(thresholds: Iterable[Mapping[str, Any]]) -> list[int]:
    return sorted(
        int(entry["at_bps"])
        for entry in thresholds
        if entry.get("action") == "warn"
    )


def thresholds_public(thresholds: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Storage form -> API form (``at`` as a ratio)."""
    return [
        {"at": int(entry["at_bps"]) / BPS, "action": str(entry["action"])}
        for entry in thresholds
    ]


# ---------------------------------------------------------------------------
# Limits: storage <-> evaluation shapes
# ---------------------------------------------------------------------------


def usd_to_micro(usd: float) -> int:
    # Admin/API values are decimal USD but arrive as binary floats. Round to
    # the nearest micro-dollar instead of truncating values such as 1.000044
    # to 1.000043 due to representation error.
    micro = int(round(float(usd) * MICRO))
    if usd > 0 and micro == 0:
        return 1
    return micro


def limit_attributes(
    limits: Mapping[str, Any],
    *,
    default_thresholds_list: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Flatten an API-shaped limits object into users-table attributes.

    ``limits`` maps each period to ``{usd, input_tokens, output_tokens,
    thresholds?}`` or ``None`` (disabled) and may carry ``rate``:
    ``{rpm, tpm}`` or ``None``. Thresholds omitted for an enabled period are
    materialized from ``default_thresholds_list`` (the deployment default)
    so every row written through this path stores its own list.
    """
    attributes: dict[str, object] = {}
    defaults = default_thresholds_list or default_thresholds()
    for period, value in limits.items():
        if period == "rate":
            continue
        if period not in PERIODS:
            raise ValueError(f"unsupported quota period: {period}")
        enabled = value is not None
        attributes[f"{period}_limits_enabled"] = enabled
        attributes[f"{period}_usd_micro"] = (
            usd_to_micro(float(value.get("usd", 0))) if value else 0
        )
        attributes[f"{period}_input_tokens"] = (
            int(value.get("input_tokens", 0)) if value else 0
        )
        attributes[f"{period}_output_tokens"] = (
            int(value.get("output_tokens", 0)) if value else 0
        )
        if value:
            raw_thresholds = value.get("thresholds")
            attributes[f"{period}_thresholds"] = (
                normalize_thresholds(raw_thresholds, name=f"{period}.thresholds")
                if raw_thresholds is not None
                else [dict(entry) for entry in defaults]
            )
        else:
            attributes[f"{period}_thresholds"] = []
    rate = limits.get("rate")
    if rate is not None:
        if not isinstance(rate, Mapping):
            raise ValueError("rate must be an object or null")
        for dimension in RATE_DIMENSIONS:
            attributes[dimension] = int(rate.get(dimension, 0))
    elif "rate" in limits:
        for dimension in RATE_DIMENSIONS:
            attributes[dimension] = 0
    return attributes


def limits_from_item(
    item: Mapping[str, object],
    *,
    default_thresholds_list: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Users-table item -> ``{period: {usd_micro, ..., thresholds} | None}``.

    Rows without a stored ``{period}_thresholds`` list use
    ``default_thresholds_list`` (the deployment default).
    """
    defaults = default_thresholds_list or default_thresholds()
    result: dict[str, dict[str, Any] | None] = {}
    for period in PERIODS:
        enabled_default = period == "daily"
        enabled = bool(item.get(f"{period}_limits_enabled", enabled_default))
        if not enabled:
            result[period] = None
            continue
        stored = thresholds_from_storage(item.get(f"{period}_thresholds"))
        result[period] = {
            "usd_micro": int(item.get(f"{period}_usd_micro", 0)),
            "input_tokens": int(item.get(f"{period}_input_tokens", 0)),
            "output_tokens": int(item.get(f"{period}_output_tokens", 0)),
            "thresholds": stored
            if stored is not None
            else [dict(entry) for entry in defaults],
        }
    return result


def rate_limits_from_item(item: Mapping[str, object]) -> dict[str, int]:
    return {
        dimension: max(int(item.get(dimension, 0) or 0), 0)
        for dimension in RATE_DIMENSIONS
    }


def rate_limits_enabled(rate_limits: Mapping[str, int] | None) -> bool:
    return bool(rate_limits) and any(
        int(rate_limits.get(dimension, 0)) > 0 for dimension in RATE_DIMENSIONS
    )


def rate_row_key(user_id: str, value: datetime | None = None) -> dict[str, str]:
    """Usage-table key of the per-minute counter row for ``user_id``."""
    return {
        "user_id": f"{RATE_ROW_PREFIX}{user_id}",
        "window": minute_window(value).key,
    }


def rate_usage_from_item(
    item: Mapping[str, object] | None, value: datetime | None = None
) -> dict[str, object]:
    """Normalize a per-minute counter row (or its absence) for evaluation."""
    window = minute_window(value)
    item = item or {}
    return {
        "period": RATE_PERIOD,
        "window": window.key,
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "resets_at": window.end.isoformat(),
        "requests": int(item.get("requests", 0) or 0),
        "tokens": int(item.get("tokens", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_limits(
    limits: Mapping[str, Mapping[str, Any] | None],
    usage: Mapping[str, Mapping[str, object]],
    value: datetime | None = None,
    *,
    rate_limits: Mapping[str, int] | None = None,
    rate_usage: Mapping[str, object] | None = None,
) -> QuotaEvaluation:
    """Compare usage with limits, honouring each period's thresholds.

    A dimension breaches when ``usage * 10_000 >= block_at_bps * limit``;
    periods whose thresholds carry no ``block`` never breach (alert-only).
    ``warnings`` lists, per period, every warn threshold the period's peak
    utilization has reached; the caller decides which were already sent.
    When ``rate_limits`` has a positive rpm/tpm and ``rate_usage`` is given,
    the current minute is evaluated as the synthetic ``minute`` period.
    """
    windows = calendar_windows(value)
    breaches: list[QuotaBreach] = []
    ratios: dict[str, float] = {}
    warnings: list[ThresholdCrossing] = []
    for period in PERIODS:
        period_limits = limits.get(period)
        if period_limits is None:
            continue
        thresholds = period_limits.get("thresholds") or default_thresholds()
        block_bps = block_threshold_bps(thresholds)
        period_usage = usage.get(period, {})
        peak = 0.0
        for dimension in DIMENSIONS:
            limit_key = "usd_micro" if dimension == "usd" else dimension
            limit = int(period_limits.get(limit_key, 0))
            if limit <= 0:
                continue
            current = int(period_usage.get(USAGE_FIELDS[dimension], 0))
            ratio = current / limit
            ratios[f"{period}.{dimension}"] = ratio
            peak = max(peak, ratio)
            if block_bps is not None and current * BPS >= block_bps * limit:
                breaches.append(
                    QuotaBreach(
                        period=period,
                        dimension=dimension,
                        usage=current,
                        limit=limit,
                        window=windows[period],
                        at_bps=block_bps,
                    )
                )
        for at_bps in warn_thresholds_bps(thresholds):
            if peak * BPS >= at_bps:
                warnings.append(
                    ThresholdCrossing(
                        period=period,
                        at_bps=at_bps,
                        ratio=peak,
                        window=windows[period],
                    )
                )
    if rate_limits_enabled(rate_limits) and rate_usage is not None:
        window = minute_window(value)
        counters = {"rpm": "requests", "tpm": "tokens"}
        for dimension in RATE_DIMENSIONS:
            limit = int(rate_limits.get(dimension, 0))  # type: ignore[union-attr]
            if limit <= 0:
                continue
            current = int(rate_usage.get(counters[dimension], 0) or 0)
            ratios[f"{RATE_PERIOD}.{dimension}"] = current / limit
            if current >= limit:
                breaches.append(
                    QuotaBreach(
                        period=RATE_PERIOD,
                        dimension=dimension,
                        usage=current,
                        limit=limit,
                        window=window,
                    )
                )
    order = (*PERIODS, RATE_PERIOD)
    dimension_order = (*DIMENSIONS, *RATE_DIMENSIONS)
    breaches.sort(
        key=lambda item: (
            order.index(item.period),
            dimension_order.index(item.dimension),
        )
    )
    return QuotaEvaluation(tuple(breaches), ratios, tuple(warnings))


def quota_reason(evaluation: QuotaEvaluation) -> str:
    if not evaluation.breaches:
        return ""
    first = evaluation.breaches[0]
    if first.period == RATE_PERIOD:
        return (
            f"auto: {first.dimension} rate limit reached "
            f"in minute {first.window.key}"
        )
    dimension = (
        "USD"
        if first.dimension == "usd"
        else first.dimension.replace("_", " ")
    )
    suffix = (
        "" if first.at_bps == BPS else f" at {first.at_bps / 100:g}%"
    )
    scope = f" for model {first.model_id}" if first.model_id else ""
    return (
        f"auto: {first.period} {dimension} quota exhausted{suffix}{scope} "
        f"in {first.window.key}"
    )


# ---------------------------------------------------------------------------
# Model-scoped budgets (optional second axis)
# ---------------------------------------------------------------------------
#
# A subject may carry, next to its subject-level limits, zero or more budgets
# keyed by model ID. They reuse the period/thresholds shape and are evaluated
# against per-model daily ledger rows written in the same transaction as the
# subject row. The enforcement primitives (SourceIdentity deny shards, role
# inline deny) are subject-wide, so ANY model budget breach blocks the whole
# subject; see README "Per-model budgets" for the rationale.

MODEL_BUDGETS_ATTRIBUTE = "model_budgets"
MODEL_LEDGER_SEPARATOR = "#model#"
MODEL_ID_MAX_LENGTH = 200


def model_ledger_subject(user_id: str, model_id: str) -> str:
    """Usage-table partition key of the per-model daily ledger for a subject.

    ``<user_id>#model#<model_id>`` keeps model rows adjacent to the subject
    row in the same table and query pattern (``BETWEEN`` on ``window``).
    Subject IDs cannot contain the separator because the vend path's
    reserved-prefix validation and the workload namespace never produce it.
    """
    return f"{user_id}{MODEL_LEDGER_SEPARATOR}{model_id}"


def validate_model_id(model_id: str) -> str:
    """A budget key is a Bedrock model or inference-profile *ID*, never an ARN."""
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty string")
    value = model_id.strip()
    if value.startswith("arn:"):
        raise ValueError(
            "model_id must be a model or inference-profile ID as it appears "
            "in the invocation log, not an ARN"
        )
    if MODEL_LEDGER_SEPARATOR in value or "/" in value:
        raise ValueError("model_id contains a reserved character sequence")
    if len(value) > MODEL_ID_MAX_LENGTH:
        raise ValueError(f"model_id may contain at most {MODEL_ID_MAX_LENGTH} characters")
    return value


def model_budgets_from_item(
    item: Mapping[str, object],
    *,
    default_thresholds_list: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, dict[str, Any] | None]]:
    """``{model_id: {period: {usd_micro, ..., thresholds} | None}}``.

    Storage form on the users row: ``model_budgets: {model_id: {<flattened
    period attributes as on the subject row>}}``. Malformed entries are
    skipped rather than failing metering.
    """
    raw = item.get(MODEL_BUDGETS_ATTRIBUTE)
    if not isinstance(raw, Mapping):
        return {}
    budgets: dict[str, dict[str, dict[str, Any] | None]] = {}
    for model_id, attributes in raw.items():
        if not isinstance(attributes, Mapping):
            continue
        try:
            budgets[str(model_id)] = limits_from_item(
                attributes, default_thresholds_list=default_thresholds_list
            )
        except (TypeError, ValueError):
            continue
    return budgets


def model_budget_attributes(
    limits: Mapping[str, Any],
    *,
    default_thresholds_list: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """Flatten one model budget (API shape) into its storage map.

    Same attribute names as the subject row so ``limits_from_item`` reads
    both; ``rate`` is not supported per model (rate limits are subject-level).
    """
    if "rate" in limits:
        raise ValueError("model budgets do not support rate limits")
    attributes = limit_attributes(
        limits, default_thresholds_list=default_thresholds_list
    )
    if not any(attributes.get(f"{period}_limits_enabled") for period in PERIODS):
        raise ValueError("a model budget must enable at least one period")
    return attributes


def evaluate_model_budgets(
    budgets: Mapping[str, Mapping[str, Mapping[str, Any] | None]],
    usage_by_model: Mapping[str, Mapping[str, Mapping[str, object]]],
    value: datetime | None = None,
) -> QuotaEvaluation:
    """Evaluate every model budget against that model's period usage.

    ``usage_by_model`` maps model_id -> ``aggregate_daily_rows`` output for
    the model's ledger. Models with a budget but no usage evaluate at zero.
    Breaches and warnings are tagged with their model_id and merged in
    model-ID order after the subject-level evaluation by the caller.
    """
    breaches: list[QuotaBreach] = []
    ratios: dict[str, float] = {}
    warnings: list[ThresholdCrossing] = []
    empty = aggregate_daily_rows((), value)
    for model_id in sorted(budgets):
        evaluation = evaluate_limits(
            budgets[model_id], usage_by_model.get(model_id, empty), value
        )
        for breach in evaluation.breaches:
            breaches.append(
                QuotaBreach(
                    period=breach.period,
                    dimension=breach.dimension,
                    usage=breach.usage,
                    limit=breach.limit,
                    window=breach.window,
                    at_bps=breach.at_bps,
                    model_id=model_id,
                )
            )
        for key, ratio in evaluation.ratios.items():
            ratios[f"model.{model_id}.{key}"] = ratio
        for crossing in evaluation.warnings:
            warnings.append(
                ThresholdCrossing(
                    period=crossing.period,
                    at_bps=crossing.at_bps,
                    ratio=crossing.ratio,
                    window=crossing.window,
                    model_id=model_id,
                )
            )
    return QuotaEvaluation(tuple(breaches), ratios, tuple(warnings))


def merge_evaluations(
    subject: QuotaEvaluation, models: QuotaEvaluation
) -> QuotaEvaluation:
    """Subject-level results first, then model-scoped, so ``quota_reason``
    names the subject breach when both exist."""
    return QuotaEvaluation(
        subject.breaches + models.breaches,
        {**subject.ratios, **models.ratios},
        subject.warnings + models.warnings,
    )
