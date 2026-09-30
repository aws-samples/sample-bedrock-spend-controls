"""Converge workload quota status onto per-role IAM inline Deny policies.

Workload mode meters apps that call bedrock-runtime directly with their own
IAM credentials (attributed by application-inference-profile ARN). There is
no credential vend to refuse, so enforcement is an IAM actuator: a blocked
``workload:<name>`` row gets an inline Deny on its configured role; an
active row gets the Deny removed. Both operations are idempotent
(PutRolePolicy is an upsert; DeleteRolePolicy tolerates absence), so the
users-table stream fast path and the repair schedule can both run safely.

IAM failure never changes the DynamoDB block: metering keeps accruing and
the next run retries. Workloads without a configured role_arn are metered
and alerted but cannot be hard-enforced; they surface as
``enforcement_ready: false`` in the admin API and as the
``WorkloadEnforcementSkipped`` metric here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from bedrock_spend_controls.row_enforcement import (
    AUTO_LIFT_REASON,
    automatic_owned,
    over_budget,
)

BEDROCK_ACTIONS = (
    "bedrock:CountTokens",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
DENY_SID = "BedrockQuotaWorkloadDeny"
METRICS_NAMESPACE = os.environ.get(
    "METRICS_NAMESPACE", "BedrockSpendControls"
)


def _compact(document: dict) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def deny_policy() -> dict:
    """The inline Deny document; identical for every workload."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": DENY_SID,
                "Effect": "Deny",
                "Action": list(BEDROCK_ACTIONS),
                "Resource": "*",
            }
        ],
    }


def _principal(arn: str) -> tuple[str, str]:
    """Split an IAM principal ARN into (kind, name).

    Supports roles and users (Bedrock long-term API keys attach to IAM
    users). Path segments are dropped: IAM APIs take the terminal name.
    """
    resource = arn.split(":", 5)[5]
    kind, _, remainder = resource.partition("/")
    if kind not in ("role", "user") or not remainder:
        raise ValueError(f"unsupported IAM principal ARN: {arn}")
    return kind, remainder.rsplit("/", 1)[-1]


def _get_attached(iam, kind: str, name: str, policy_name: str) -> dict | None:
    try:
        if kind == "role":
            response = iam.get_role_policy(
                RoleName=name, PolicyName=policy_name
            )
        else:
            response = iam.get_user_policy(
                UserName=name, PolicyName=policy_name
            )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return None
        raise
    document = response.get("PolicyDocument")
    if isinstance(document, str):
        document = json.loads(unquote(document))
    return document if isinstance(document, dict) else None


def _attach(iam, principal_arn: str, policy_name: str) -> bool:
    kind, name = _principal(principal_arn)
    desired = deny_policy()
    current = _get_attached(iam, kind, name, policy_name)
    if current is not None and _compact(current) == _compact(desired):
        return False
    if kind == "role":
        iam.put_role_policy(
            RoleName=name,
            PolicyName=policy_name,
            PolicyDocument=_compact(desired),
        )
    else:
        iam.put_user_policy(
            UserName=name,
            PolicyName=policy_name,
            PolicyDocument=_compact(desired),
        )
    return True


