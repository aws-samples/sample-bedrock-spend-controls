"""Row-level block ownership and re-evaluation shared by the enforcers.

The workload enforcer (``workload:`` rows, 5-minute schedule) and the
auto-block sweeper (JWT user rows, nightly schedule) both answer the same
two questions the credential broker answers at vend time:

1. Is this ``blocked`` row *owned* by the automatic path, so a scheduled
   job may lift it? Admin-origin blocks are never touched by machines.
2. Is the subject still over budget in the **current** calendar periods,
   rate minute, and per-model budgets? The evaluation mirrors
   ``QuotaStore.evaluate_user_quota`` in the gateway: same ledger query
   range, same threshold defaults, same merge.

This module receives DynamoDB table resources as parameters and imports no
AWS SDK itself, keeping the layer free of boto so unit tests can drive it
with fakes.
"""

from __future__ import annotations

from datetime import datetime

from .quota_periods import (
    aggregate_daily_rows,
    calendar_windows,
    default_thresholds,
    evaluate_limits,
    evaluate_model_budgets,
    limits_from_item,
    merge_evaluations,
    model_budgets_from_item,
    model_ledger_subject,
    rate_limits_enabled,
    rate_limits_from_item,
    rate_row_key,
    rate_usage_from_item,
)

# Users-table key prefixes that are bookkeeping rows, never quota subjects.
# The gateway rejects them at provisioning time; the revocation processor
# and the sweeper filter their table scans with the same tuple.
RESERVED_USER_ID_PREFIXES = (
    "SESSION#",
    "VEND#",
    "REVOCATION#",
    "CONFIG#",
    "EMERGENCY_AUDIT#",
    "RATE#",
    "RECONCILE#",
)

# Public namespace for workload-mode quota subjects (apps calling
# bedrock-runtime with their own IAM credentials, attributed by application
# inference profile). Workload rows share the users table and admin API but
# never authenticate through the JWT vend path.
WORKLOAD_USER_ID_PREFIX = "workload:"

AUTO_LIFT_REASON = "auto: current calendar periods are under quota"


def automatic_owned(item: dict) -> bool:
    """True when the row's status was written by the automatic path.

    ``status_origin`` is ``"admin"`` or ``"automatic"``. A row without the
    attribute is treated as admin-owned: machines never lift a block they
    cannot prove they wrote.
    """
    return str(item.get("status_origin", "admin")) == "automatic"


def ledger_rows(usage_table, subject: str, start: str, end: str) -> list[dict]:
    response = usage_table.query(
        KeyConditionExpression=(
            "user_id = :user_id AND #window BETWEEN :start AND :end"
        ),
        ExpressionAttributeNames={"#window": "window"},
        ExpressionAttributeValues={
            ":user_id": subject,
            ":start": start,
            ":end": end,
        },
        ConsistentRead=True,
    )
    return list(response.get("Items", []))


def over_budget(
    usage_table, item: dict, now: datetime, *, warn_threshold: float
) -> bool:
    """Re-evaluate a row against its current windows (strongly consistent).

    ``warn_threshold`` is the deployment default used for rows and periods
    without their own thresholds list; callers read it from
    ``WARN_THRESHOLD`` so every component resolves defaults identically.
    """
    windows = calendar_windows(now)
    start = min(window.start for window in windows.values()).date().isoformat()
    end = windows["daily"].start.date().isoformat()
    user_id = str(item["user_id"])
    usage = aggregate_daily_rows(ledger_rows(usage_table, user_id, start, end), now)
    rate_limits = rate_limits_from_item(item)
    rate_usage = None
    if rate_limits_enabled(rate_limits):
        rate_item = usage_table.get_item(
            Key=rate_row_key(user_id, now), ConsistentRead=True
        ).get("Item")
        rate_usage = rate_usage_from_item(rate_item, now)
    defaults = default_thresholds(warn_threshold)
    limits = limits_from_item(item, default_thresholds_list=defaults)
    budgets = model_budgets_from_item(item, default_thresholds_list=defaults)
    usage_by_model = {
        model_id: aggregate_daily_rows(
            ledger_rows(
                usage_table, model_ledger_subject(user_id, model_id), start, end
            ),
            now,
        )
        for model_id in budgets
    }
    return merge_evaluations(
        evaluate_limits(
            limits, usage, now, rate_limits=rate_limits, rate_usage=rate_usage
        ),
        evaluate_model_budgets(budgets, usage_by_model, now),
    ).over_budget
