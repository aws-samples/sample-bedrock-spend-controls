"""Nightly lift of automatic blocks for JWT users who never came back.

The credential broker lifts an automatic block lazily: at the next
``POST /v1/credentials`` it re-evaluates the current windows and, if the
subject is under quota again, flips the row to ``active``. A user who is
blocked and never asks again therefore stays ``blocked`` indefinitely, and
their ``source_identity`` keeps occupying a slot in the per-user IAM Deny
shards (~40-60 identities per shard). Enough of those and the shards
overflow, which silently stops *new* blocks from being enforced.

This function closes that gap. Once per night (00:05 UTC, right after the
daily/weekly/monthly windows roll over) it scans blocked user rows and, for
every row the automatic path owns whose current windows are under quota,
performs the same optimistic write the broker performs at vend: flip the
row to ``active`` and rewrite the ``REVOCATION#<user>`` sentinel with
``desired_status: active`` in one transaction. The sentinel Put is what the
users-table stream fast path keys on, so the revocation processor rebuilds
the Deny shards within seconds.

Rules the sweeper never bends:

* ``status_origin: admin`` rows are never touched; only ``automatic``
  blocks are candidates. A row with no ``status_origin`` counts as admin.
* The lift criterion is exactly the broker's: no breach across calendar
  periods, the current rate minute, and every per-model budget. There is no
  "block TTL". A subject that exhausted a monthly budget stays blocked until
  the month rolls over.
* Writes are conditional on the observed ``version``; a lost race is
  counted, not retried, because the winner already re-evaluated fresh state.
* ``workload:`` rows belong to the workload enforcer and are skipped.

Invoke with ``{"source": "aws.events"}`` (the schedule) or manually with
``{"source": "manual", "dry_run": true}`` to list candidates without writing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from bedrock_spend_controls.row_enforcement import (
    AUTO_LIFT_REASON,
    RESERVED_USER_ID_PREFIXES,
    WORKLOAD_USER_ID_PREFIX,
    automatic_owned,
    over_budget,
)

METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "BedrockSpendControls")
STATE_ROW_ID = "CONFIG#AUTO_BLOCK_SWEEP"
_SERIALIZER = TypeSerializer()


def _av(value: Any) -> dict:
    return _SERIALIZER.serialize(value)


def _ttl_epoch(now: datetime) -> int:
    keep_days = int(os.environ.get("USAGE_RETENTION_DAYS", "35"))
    return int(now.timestamp()) + keep_days * 86400


def _is_candidate_user(user_id: str) -> bool:
    return bool(user_id) and not user_id.startswith(
        RESERVED_USER_ID_PREFIXES + (WORKLOAD_USER_ID_PREFIX,)
    )


def _blocked_users(users_table) -> list[dict]:
    """All blocked JWT-user rows (consistent, paginated, server filtered)."""
    rows: list[dict] = []
    kwargs: dict[str, Any] = {
        "ConsistentRead": True,
        "FilterExpression": Attr("status").eq("blocked"),
    }
    while True:
        response = users_table.scan(**kwargs)
        for item in response.get("Items", []):
            if _is_candidate_user(str(item.get("user_id", ""))):
                rows.append(item)
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return sorted(rows, key=lambda item: str(item["user_id"]))


def _lift(client, users_table_name: str, item: dict, now: datetime) -> bool:
    """Flip an automatic block to active and rewrite its revocation sentinel.

    Mirrors ``QuotaStore.set_user_status`` at vend: the Update is
    conditional on the observed version, status and reason; the sentinel Put
    rides in the same transaction so the row and the Deny-shard desired
    state can never disagree. Returns False on a lost race.
    """
    user_id = str(item["user_id"])
    changed_at = now.isoformat()
    observed_version = int(item.get("version", 0) or 0)
    observed_reason = str(item.get("status_reason", ""))
    version_condition = (
        "(attribute_not_exists(#version) OR #version = :observed_version)"
        if observed_version == 0
        else "#version = :observed_version"
    )
    reason_condition = (
        "(attribute_not_exists(status_reason) OR "
        "status_reason = :observed_reason)"
        if observed_reason == ""
        else "status_reason = :observed_reason"
    )
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": users_table_name,
                        "Key": {"user_id": _av(user_id)},
                        "UpdateExpression": (
                            "SET #s = :active, status_reason = :reason, "
                            "status_changed_at = :t, updated_at = :t, "
                            "status_origin = :origin, "
                            "#version = if_not_exists(#version, :zero) + :one"
                        ),
                        "ConditionExpression": (
                            f"{version_condition} AND #s = :blocked AND "
                            f"{reason_condition}"
                        ),
                        "ExpressionAttributeNames": {
                            "#s": "status",
                            "#version": "version",
                        },
                        "ExpressionAttributeValues": {
                            ":active": _av("active"),
                            ":blocked": _av("blocked"),
                            ":reason": _av(AUTO_LIFT_REASON),
                            ":t": _av(changed_at),
                            ":origin": _av("automatic"),
                            ":zero": _av(0),
                            ":one": _av(1),
                            ":observed_version": _av(observed_version),
                            ":observed_reason": _av(observed_reason),
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": users_table_name,
                        "Item": {
                            "user_id": _av(f"REVOCATION#{user_id}"),
                            "maps_to": _av(user_id),
                            "desired_status": _av("active"),
                            "source_identity": _av(
                                str(item.get("source_identity", ""))
                            ),
                            "updated_at": _av(changed_at),
                            "expires_at": _av(_ttl_epoch(now)),
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }:
            return False
        raise
    return True


def _record_state(users_table, summary: dict) -> None:
    """Persist the last run so the admin console can show it ran.

    Plain PutItem, no version: the row is an operator-facing breadcrumb,
    never an input to enforcement. No TTL so "last ran" survives quiet
    periods.
    """
    users_table.put_item(Item={"user_id": STATE_ROW_ID, **summary})


def _notify(sns, subject: str, payload: dict) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if topic_arn:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=json.dumps(payload, indent=2, default=str),
        )


def _emit(metrics: dict[str, tuple[str, int]], properties: dict) -> None:
    record = {
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1_000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": [[]],
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


def _clients():
    # Transactions send TypeSerializer-encoded items and therefore need a
    # genuine low-level client. A boto3 *resource* meta client carries the
    # document-interface transform, which would re-serialize the already
    # encoded attribute values into nested maps and make DynamoDB reject the
    # UpdateExpression ("Incorrect operand type ... operand type: M").
    return boto3.resource("dynamodb"), boto3.client("dynamodb"), boto3.client("sns")


def handler(
    event, context, *, dynamodb=None, dynamodb_client=None, sns=None
) -> dict:
    del context
    event = event or {}
    if dynamodb is None or dynamodb_client is None or sns is None:
        default_dynamodb, default_client, default_sns = _clients()
        dynamodb = dynamodb or default_dynamodb
        dynamodb_client = dynamodb_client or default_client
        sns = sns or default_sns

    dry_run = bool(event.get("dry_run", False))
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    client = dynamodb_client
    warn_threshold = float(os.environ.get("WARN_THRESHOLD", "0.8"))
    now = datetime.now(timezone.utc)

    result: dict = {
        "ran_at": now.isoformat(),
        "dry_run": dry_run,
        "evaluated": 0,
        "lifted": 0,
        "still_blocked": 0,
        "admin_blocked": 0,
        "raced": 0,
        "lifted_users": [],
        "failures": [],
    }
    failures: list[dict] = []
    for item in _blocked_users(users_table):
        user_id = str(item["user_id"])
        result["evaluated"] += 1
        if not automatic_owned(item):
            result["admin_blocked"] += 1
            continue
        try:
            if over_budget(usage_table, item, now, warn_threshold=warn_threshold):
                result["still_blocked"] += 1
                continue
            if dry_run:
                result["lifted"] += 1
                result["lifted_users"].append(user_id)
                continue
            if _lift(client, users_table.name, item, now):
                result["lifted"] += 1
                result["lifted_users"].append(user_id)
            else:
                result["raced"] += 1
        except ClientError as exc:
            failures.append({"user_id": user_id, "error": str(exc)})

    result["failures"] = failures
    # The state row is operator visibility only; a dry run must not look
    # like a real pass in the console.
    if not dry_run:
        try:
            _record_state(users_table, result)
        except ClientError as exc:
            failures.append({"user_id": STATE_ROW_ID, "error": str(exc)})
            result["failures"] = failures

    if failures:
        _notify(sns, "[bedrock-spend-controls] AUTO-BLOCK SWEEP FAILED", result)
        _emit({"AutoBlockSweepFailure": ("Count", 1)}, result)
        raise RuntimeError(
            f"auto-block sweep failed for {len(failures)} row(s)"
        )

    _emit(
        {
            "AutoBlockSweepSuccess": ("Count", 1),
            "AutoBlockSweepEvaluated": ("Count", result["evaluated"]),
            "AutoBlockSweepLifted": ("Count", result["lifted"]),
            "AutoBlockSweepStillBlocked": ("Count", result["still_blocked"]),
            "AutoBlockSweepRaced": ("Count", result["raced"]),
        },
        result,
    )
    return result
