"""Auto-block sweeper tests: nightly lift of automatic JWT-user blocks.

The sweeper must behave exactly like the broker's ``refresh_auto_status``
at vend (same lift criterion, same conditional write, same ``REVOCATION#``
sentinel rewrite) while never touching admin blocks, workload rows, or
bookkeeping rows.
"""

import json
import os
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from auto_block_sweeper import handler as sweeper

SCHEDULE_EVENT = {"source": "aws.events"}
USERS = os.environ["USERS_TABLE"]
USAGE = os.environ["USAGE_TABLE"]


@pytest.fixture(autouse=True)
def _topic(monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "35")


def _seed_user(
    fake_dynamodb,
    user_id: str,
    *,
    status: str = "blocked",
    status_origin: str = "automatic",
    status_reason: str = "auto: daily USD quota exhausted in 2026-09-14",
    daily_usd_micro: int = 5_000_000,
    version: int = 3,
    source_identity: str | None = None,
    **extra,
) -> None:
    item = {
        "user_id": user_id,
        "status": status,
        "status_reason": status_reason,
        "status_origin": status_origin,
        "daily_usd_micro": daily_usd_micro,
        "daily_input_tokens": 0,
        "daily_output_tokens": 0,
        "version": version,
        "source_identity": source_identity or f"bsc-{user_id}",
        **extra,
    }
    fake_dynamodb.Table(USERS).put_item(Item=item)


def _seed_usage(fake_dynamodb, user_id: str, cost_micro: int, window=None):
    window = window or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fake_dynamodb.Table(USAGE).put_item(
        Item={
            "user_id": user_id,
            "window": window,
            "cost_micro": cost_micro,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 1,
        }
    )


def _row(fake_dynamodb, user_id: str) -> dict | None:
    return fake_dynamodb.Table(USERS).get_item(Key={"user_id": user_id}).get("Item")


def _run(fake_dynamodb, fake_sns, event=SCHEDULE_EVENT):
    # The fake's meta.client is a raw-client stand-in (TypeSerializer-encoded
    # items, like usage_processor's tests); production injects a genuine
    # boto3.client("dynamodb") for the same reason.
    return sweeper.handler(
        event,
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=fake_dynamodb.meta.client,
        sns=fake_sns,
    )


def test_automatic_block_under_quota_is_lifted_with_sentinel(
    fake_dynamodb, fake_sns
):
    _seed_user(fake_dynamodb, "alice")
    # Yesterday's ledger exhausted the budget; today's window is empty.

    result = _run(fake_dynamodb, fake_sns)

    assert result["evaluated"] == 1
    assert result["lifted"] == 1
    assert result["lifted_users"] == ["alice"]
    assert result["still_blocked"] == 0
    row = _row(fake_dynamodb, "alice")
    assert row["status"] == "active"
    assert row["status_origin"] == "automatic"
    assert row["status_reason"] == sweeper.AUTO_LIFT_REASON
    assert row["version"] == 4
    sentinel = _row(fake_dynamodb, "REVOCATION#alice")
    assert sentinel["desired_status"] == "active"
    assert sentinel["maps_to"] == "alice"
    assert sentinel["source_identity"] == "bsc-alice"
    assert sentinel["expires_at"] > int(datetime.now(timezone.utc).timestamp())
    assert fake_sns.published == []


def test_still_over_budget_today_stays_blocked(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "alice")
    _seed_usage(fake_dynamodb, "alice", cost_micro=6_000_000)

    result = _run(fake_dynamodb, fake_sns)

    assert result["lifted"] == 0
    assert result["still_blocked"] == 1
    assert _row(fake_dynamodb, "alice")["status"] == "blocked"
    assert _row(fake_dynamodb, "alice")["version"] == 3
    assert _row(fake_dynamodb, "REVOCATION#alice") is None


def test_daily_reset_does_not_lift_exhausted_monthly_budget(
    fake_dynamodb, fake_sns
):
    _seed_user(
        fake_dynamodb,
        "alice",
        status_reason="auto: monthly USD quota exhausted",
        monthly_limits_enabled=True,
        monthly_usd_micro=5_000_000,
        monthly_input_tokens=0,
        monthly_output_tokens=0,
    )
    first_of_month = datetime.now(timezone.utc).replace(day=1).date().isoformat()
    _seed_usage(fake_dynamodb, "alice", cost_micro=6_000_000, window=first_of_month)

    result = _run(fake_dynamodb, fake_sns)

    assert result["still_blocked"] == 1
    assert _row(fake_dynamodb, "alice")["status"] == "blocked"


