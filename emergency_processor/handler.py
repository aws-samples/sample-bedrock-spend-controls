"""Apply the operator-controlled role-wide Bedrock emergency deny policy."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

BEDROCK_ACTIONS = (
    "bedrock:CountTokens",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
_NO_EMERGENCY = "__emergency_stop_inactive__"
METRICS_NAMESPACE = os.environ.get(
    "METRICS_NAMESPACE", "BedrockSpendControls"
)


def _compact(document: dict) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def emergency_policy(active: bool) -> dict:
    statement = {
        "Sid": "EmergencyStopAllBedrockSessions",
        "Effect": "Deny",
        "Action": list(BEDROCK_ACTIONS),
        "Resource": "*",
    }
    if not active:
        statement["Condition"] = {
            "StringEquals": {"aws:SourceIdentity": [_NO_EMERGENCY]}
        }
    return {"Version": "2012-10-17", "Statement": [statement]}


def _decode(document):
    if isinstance(document, dict):
        return document
    return json.loads(unquote(document))


def _sync_policy(iam, policy_arn: str, document: dict) -> bool:
    policy = iam.get_policy(PolicyArn=policy_arn)["Policy"]
    current_id = policy["DefaultVersionId"]
    current = iam.get_policy_version(
        PolicyArn=policy_arn, VersionId=current_id
    )["PolicyVersion"]["Document"]
    if _compact(_decode(current)) == _compact(document):
        return False
    versions = iam.list_policy_versions(PolicyArn=policy_arn).get(
        "Versions", []
    )
    if len(versions) >= 5:
        removable = sorted(
            (
                version
                for version in versions
                if not version.get("IsDefaultVersion")
            ),
            key=lambda version: version.get(
                "CreateDate", datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
        if not removable:
            raise RuntimeError("emergency policy has no removable version")
        iam.delete_policy_version(
            PolicyArn=policy_arn,
            VersionId=removable[0]["VersionId"],
        )
    iam.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=_compact(document),
        SetAsDefault=True,
    )
    return True


def _notify(sns, subject: str, payload: dict) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if topic_arn:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=json.dumps(payload, indent=2, default=str),
        )


def _emit(metric: str, payload: dict) -> None:
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(
                        datetime.now(timezone.utc).timestamp() * 1_000
                    ),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRICS_NAMESPACE,
                            "Dimensions": [[]],
                            "Metrics": [{"Name": metric, "Unit": "Count"}],
                        }
                    ],
                },
                metric: 1,
                **payload,
            }
        )
    )


def _should_process(event: dict) -> bool:
    if event.get("source") == "aws.events":
        return True
    return any(
        record.get("dynamodb", {})
        .get("Keys", {})
        .get("user_id", {})
        .get("S")
        == "CONFIG#EMERGENCY_STOP"
        for record in event.get("Records", [])
    )


def handler(event, context, *, dynamodb=None, iam=None, sns=None) -> dict:
    del context
    if not _should_process(event):
        return {"applied": False, "reason": "no-emergency-change"}
    dynamodb = dynamodb or boto3.resource("dynamodb")
    iam = iam or boto3.client("iam")
    sns = sns or boto3.client("sns")
    table = dynamodb.Table(os.environ["USERS_TABLE"])
    policy_arn = os.environ["EMERGENCY_POLICY_ARN"]

    last_payload: dict = {}
    try:
        # Opposite requests can arrive while IAM is propagating. Re-read and
        # converge the newest generation before acknowledging this delivery.
        for _attempt in range(3):
            state = table.get_item(
                Key={"user_id": "CONFIG#EMERGENCY_STOP"},
                ConsistentRead=True,
            ).get("Item")
            if not state:
                return {"applied": False, "reason": "state-not-found"}
            desired_active = bool(state.get("desired_active"))
            generation = int(state.get("generation", 0))
            payload = {
                "desired_active": desired_active,
                "generation": generation,
                "request_id": state.get("request_id", ""),
                "actor": state.get("actor", ""),
                "reason": state.get("reason", ""),
            }
            last_payload = payload
            changed = _sync_policy(
                iam, policy_arn, emergency_policy(desired_active)
            )
            target_state = "active" if desired_active else "inactive"
            if (
                not changed
                and state.get("state") == target_state
                and int(state.get("applied_generation", -1)) == generation
            ):
                return {
                    **payload,
                    "applied": False,
                    "policy_changed": False,
                    "reason": "already-applied",
                }
            applied_at = datetime.now(timezone.utc).isoformat()
            try:
                table.update_item(
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
                        ":s": "active" if desired_active else "inactive",
                        ":t": applied_at,
                        ":desired": desired_active,
                        ":generation": generation,
                    },
                )
            except ClientError as exc:
                if (
                    exc.response.get("Error", {}).get("Code")
                    == "ConditionalCheckFailedException"
                ):
                    continue
                raise

            result = {**payload, "applied": True, "policy_changed": changed}
            subject = (
                "[bedrock-spend-controls] EMERGENCY STOP ACTIVE"
                if desired_active
                else "[bedrock-spend-controls] EMERGENCY STOP RECOVERED"
            )
            _notify(sns, subject, result)
            _emit(
                "EmergencyStopActivated"
                if desired_active
                else "EmergencyStopRecovered",
                result,
            )
            return result
        raise RuntimeError(
            "emergency desired state changed repeatedly during reconciliation"
        )
    except (ClientError, RuntimeError, ValueError) as exc:
        last_payload["error"] = str(exc)
        _notify(
            sns,
            "[bedrock-spend-controls] EMERGENCY STOP APPLY FAILED",
            last_payload,
        )
        _emit("EmergencyStopFailure", last_payload)
        raise