def _detach(iam, principal_arn: str, policy_name: str) -> bool:
    kind, name = _principal(principal_arn)
    try:
        if kind == "role":
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        else:
            iam.delete_user_policy(UserName=name, PolicyName=policy_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return False
        raise
    return True


def _over_budget(usage_table, item: dict, now: datetime) -> bool:
    return over_budget(
        usage_table,
        item,
        now,
        warn_threshold=float(os.environ.get("WARN_THRESHOLD", "0.8")),
    )


def _set_active(users_table, item: dict) -> bool:
    """Lift an automatic block whose window has reset (optimistic write).

    Mirrors the gateway's refresh_auto_status semantics; a conditional
    failure means someone else changed the row first, and the next run
    re-evaluates from fresh state. No ``REVOCATION#`` sentinel is written:
    the enforcement dispatcher already fans out on the ``workload:`` key
    prefix, and workload rows never have a ``source_identity`` for the
    per-user Deny shards.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        users_table.update_item(
            Key={"user_id": str(item["user_id"])},
            UpdateExpression=(
                "SET #status = :active, status_reason = :reason, "
                "status_origin = :origin, status_changed_at = :now, "
                "updated_at = :now, version = :next_version"
            ),
            ConditionExpression=(
                "#status = :blocked AND version = :observed_version"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":active": "active",
                ":blocked": "blocked",
                ":reason": AUTO_LIFT_REASON,
                ":origin": "automatic",
                ":now": now,
                ":observed_version": int(item.get("version", 0) or 0),
                ":next_version": int(item.get("version", 0) or 0) + 1,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return False
        raise
    return True


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
    print(json.dumps(record))


def _should_enforce(event: dict) -> bool:
    if event.get("source") in ("aws.events", "enforcement-dispatch"):
        return True
    for record in event.get("Records", []):
        keys = record.get("dynamodb", {}).get("Keys", {})
        user_id = keys.get("user_id", {}).get("S", "")
        if str(user_id).startswith("workload:"):
            return True
    return False


def _clients():
    return (
        boto3.resource("dynamodb"),
        boto3.client("iam"),
        boto3.client("sns"),
    )


def handler(event, context, *, dynamodb=None, iam=None, sns=None) -> dict:
    del context
    if not _should_enforce(event):
        return {"enforced": False, "reason": "no-workload-change"}
    if dynamodb is None or iam is None or sns is None:
        default_dynamodb, default_iam, default_sns = _clients()
        dynamodb = dynamodb or default_dynamodb
        iam = iam or default_iam
        sns = sns or default_sns

    workloads = json.loads(os.environ["WORKLOADS_JSON"])
    if not workloads:
        return {"enforced": False, "reason": "no-workloads-configured"}
    policy_name = os.environ.get(
        "DENY_POLICY_NAME", "bedrock-spend-controls-workload-deny"
    )
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    now = datetime.now(timezone.utc)

    result: dict = {
        "enforced": True,
        "attached": 0,
        "detached": 0,
        "unchanged": 0,
        "unblocked": 0,
        "skipped_not_ready": 0,
        "blocked_workloads": 0,
        "workloads": [],
    }
    failures: list[dict] = []
    for workload_id in sorted(workloads):
        configuration = workloads[workload_id]
        item = users_table.get_item(
            Key={"user_id": workload_id}, ConsistentRead=True
        ).get("Item")
        status = str(item.get("status", "active")) if item else "active"
        if (
            item is not None
            and status == "blocked"
            and automatic_owned(item)
            and not _over_budget(usage_table, item, now)
        ):
            if _set_active(users_table, item):
                status = "active"
                result["unblocked"] += 1
        desired_blocked = status == "blocked"
        if desired_blocked:
            result["blocked_workloads"] += 1
        principal_arn = str(configuration.get("role_arn") or "")
        entry = {
            "workload_id": workload_id,
            "status": status,
            "enforcement_ready": bool(principal_arn),
        }
        if not principal_arn:
            if desired_blocked:
                result["skipped_not_ready"] += 1
            result["workloads"].append(entry)
            continue
        try:
            if desired_blocked:
                changed = _attach(iam, principal_arn, policy_name)
                result["attached" if changed else "unchanged"] += 1
            else:
                changed = _detach(iam, principal_arn, policy_name)
                result["detached" if changed else "unchanged"] += 1
            entry["changed"] = changed
        except (ClientError, ValueError) as exc:
            entry["error"] = str(exc)
            failures.append(entry)
        result["workloads"].append(entry)

    if failures:
        payload = {"failures": failures, "result": result}
        _notify(
            sns, "[bedrock-spend-controls] WORKLOAD ENFORCEMENT FAILED", payload
        )
        _emit(
            {"WorkloadEnforcementFailure": ("Count", len(failures))},
            payload,
        )
        raise RuntimeError(
            f"workload enforcement failed for {len(failures)} workload(s)"
        )

    if result["skipped_not_ready"]:
        _notify(
            sns,
            "[bedrock-spend-controls] WORKLOAD BLOCKED WITHOUT ENFORCEMENT",
            result,
        )
    _emit(
        {
            "WorkloadEnforcementSuccess": ("Count", 1),
            "WorkloadDenyAttached": ("Count", result["attached"]),
            "WorkloadDenyDetached": ("Count", result["detached"]),
            "WorkloadEnforcementSkipped": (
                "Count",
                result["skipped_not_ready"],
            ),
            "WorkloadsBlocked": ("Count", result["blocked_workloads"]),
        },
        result,
    )
    return result