def test_admin_block_is_never_touched(fake_dynamodb, fake_sns):
    _seed_user(
        fake_dynamodb,
        "alice",
        status_origin="admin",
        status_reason="incident freeze",
    )

    result = _run(fake_dynamodb, fake_sns)

    assert result["evaluated"] == 1
    assert result["admin_blocked"] == 1
    assert result["lifted"] == 0
    row = _row(fake_dynamodb, "alice")
    assert row["status"] == "blocked"
    assert row["status_origin"] == "admin"
    assert row["version"] == 3
    assert _row(fake_dynamodb, "REVOCATION#alice") is None


def test_row_without_status_origin_is_admin_owned_and_never_lifted(
    fake_dynamodb, fake_sns
):
    """Ownership comes from ``status_origin`` alone; an ``auto:`` reason on
    a row with no origin attribute does not make it a candidate."""
    fake_dynamodb.Table(USERS).put_item(
        Item={
            "user_id": "no-origin-user",
            "status": "blocked",
            "status_reason": "auto: daily quota exhausted in 2026-09-01",
            "daily_usd_micro": 5_000_000,
            "daily_input_tokens": 0,
            "daily_output_tokens": 0,
            # no status_origin, no version, no source_identity
        }
    )

    result = _run(fake_dynamodb, fake_sns)

    assert result["admin_blocked"] == 1
    assert result["lifted"] == 0
    assert _row(fake_dynamodb, "no-origin-user")["status"] == "blocked"
    assert _row(fake_dynamodb, "REVOCATION#no-origin-user") is None


def test_workload_and_bookkeeping_rows_are_skipped(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "workload:payments")
    _seed_user(fake_dynamodb, "REVOCATION#ghost")
    _seed_user(fake_dynamodb, "SESSION#abc")
    _seed_user(fake_dynamodb, "CONFIG#ENFORCEMENT")
    _seed_user(fake_dynamodb, "alice", status="active")
    _seed_user(fake_dynamodb, "bob")

    result = _run(fake_dynamodb, fake_sns)

    assert result["evaluated"] == 1
    assert result["lifted_users"] == ["bob"]
    assert _row(fake_dynamodb, "workload:payments")["status"] == "blocked"
    assert _row(fake_dynamodb, "workload:payments")["version"] == 3
    assert _row(fake_dynamodb, "alice")["version"] == 3


def test_lost_race_is_counted_not_retried_and_not_a_failure(
    fake_dynamodb, fake_sns, monkeypatch
):
    _seed_user(fake_dynamodb, "alice")
    users_table = fake_dynamodb.Table(USERS)
    original_scan = users_table.scan

    def scan_then_admin_blocks(**kwargs):
        response = original_scan(**kwargs)
        # An admin re-blocks alice between our read and our write.
        users_table.put_item(
            Item={
                **users_table.items[("alice",)],
                "status_origin": "admin",
                "status_reason": "hold",
                "version": 9,
            }
        )
        return response

    monkeypatch.setattr(users_table, "scan", scan_then_admin_blocks)

    result = _run(fake_dynamodb, fake_sns)

    assert result["raced"] == 1
    assert result["lifted"] == 0
    assert result["failures"] == []
    row = _row(fake_dynamodb, "alice")
    assert row["status"] == "blocked"
    assert row["status_origin"] == "admin"
    assert row["version"] == 9
    assert _row(fake_dynamodb, "REVOCATION#alice") is None
    assert fake_sns.published == []


def test_dry_run_reports_candidates_without_writing(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "alice")
    _seed_user(fake_dynamodb, "bob")
    _seed_usage(fake_dynamodb, "bob", cost_micro=6_000_000)

    result = _run(fake_dynamodb, fake_sns, {"source": "manual", "dry_run": True})

    assert result["dry_run"] is True
    assert result["lifted"] == 1
    assert result["lifted_users"] == ["alice"]
    assert result["still_blocked"] == 1
    assert _row(fake_dynamodb, "alice")["status"] == "blocked"
    assert _row(fake_dynamodb, "REVOCATION#alice") is None
    assert _row(fake_dynamodb, sweeper.STATE_ROW_ID) is None


