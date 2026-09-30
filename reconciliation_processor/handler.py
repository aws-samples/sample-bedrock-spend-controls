"""Daily reconciliation of the DynamoDB spend ledger against Cost Explorer.

The ledger's USD figures are estimates priced from the catalog; this Lambda
checks, once a day, that they track the actual bill. It is opt-in
(``reconciliation_enabled``), needs no CUR/Athena, and reads Cost Explorer
with ``ce:GetCostAndUsage`` for a single reconciled day ``D-<lag>`` (default
``D-2`` so CE has settled). Two comparisons:

* **Aggregate** — sum of every subject-level daily ledger row for that UTC
  day (users and workloads; per-model rows excluded to avoid double counting)
  vs Cost Explorer Bedrock spend filtered to the deployment Region and the
  Bedrock service names. Because the bill includes every principal in the
  account while the ledger holds only metered subjects, a positive
  billed-minus-estimated delta is expected wherever other principals call
  Bedrock; the runbook explains how to tell that apart from a pricing gap.
* **Per workload** — the ``workload:<name>`` daily row vs Cost Explorer
  filtered by the cost-allocation tag ``bedrock-spend-controls-workload=<name>``
  that the stack stamps on each application inference profile. This needs the
  tag *activated* in Billing → Cost allocation tags; when the ledger has spend
  but CE returns nothing for the tag, the Lambda emits
  ``ReconciliationTagInactive`` so the operator can tell the two apart.

Results are stored as ``RECONCILE#<date>`` rows in the usage table (TTL =
``usage_retention_days``) so ``GET /admin/reconciliation`` can list recent
runs without calling Cost Explorer ($0.01 per request) on every page load.
The usage table is the right home: the rows share its retention semantics,
its partition-key prefix convention (``REQUEST#``, ``RATE#``), and its
read grant on the broker; the admin-audit table is for operator actions.

JWT-vended user spend cannot be reconciled per user: every user shares one
role and therefore one line in the bill. Aggregate is the finest grain.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

MICRO = 1_000_000
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "BedrockSpendControls")
RECONCILE_PREFIX = "RECONCILE#"
# Cost Explorer SERVICE dimension values under which Bedrock usage bills,
# as returned by GetDimensionValues(SearchString="Bedrock"): on-demand model
# usage appears under BOTH names depending on the SKU, and Marketplace-billed
# third-party models (Anthropic) land under the same two services rather
# than a separate "Claude" value. Override with CE_SERVICE_NAMES_JSON if
# your bill differs.
DEFAULT_SERVICE_NAMES = ("Amazon Bedrock", "Amazon Bedrock Service")
WORKLOAD_TAG_KEY = os.environ.get("WORKLOAD_TAG_KEY", "bedrock-spend-controls-workload")
# Usage-table partition-key prefixes that are not subject ledgers.
_NON_SUBJECT_PREFIXES = ("REQUEST#", "RATE#", RECONCILE_PREFIX)
_MODEL_SEPARATOR = "#model#"


def _emit(metrics: dict[str, tuple[str, float]], properties: dict, dimensions: list[list[str]] | None = None) -> None:
    record = {
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1_000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": dimensions or [[]],
                    "Metrics": [
                        {"Name": name, "Unit": unit}
                        for name, (unit, _value) in metrics.items()
                    ],
                }
            ],
        },
        **properties,
        **{name: value for name, (_unit, value) in metrics.items()},
    }
    print(json.dumps(record, default=str))


def _notify(sns, subject: str, payload: dict) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if topic_arn:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=json.dumps(payload, indent=2, default=str),
        )


def _is_subject_ledger(user_id: str) -> bool:
    return (
        bool(user_id)
        and not user_id.startswith(_NON_SUBJECT_PREFIXES)
        and _MODEL_SEPARATOR not in user_id
    )


def ledger_usd_for_day(usage_table, day: str) -> tuple[float, dict[str, float]]:
    """(total USD across subject rows, {workload_id: USD}) for one UTC day.

    A scan filtered on ``window = <day>``: the usage table's key is
    (user_id, window), so there is no index that serves "every subject for a
    day". One scan per day is cheap at this table's size and runs once daily.
    """
    total_micro = 0
    workloads_micro: dict[str, int] = {}
    kwargs: dict[str, Any] = {
        "FilterExpression": Attr("window").eq(day) & Attr("cost_micro").exists(),
        "ProjectionExpression": "user_id, cost_micro",
    }
    while True:
        response = usage_table.scan(**kwargs)
        for item in response.get("Items", []):
            user_id = str(item.get("user_id", ""))
            if not _is_subject_ledger(user_id):
                continue
            micro = int(item.get("cost_micro", 0))
            total_micro += micro
            if user_id.startswith("workload:"):
                workloads_micro[user_id] = workloads_micro.get(user_id, 0) + micro
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return total_micro / MICRO, {k: v / MICRO for k, v in workloads_micro.items()}


def _ce_amount(response: dict) -> float:
    total = 0.0
    for period in response.get("ResultsByTime", []):
        # Without GroupBy, CE populates Total and leaves Groups empty; with
        # GroupBy, Total is {} and Groups carry the amounts. Prefer Total and
        # only fall back to Groups so a future GroupBy cannot double count.
        metric = period.get("Total", {}).get("UnblendedCost", {})
        if metric:
            total += float(metric.get("Amount", 0))
            continue
        for group in period.get("Groups", []):
            total += float(group["Metrics"]["UnblendedCost"]["Amount"])
    return total


def billed_usd_for_day(ce, day: str, region: str, service_names: tuple[str, ...]) -> float:
    start = date.fromisoformat(day)
    end = start + timedelta(days=1)
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": list(service_names), "MatchOptions": ["EQUALS"]}},
                {"Dimensions": {"Key": "REGION", "Values": [region], "MatchOptions": ["EQUALS"]}},
            ]
        },
    )
    return _ce_amount(response)


def billed_usd_for_workload(ce, day: str, region: str, service_names: tuple[str, ...], workload_name: str) -> float:
    start = date.fromisoformat(day)
    end = start + timedelta(days=1)
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": list(service_names), "MatchOptions": ["EQUALS"]}},
                {"Dimensions": {"Key": "REGION", "Values": [region], "MatchOptions": ["EQUALS"]}},
                {"Tags": {"Key": WORKLOAD_TAG_KEY, "Values": [workload_name], "MatchOptions": ["EQUALS"]}},
            ]
        },
    )
    return _ce_amount(response)


def _delta(estimated: float, billed: float) -> tuple[float, float | None]:
    """(billed - estimated, bounded percent) — None when both sides are zero.

    The percent is relative to the LARGER of the two figures, so it always
    lies in [-100, 100]: -100 means the bill saw nothing the ledger metered
    (credits, an inactive cost tag, or over-pricing), +100 means the ledger
    saw nothing the bill charged for (unmetered callers). The deploy-time
    ``reconciliation_alarm_percent`` cap of 100 assumes this bounded scale.
    """
    difference = billed - estimated
    scale = max(estimated, billed)
    if scale <= 0:
        return difference, None
    return difference, difference / scale * 100.0


def reconcile_day(*, usage_table, ce, day: str, region: str, workloads: dict[str, dict], service_names: tuple[str, ...]) -> dict:
    estimated, workload_estimates = ledger_usd_for_day(usage_table, day)
    billed = billed_usd_for_day(ce, day, region, service_names)
    difference, percent = _delta(estimated, billed)
    result: dict[str, Any] = {
        "day": day,
        "region": region,
        "service_names": list(service_names),
        "aggregate": {
            "estimated_usd": round(estimated, 6),
            "billed_usd": round(billed, 6),
            "delta_usd": round(difference, 6),
            "delta_percent": round(percent, 3) if percent is not None else None,
        },
        "workloads": [],
        "tag_inactive_workloads": [],
    }
    for workload_id in sorted(workloads):
        name = str(workloads[workload_id].get("name") or workload_id.split(":", 1)[-1])
        ledger = workload_estimates.get(workload_id, 0.0)
        tagged = billed_usd_for_workload(ce, day, region, service_names, name)
        w_delta, w_percent = _delta(ledger, tagged)
        # Ledger says the workload spent, CE says the tag saw nothing: the
        # cost-allocation tag is almost certainly not activated.
        tag_inactive = ledger > 0 and tagged == 0
        if tag_inactive:
            result["tag_inactive_workloads"].append(name)
        result["workloads"].append(
            {
                "workload_id": workload_id,
                "name": name,
                "estimated_usd": round(ledger, 6),
                "billed_usd": round(tagged, 6),
                "delta_usd": round(w_delta, 6),
                "delta_percent": round(w_percent, 3) if w_percent is not None else None,
                "tag_inactive": tag_inactive,
            }
        )
    return result


def _to_decimal(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_decimal(v) for v in value]
    return value


def store_result(usage_table, result: dict, *, retention_days: int, now: datetime) -> None:
    usage_table.put_item(
        Item={
            "user_id": f"{RECONCILE_PREFIX}{result['day']}",
            "window": result["day"],
            "run_at": now.isoformat(),
            "result": _to_decimal(result),
            "expires_at": int(now.timestamp()) + retention_days * 86400,
        }
    )


def _clients():
    return boto3.resource("dynamodb"), boto3.client("ce", region_name="us-east-1"), boto3.client("sns")


def handler(event, context, *, dynamodb=None, ce=None, sns=None) -> dict:
    del context
    if dynamodb is None or ce is None or sns is None:
        default_dynamodb, default_ce, default_sns = _clients()
        dynamodb = dynamodb or default_dynamodb
        ce = ce or default_ce
        sns = sns or default_sns
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    region = os.environ["RECONCILE_REGION"]
    lag_days = int(os.environ.get("RECONCILE_LAG_DAYS", "2"))
    retention_days = int(os.environ.get("USAGE_RETENTION_DAYS", "35"))
    service_names = tuple(json.loads(os.environ.get("CE_SERVICE_NAMES_JSON", json.dumps(list(DEFAULT_SERVICE_NAMES)))))
    workloads = json.loads(os.environ.get("WORKLOADS_JSON", "{}"))
    now = datetime.now(timezone.utc)
    day = str((event or {}).get("day") or (now.date() - timedelta(days=lag_days)).isoformat())

    try:
        result = reconcile_day(
            usage_table=usage_table, ce=ce, day=day, region=region,
            workloads=workloads, service_names=service_names,
        )
    except ClientError as exc:
        payload = {"day": day, "error": str(exc)}
        _emit({"ReconciliationFailure": ("Count", 1)}, payload)
        _notify(sns, "[bedrock-spend-controls] RECONCILIATION FAILED", payload)
        raise
    store_result(usage_table, result, retention_days=retention_days, now=now)

    aggregate = result["aggregate"]
    _emit(
        {
            "ReconciliationEstimatedUSD": ("None", aggregate["estimated_usd"]),
            "ReconciliationBilledUSD": ("None", aggregate["billed_usd"]),
            "ReconciliationDeltaUSD": ("None", aggregate["delta_usd"]),
            # Absolute percent so the alarm catches under- and over-estimates.
            "ReconciliationDeltaPercent": ("Percent", abs(aggregate["delta_percent"]) if aggregate["delta_percent"] is not None else 0.0),
            "ReconciliationRuns": ("Count", 1),
        },
        {"Day": day, "Scope": "aggregate", "SignedDeltaPercent": aggregate["delta_percent"]},
    )
    for workload in result["workloads"]:
        _emit(
            {
                "ReconciliationEstimatedUSD": ("None", workload["estimated_usd"]),
                "ReconciliationBilledUSD": ("None", workload["billed_usd"]),
                "ReconciliationDeltaUSD": ("None", workload["delta_usd"]),
                "ReconciliationDeltaPercent": ("Percent", abs(workload["delta_percent"]) if workload["delta_percent"] is not None else 0.0),
                "ReconciliationTagInactive": ("Count", 1 if workload["tag_inactive"] else 0),
            },
            {"Day": day, "Scope": "workload", "Workload": workload["name"], "SignedDeltaPercent": workload["delta_percent"]},
            dimensions=[["Workload"]],
        )
    if result["tag_inactive_workloads"]:
        _notify(
            sns,
            "[bedrock-spend-controls] RECONCILIATION: cost-allocation tag not active",
            {
                "day": day,
                "workloads": result["tag_inactive_workloads"],
                "tag_key": WORKLOAD_TAG_KEY,
                "action": (
                    "Activate the tag under Billing and Cost Management > Cost "
                    "allocation tags (aws ce update-cost-allocation-tags-status). "
                    "Per-workload reconciliation cannot work until it is active; "
                    "CE data for the tag starts flowing ~24 h after activation."
                ),
            },
        )
    return result
