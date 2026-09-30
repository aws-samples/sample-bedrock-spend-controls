"""Reconcile blocked quota identities into pre-attached IAM deny policies.

This Lambda is intentionally separate from usage accounting. The enforcement
dispatcher invokes it when a ``REVOCATION#`` status sentinel changes on the
users-table stream, and a schedule repairs missed or partially applied state.
IAM failure never changes the DynamoDB block: future vends remain denied while
existing sessions fall back to their permission/STS expiration.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from bedrock_spend_controls.row_enforcement import RESERVED_USER_ID_PREFIXES

BEDROCK_ACTIONS = (
    "bedrock:CountTokens",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
_NO_BLOCKED_IDENTITY = "__no_blocked_quota_identity__"
METRICS_NAMESPACE = os.environ.get(
    "METRICS_NAMESPACE", "BedrockSpendControls"
)


def _compact(document: dict) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def deny_policy(source_identities: list[str]) -> dict:
    identities = sorted(set(source_identities)) or [_NO_BLOCKED_IDENTITY]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyBlockedQuotaIdentities",
                "Effect": "Deny",
                "Action": list(BEDROCK_ACTIONS),
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceIdentity": identities}
                },
            }
        ],
    }


def shard_for(source_identity: str, shard_count: int) -> int:
    digest = hashlib.sha256(source_identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def _is_user(item: dict) -> bool:
    user_id = str(item.get("user_id", ""))
    return bool(user_id) and not user_id.startswith(RESERVED_USER_ID_PREFIXES)


def _blocked_identities(users_table) -> list[str]:
    identities: list[str] = []
    response = users_table.scan(ConsistentRead=True)
    while True:
        for item in response.get("Items", []):
            if (
                _is_user(item)
                and str(item.get("status", "active")) == "blocked"
                and item.get("source_identity")
            ):
                identities.append(str(item["source_identity"]))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        response = users_table.scan(
            ConsistentRead=True, ExclusiveStartKey=last
        )
    return sorted(set(identities))


def _decode_document(document: Any) -> dict:
    if isinstance(document, dict):
        return document
    if not isinstance(document, str):
        raise ValueError("IAM policy version returned an invalid document")
    return json.loads(unquote(document))


def _current_document(iam, policy_arn: str) -> dict:
    policy = iam.get_policy(PolicyArn=policy_arn)["Policy"]
    version = iam.get_policy_version(
        PolicyArn=policy_arn,
        VersionId=policy["DefaultVersionId"],
    )["PolicyVersion"]
    return _decode_document(version["Document"])


def _make_version_room(iam, policy_arn: str) -> None:
    versions = iam.list_policy_versions(PolicyArn=policy_arn).get(
        "Versions", []
    )
    if len(versions) < 5:
        return
    candidates = sorted(
        (version for version in versions if not version.get("IsDefaultVersion")),
        key=lambda version: version.get(
            "CreateDate", datetime.min.replace(tzinfo=timezone.utc)
        ),
    )
    if not candidates:
        raise RuntimeError("managed policy has no removable non-default version")
    iam.delete_policy_version(
        PolicyArn=policy_arn,
        VersionId=candidates[0]["VersionId"],
    )


def _sync_policy(iam, policy_arn: str, document: dict) -> bool:
    if _compact(_current_document(iam, policy_arn)) == _compact(document):
        return False
    _make_version_room(iam, policy_arn)
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


def _should_reconcile(event: dict) -> bool:
    if event.get("source") in ("aws.events", "enforcement-dispatch"):
        return True
    for record in event.get("Records", []):
        keys = record.get("dynamodb", {}).get("Keys", {})
        user_id = keys.get("user_id", {}).get("S", "")
        if str(user_id).startswith("REVOCATION#"):
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
    if not _should_reconcile(event):
        return {"reconciled": False, "reason": "no-status-change"}
    if dynamodb is None or iam is None or sns is None:
        default_dynamodb, default_iam, default_sns = _clients()
        dynamodb = dynamodb or default_dynamodb
        iam = iam or default_iam
        sns = sns or default_sns

    policy_arns = json.loads(os.environ["REVOCATION_POLICY_ARNS_JSON"])
    if not policy_arns:
        raise ValueError("at least one revocation policy ARN is required")
    max_characters = int(os.environ.get("REVOCATION_POLICY_MAX_CHARACTERS", "6144"))
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    identities = _blocked_identities(users_table)
    grouped: list[list[str]] = [[] for _ in policy_arns]
    for identity in identities:
        grouped[shard_for(identity, len(policy_arns))].append(identity)

    result = {
        "reconciled": True,
        "blocked_identities": len(identities),
        "updated_shards": 0,
        "unchanged_shards": 0,
        "overflow_shards": [],
        "shards": [],
    }
    try:
        for index, (policy_arn, shard_identities) in enumerate(
            zip(policy_arns, grouped, strict=True)
        ):
            desired = deny_policy(shard_identities)
            desired_characters = len(_compact(desired))
            overflow = desired_characters > max_characters
            if overflow:
                # Preserve the last known-good deny set. Replacing it with a
                # no-op would immediately restore every blocked identity in
                # this shard. New identities fall back to session expiry and
                # the capacity alarm until operators add qualified capacity.
                applied = _current_document(iam, policy_arn)
                changed = False
            else:
                applied = desired
                changed = _sync_policy(iam, policy_arn, applied)
            applied_characters = len(_compact(applied))
            result["updated_shards" if changed else "unchanged_shards"] += 1
            if overflow:
                result["overflow_shards"].append(index)
            result["shards"].append(
                {
                    "index": index,
                    "identities": len(shard_identities),
                    "desired_characters": desired_characters,
                    "applied_characters": applied_characters,
                    "overflow": overflow,
                    "changed": changed,
                }
            )
    except (ClientError, RuntimeError, ValueError) as exc:
        payload = {
            "error": str(exc),
            "blocked_identities": len(identities),
            "result": result,
        }
        _notify(sns, "[bedrock-spend-controls] REVOCATION SYNC FAILED", payload)
        _emit(
            {"RevocationSyncFailure": ("Count", 1)},
            payload,
        )
        raise

    overflow_count = len(result["overflow_shards"])
    if overflow_count:
        _notify(
            sns,
            "[bedrock-spend-controls] REVOCATION POLICY CAPACITY EXCEEDED",
            result,
        )
    _emit(
        {
            "RevocationSyncSuccess": ("Count", 1),
            "RevocationPolicyOverflow": ("Count", overflow_count),
            "RevokedIdentitiesDesired": (
                "Count",
                len(identities),
            ),
        },
        result,
    )
    return result
