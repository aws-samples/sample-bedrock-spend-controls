"""Fan out users-table stream events to the enforcement processors.

DynamoDB Streams supports at most two simultaneous consumers per shard
before read throttling. This dispatcher is the single enforcement consumer
(the emergency-stop processor is the other): it inspects the batch for
revocation sentinels (``REVOCATION#``) and workload rows (``workload:``)
and asynchronously invokes the corresponding processor. Both processors
are idempotent convergers with their own repair schedules, so a lost
dispatch is repaired within minutes; the dispatch only provides the fast
path.
"""

from __future__ import annotations

import json
import os

import boto3

DISPATCH_SOURCE = "enforcement-dispatch"

_lambda_client = None


def _client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def handler(event, context, *, lambda_client=None) -> dict:
    del context
    lambda_client = lambda_client or _client()
    revocation_change = False
    workload_change = False
    for record in event.get("Records", []):
        keys = record.get("dynamodb", {}).get("Keys", {})
        user_id = str(keys.get("user_id", {}).get("S", ""))
        if user_id.startswith("REVOCATION#"):
            revocation_change = True
        elif user_id.startswith("workload:"):
            workload_change = True

    payload = json.dumps({"source": DISPATCH_SOURCE}).encode("utf-8")
    dispatched: list[str] = []
    revocation_fn = os.environ.get("REVOCATION_FUNCTION_NAME", "")
    workload_fn = os.environ.get("WORKLOAD_ENFORCER_FUNCTION_NAME", "")
    if revocation_change and revocation_fn:
        lambda_client.invoke(
            FunctionName=revocation_fn,
            InvocationType="Event",
            Payload=payload,
        )
        dispatched.append("revocation")
    if workload_change and workload_fn:
        lambda_client.invoke(
            FunctionName=workload_fn,
            InvocationType="Event",
            Payload=payload,
        )
        dispatched.append("workload")
    return {
        "records": len(event.get("Records", [])),
        "dispatched": dispatched,
    }