def test_state_row_records_the_last_real_run(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "alice")
    _seed_user(fake_dynamodb, "bob")
    _seed_usage(fake_dynamodb, "bob", cost_micro=6_000_000)
    _seed_user(fake_dynamodb, "carol", status_origin="admin", status_reason="hold")

    result = _run(fake_dynamodb, fake_sns)

    state = _row(fake_dynamodb, sweeper.STATE_ROW_ID)
    assert state["ran_at"] == result["ran_at"]
    assert state["evaluated"] == 3
    assert state["lifted"] == 1
    assert state["still_blocked"] == 1
    assert state["admin_blocked"] == 1
    assert state["raced"] == 0
    assert state["dry_run"] is False
    assert state["failures"] == []
    assert "expires_at" not in state


def test_scan_pagination_is_followed(fake_dynamodb, fake_sns, monkeypatch):
    for index in range(5):
        _seed_user(fake_dynamodb, f"user-{index}")
    users_table = fake_dynamodb.Table(USERS)
    original_scan = users_table.scan
    pages: list[dict] = []

    def paged_scan(**kwargs):
        pages.append(kwargs)
        return original_scan(Limit=2, **kwargs)

    monkeypatch.setattr(users_table, "scan", paged_scan)

    result = _run(fake_dynamodb, fake_sns)

    assert result["lifted"] == 5
    assert len(pages) == 3
    assert "ExclusiveStartKey" in pages[1]


def test_unexpected_dynamodb_error_alerts_and_raises(
    fake_dynamodb, fake_sns, monkeypatch
):
    _seed_user(fake_dynamodb, "alice")

    def failing_transaction(**kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "slow down"}},
            "TransactWriteItems",
        )

    monkeypatch.setattr(
        fake_dynamodb.meta.client, "transact_write_items", failing_transaction
    )

    with pytest.raises(RuntimeError, match="1 row"):
        _run(fake_dynamodb, fake_sns)

    assert len(fake_sns.published) == 1
    assert "AUTO-BLOCK SWEEP FAILED" in fake_sns.published[0]["Subject"]
    message = json.loads(fake_sns.published[0]["Message"])
    assert message["failures"][0]["user_id"] == "alice"
    assert _row(fake_dynamodb, "alice")["status"] == "blocked"
    # The state row still records the failed pass so the console shows it.
    assert _row(fake_dynamodb, sweeper.STATE_ROW_ID)["failures"][0]["user_id"] == "alice"


def test_success_emits_emf_counters(fake_dynamodb, fake_sns, capsys):
    _seed_user(fake_dynamodb, "alice")

    _run(fake_dynamodb, fake_sns)

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    record = next(line for line in lines if "AutoBlockSweepSuccess" in line)
    names = {m["Name"] for m in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert names == {
        "AutoBlockSweepSuccess",
        "AutoBlockSweepEvaluated",
        "AutoBlockSweepLifted",
        "AutoBlockSweepStillBlocked",
        "AutoBlockSweepRaced",
    }
    assert record["AutoBlockSweepLifted"] == 1
    assert record["AutoBlockSweepEvaluated"] == 1


def test_production_wiring_uses_a_low_level_client_for_the_transaction(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Regression: a boto3 *resource* meta client re-serializes the already
    TypeSerializer-encoded transaction items (DynamoDB rejects the update
    with "operand type: M"). Without injected clients the handler must
    obtain a genuine boto3.client("dynamodb") and send the transaction
    through it, never through resource.meta.client."""
    _seed_user(fake_dynamodb, "alice")
    real_client = fake_dynamodb.meta.client
    calls: list[str] = []

    class LowLevelClient:
        def transact_write_items(self, **kwargs):
            calls.append("boto3.client")
            return real_client.transact_write_items(**kwargs)

    class ForbiddenMetaClient:
        def transact_write_items(self, **kwargs):
            raise AssertionError("resource meta client must not carry the transaction")

    monkeypatch.setattr(fake_dynamodb.meta, "client", ForbiddenMetaClient())
    monkeypatch.setattr(sweeper.boto3, "resource", lambda name: fake_dynamodb)
    monkeypatch.setattr(
        sweeper.boto3,
        "client",
        lambda name: LowLevelClient() if name == "dynamodb" else fake_sns,
    )

    result = sweeper.handler(SCHEDULE_EVENT, None)

    assert result["lifted"] == 1
    assert calls == ["boto3.client"]
    assert _row(fake_dynamodb, "alice")["status"] == "active"
