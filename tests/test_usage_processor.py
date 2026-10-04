import base64
import copy
import gzip
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

import handler as processor

ROLE_NAME = "BedrockUserRole"
ROOT = Path(__file__).resolve().parents[1]
_DESERIALIZER = TypeDeserializer()


def _decode_map(values: dict) -> dict:
    return {key: _DESERIALIZER.deserialize(value) for key, value in values.items()}


class FakeDynamoClient:
    def __init__(self, resource):
        self.resource = resource

    def transact_write_items(self, TransactItems):  # noqa: N803
        snapshots = {
            name: copy.deepcopy(table.items)
            for name, table in self.resource.tables.items()
        }
        try:
            for action in TransactItems:
                if "Put" in action:
                    put = action["Put"]
                    table = self.resource.Table(put["TableName"])
                    item = _decode_map(put["Item"])
                    table.put_item(
                        Item=item,
                        ConditionExpression=put.get("ConditionExpression"),
                    )
                elif "Update" in action:
                    update = action["Update"]
                    self.resource.Table(update["TableName"]).update_item(
                        Key=_decode_map(update["Key"]),
                        UpdateExpression=update["UpdateExpression"],
                        ConditionExpression=update.get("ConditionExpression"),
                        ExpressionAttributeNames=update.get(
                            "ExpressionAttributeNames"
                        ),
                        ExpressionAttributeValues=_decode_map(
                            update.get("ExpressionAttributeValues")
                        ),
                    )
                else:
                    raise AssertionError("unsupported transaction action")
        except ClientError as exc:
            for name, items in snapshots.items():
                self.resource.tables[name].items = items
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": str(exc),
                    }
                },
                "TransactWriteItems",
            ) from exc

    def get_item(self, TableName, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True
        table = self.resource.Table(TableName)
        item = table.get_item(Key=_decode_map(Key)).get("Item")
        return {"Item": item} if item else {}


class TransientCancellationClient(FakeDynamoClient):
    """Cancels the first ``failures`` transactions (every one by default)."""

    def __init__(self, resource, failures: int | None = None):
        super().__init__(resource)
        self.failures = failures
        self.attempts = 0

    def transact_write_items(self, TransactItems):  # noqa: N803
        self.attempts += 1
        if self.failures is None or self.failures > 0:
            if self.failures is not None:
                self.failures -= 1
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "transaction conflict",
                    }
                },
                "TransactWriteItems",
            )
        return super().transact_write_items(TransactItems)


class RecordingClient(FakeDynamoClient):
    def __init__(self, resource):
        super().__init__(resource)
        self.transactions: list[list[dict]] = []

    def transact_write_items(self, TransactItems):  # noqa: N803
        self.transactions.append(TransactItems)
        return super().transact_write_items(TransactItems)


class FailingSNS:
    """Raises on the first ``failures`` publishes, then records them."""

    def __init__(self, failures: int):
        self.failures = failures
        self.published: list[dict] = []

    def publish(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("sns down")
        self.published.append(kwargs)
        return {"MessageId": "fake"}


def _freeze(monkeypatch, at: datetime):
    """Pin ``processor.datetime.now``; move it later via ``FrozenDateTime._now``."""

    class FrozenDateTime(datetime):
        _now = at

        @classmethod
        def now(cls, tz=None):
            return cls._now if tz is not None else cls._now.replace(tzinfo=None)

    monkeypatch.setattr(processor, "datetime", FrozenDateTime)
    return FrozenDateTime


def _subscription(records: list[dict]) -> dict:
    payload = {
        "messageType": "DATA_MESSAGE",
        "owner": "111122223333",
        "logGroup": "/aws/bedrock/modelinvocations",
        "logStream": "stream",
        "logEvents": records,
    }
    compressed = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(compressed).decode()}}


def _record(
    *,
    request_id="request-1",
    session="alice-session",
    model="openai.gpt-oss-20b",
    input_tokens=100,
    output_tokens=50,
    when: datetime | None = None,
    role=ROLE_NAME,
) -> dict:
    when = when or datetime.now(timezone.utc)
    message = {
        "schemaType": "ModelInvocationLog",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "requestId": request_id,
        "modelId": model,
        "identity": {
            "arn": (
                "arn:aws:sts::111122223333:"
                f"assumed-role/{role}/{session}"
            )
        },
        "input": {"inputTokenCount": input_tokens},
        "output": {"outputTokenCount": output_tokens},
    }
    return {
        "id": f"log-{request_id}",
        "timestamp": int(when.timestamp() * 1000),
        "message": json.dumps(message),
    }


def _seed_user(
    db,
    user_id: str,
    *,
    usd=1.0,
    in_limit=1000,
    out_limit=1000,
    **period_limits,
):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={
            "user_id": user_id,
            "name": user_id,
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": int(usd * processor.MICRO),
            "daily_input_tokens": in_limit,
            "daily_output_tokens": out_limit,
            **period_limits,
        }
    )


def _seed_session(db, session: str, user_id: str):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={"user_id": f"SESSION#{session}", "maps_to": user_id}
    )


def _run(event, db, sns):
    return processor.handler(
        event,
        None,
        dynamodb=db,
        dynamodb_client=FakeDynamoClient(db),
        sns=sns,
    )


def test_subscription_event_updates_daily_usage(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")

    result = _run(_subscription([_record()]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["requests"] == 1
    assert row["cost_micro"] == 200


def test_emf_reports_invocation_to_detection_lag(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    occurred_at = datetime.now(timezone.utc) - timedelta(seconds=2)

    _run(
        _subscription([_record(when=occurred_at)]),
        fake_dynamodb,
        fake_sns,
    )

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    emf = next(record for record in records if "_aws" in record)
    metric_names = {
        metric["Name"]
        for metric in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    assert "DetectionLagMilliseconds" in metric_names
    assert emf["DetectionLagMilliseconds"] >= 2_000
    assert emf["InvocationOccurredAt"] == occurred_at.isoformat()
    assert emf["ProcessedAt"]


def test_duplicate_delivery_is_idempotent(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    event = _subscription([_record()])

    assert _run(event, fake_dynamodb, fake_sns)["processed"] == 1
    duplicate = _run(event, fake_dynamodb, fake_sns)
    assert duplicate["duplicates"] == 1
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["requests"] == 1
    current = processor._current_usage(
        fake_dynamodb.Table(os.environ["USAGE_TABLE"]),
        "alice",
        datetime.now(timezone.utc),
    )
    assert {period: value["requests"] for period, value in current.items()} == {
        "daily": 1,
        "weekly": 1,
        "monthly": 1,
    }


def test_transient_transaction_cancellation_is_retried_not_dropped(
    fake_dynamodb, monkeypatch
):
    """A cancellation that never clears is raised (so the Logs subscription
    retries the batch) only after the in-function backoff is exhausted."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    pauses: list[float] = []
    monkeypatch.setattr(processor, "_sleep", pauses.append)
    usage = processor.InvocationUsage(
        request_id="retry-me",
        session_name="alice-session",
        model_id="openai.gpt-oss-20b",
        input_tokens=10,
        output_tokens=5,
        occurred_at=datetime.now(timezone.utc),
    )
    client = TransientCancellationClient(fake_dynamodb)

    with pytest.raises(ClientError, match="transaction conflict"):
        processor._apply_usage(
            client, os.environ["USAGE_TABLE"], "alice", usage, 1
        )

    assert client.attempts == len(processor._TRANSACTION_RETRY_BACKOFF_SECONDS) + 1
    assert pauses == list(processor._TRANSACTION_RETRY_BACKOFF_SECONDS)
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": usage.window}
    )
    assert "Item" not in row


def test_hot_row_conflict_is_retried_in_function_and_then_commits(
    fake_dynamodb, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    pauses: list[float] = []
    monkeypatch.setattr(processor, "_sleep", pauses.append)
    usage = processor.InvocationUsage(
        request_id="conflict-then-commit",
        session_name="alice-session",
        model_id="openai.gpt-oss-20b",
        input_tokens=10,
        output_tokens=5,
        occurred_at=datetime.now(timezone.utc),
    )
    client = TransientCancellationClient(fake_dynamodb, failures=2)

    applied = processor._apply_usage(
        client, os.environ["USAGE_TABLE"], "alice", usage, 7
    )

    assert applied is True
    assert client.attempts == 3
    assert pauses == [0.05, 0.2]
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": usage.window}
    )["Item"]
    assert row["requests"] == 1
    assert row["cost_micro"] == 7
    # A duplicate that arrives while the conflict clears is still detected
    # on the retry path (marker read between attempts), never double-charged.
    assert (
        processor._apply_usage(
            TransientCancellationClient(fake_dynamodb, failures=1),
            os.environ["USAGE_TABLE"],
            "alice",
            usage,
            7,
        )
        is False
    )


def test_unknown_session_and_other_role_are_not_metered(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    event = _subscription(
        [
            _record(session="unknown"),
            _record(request_id="other", role="DifferentRole"),
        ]
    )
    result = _run(event, fake_dynamodb, fake_sns)
    assert result["unresolved_sessions"] == ["unknown"]
    assert result["ignored"] == 1
    assert result["processed"] == 0


def test_processor_warns_once_then_blocks_at_any_limit(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:test")
    monkeypatch.setenv("WARN_THRESHOLD", "0.8")
    _seed_user(fake_dynamodb, "alice", in_limit=100, out_limit=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    first = _record(request_id="first", input_tokens=80, output_tokens=0)
    _run(_subscription([first]), fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1
    assert "WARNING" in fake_sns.published[0]["Subject"]

    duplicate_warning = _record(
        request_id="second", input_tokens=1, output_tokens=0
    )
    _run(_subscription([duplicate_warning]), fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1

    blocking = _record(
        request_id="third", input_tokens=19, output_tokens=0
    )
    _run(_subscription([blocking]), fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"].startswith("auto:")
    assert user["version"] == 1
    assert user["status_origin"] == "automatic"
    assert user["updated_at"]
    revocation_event = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert revocation_event["desired_status"] == "blocked"
    assert len(fake_sns.published) == 2
    assert "BLOCKED" in fake_sns.published[1]["Subject"]


def test_historical_late_log_is_metered_but_does_not_block_current_day(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", in_limit=10)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    event = _subscription(
        [_record(when=yesterday, input_tokens=100, output_tokens=0)]
    )
    _run(event, fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "active"
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": yesterday.strftime("%Y-%m-%d")}
    )["Item"]
    assert row["input_tokens"] == 100


def test_late_daily_log_can_exhaust_current_weekly_quota(
    fake_dynamodb, fake_sns, monkeypatch
):
    fixed = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)  # Wednesday

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(processor, "datetime", FrozenDateTime)
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(
        fake_dynamodb,
        "alice",
        in_limit=1_000,
        weekly_limits_enabled=True,
        weekly_usd_micro=0,
        weekly_input_tokens=150,
        weekly_output_tokens=0,
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")
    fake_dynamodb.Table(os.environ["USAGE_TABLE"]).put_item(
        Item={
            "user_id": "alice",
            "window": "2026-09-09",
            "input_tokens": 60,
        }
    )

    _run(
        _subscription(
            [
                _record(
                    request_id="late-weekly",
                    when=datetime(2026, 9, 8, 23, tzinfo=timezone.utc),
                    input_tokens=100,
                    output_tokens=0,
                )
            ]
        ),
        fake_dynamodb,
        fake_sns,
    )

    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"] == (
        "auto: weekly input tokens quota exhausted in 2026-09-07"
    )


def test_warning_threshold_is_tracked_per_calendar_period(
    fake_dynamodb, fake_sns, monkeypatch
):
    fixed = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(processor, "datetime", FrozenDateTime)
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("WARN_THRESHOLD", "0.8")
    _seed_user(
        fake_dynamodb,
        "alice",
        in_limit=1_000,
        weekly_limits_enabled=True,
        weekly_usd_micro=0,
        weekly_input_tokens=100,
        weekly_output_tokens=0,
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([_record(request_id="weekly-warning", when=fixed, input_tokens=80, output_tokens=0)]),
        fake_dynamodb,
        fake_sns,
    )

    assert len(fake_sns.published) == 1
    payload = json.loads(fake_sns.published[0]["Message"])
    assert payload["period"] == "weekly"
    assert payload["threshold"] == 0.8
    row = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    # Markers are per threshold (basis points) per period so a multi-level
    # list sends each level exactly once per calendar window.
    assert row["warning_sent_weekly_8000_window"] == "2026-09-07"


def test_duplicate_delivery_repairs_failed_status_convergence(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", in_limit=1)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    event = _subscription([_record(input_tokens=1, output_tokens=0)])
    real_evaluate = processor._evaluate_quota

    monkeypatch.setattr(
        processor,
        "_evaluate_quota",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("after commit")),
    )
    with pytest.raises(RuntimeError, match="after commit"):
        _run(event, fake_dynamodb, fake_sns)

    monkeypatch.setattr(processor, "_evaluate_quota", real_evaluate)
    result = _run(event, fake_dynamodb, fake_sns)

    assert result["duplicates"] == 1
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    usage = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": datetime.now(timezone.utc).date().isoformat()}
    )["Item"]
    assert usage["requests"] == 1


def test_unknown_model_uses_configured_conservative_fallback(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"known":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":40,"output_per_mtok":90}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    event = _subscription(
        [
            _record(
                model="unknown",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]
    )
    _run(event, fake_dynamodb, fake_sns)
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 130 * processor.MICRO
    emf = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in json.loads(line)
    )
    assert emf["Model"] == "unknown"
    assert emf["PriceSource"] == "fallback"


def test_us_opus_inference_profile_uses_exact_snapshot_price(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps(
            {
                "us.anthropic.claude-opus-4-7": {
                    "input_per_mtok": 5.5,
                    "output_per_mtok": 27.5,
                }
            }
        ),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription(
            [
                _record(
                    model="us.anthropic.claude-opus-4-7",
                    input_tokens=1_000_000,
                    output_tokens=1_000_000,
                )
            ]
        ),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 33 * processor.MICRO
    emf = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in json.loads(line)
    )
    assert emf["Model"] == "us.anthropic.claude-opus-4-7"
    assert emf["PriceSource"] == "snapshot"
    assert emf["EstimatedCostUSD"] == 33.0


def test_control_message_is_a_noop(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    payload = gzip.compress(
        json.dumps({"messageType": "CONTROL_MESSAGE"}).encode()
    )
    event = {"awslogs": {"data": base64.b64encode(payload).decode()}}
    assert _run(event, fake_dynamodb, fake_sns)["processed"] == 0


def test_processor_does_not_take_over_admin_status_with_auto_prefixed_reason(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", in_limit=10)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    users.update_item(
        Key={"user_id": "alice"},
        UpdateExpression=(
            "SET #s = :status, status_reason = :reason, "
            "status_origin = :origin, version = :version"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "blocked",
            ":reason": "auto: operator-authored reason",
            ":origin": "admin",
            ":version": 7,
        },
    )

    _run(
        _subscription([_record(input_tokens=100, output_tokens=0)]),
        fake_dynamodb,
        fake_sns,
    )

    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "admin"
    assert user["status_reason"] == "auto: operator-authored reason"
    assert user["version"] == 7
    assert "Item" not in users.get_item(Key={"user_id": "REVOCATION#alice"})


def _emf_records(capsys):
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in line and "_aws" in json.loads(line)
    ]


def test_profile_id_resolves_to_base_model_price_before_fallback(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            }
        }),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="eu.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Base model price (5 + 25), not the 90 USD fallback.
    assert row["cost_micro"] == 30 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "base-model"
    assert emf["FallbackPricedRequests"] == 0


def test_explicit_profile_price_wins_over_base_model(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            },
            "us.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.5,
                "output_per_mtok": 27.5,
            },
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="us.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 33 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "snapshot"
    assert emf["FallbackPricedRequests"] == 0


def test_fallback_pricing_emits_alarmable_metric(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"known":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":40,"output_per_mtok":90}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([_record(model="unknown", input_tokens=1, output_tokens=1)]),
        fake_dynamodb,
        fake_sns,
    )

    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "fallback"
    assert emf["FallbackPricedRequests"] == 1
    metric_names = {
        metric["Name"]
        for metric in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    assert "FallbackPricedRequests" in metric_names


class _FakeSsmParameters:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def get_parameter(self, Name):  # noqa: N803 (boto3 API)
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"Parameter": {"Name": Name, "Value": self.value}}


@pytest.fixture(autouse=True)
def _reset_price_cache():
    processor._price_cache.update({"next_attempt_at": 0.0, "value": None})
    yield
    processor._price_cache.update({"next_attempt_at": 0.0, "value": None})


def test_parameter_prices_override_the_deployment_snapshot(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"m":{"input_per_mtok":100,"output_per_mtok":100}}',
    )
    ssm = _FakeSsmParameters(
        value=json.dumps({
            "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            "fallback": {"input_per_mtok": 40.0, "output_per_mtok": 90.0},
            "resolved_at": "2026-09-02T00:00:00+00:00",
        })
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    processor.handler(
        _subscription([
            _record(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
        ]),
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=FakeDynamoClient(fake_dynamodb),
        sns=fake_sns,
        ssm=ssm,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Refreshed parameter price (1 + 2), not the stale env snapshot (200).
    assert row["cost_micro"] == 3 * processor.MICRO
    assert ssm.calls == 1


def test_broken_parameter_falls_back_to_the_deployment_snapshot(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"m":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    ssm = _FakeSsmParameters(error=RuntimeError("parameter store down"))
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    processor.handler(
        _subscription([
            _record(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
        ]),
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=FakeDynamoClient(fake_dynamodb),
        sns=fake_sns,
        ssm=ssm,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Metering never stops: env snapshot is the availability fallback.
    assert row["cost_micro"] == 3 * processor.MICRO
    warning = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "level" in json.loads(line or "{}")
    )
    assert warning["level"] == "warning"


def test_compressed_parameter_value_written_by_the_resolver_is_read(monkeypatch):
    """Cross-Lambda contract: the processor decodes exactly what the price
    resolver's writer produces (gzip + base64 behind the ``gz1:`` marker)."""
    from cdk.pricing_resolver import handler as resolver

    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    document = json.dumps({
        "models": {"m": {"input_per_mtok": 1, "output_per_mtok": 2.5}},
        "fallback": {"input_per_mtok": 40, "output_per_mtok": 90},
        "resolved_at": "2026-10-02T00:00:00+00:00",
    }, sort_keys=True, separators=(",", ":"))
    encoded = resolver.encode_parameter_value(document)
    assert encoded.startswith(processor._PRICE_PARAMETER_COMPRESSED_PREFIX)
    assert not encoded.startswith("{")

    assert processor._parameter_prices(_FakeSsmParameters(value=encoded), 0.0) == (
        {"m": {"input": 1.0, "output": 2.5}},
        {"input": 40.0, "output": 90.0},
    )


def test_legacy_plain_json_parameter_value_is_still_read():
    """An in-place upgrade reads the previous template's plain-JSON value
    until the first refresh rewrites it in the compressed form."""
    document = json.dumps({
        "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
        "fallback": {"input_per_mtok": 40.0, "output_per_mtok": 90.0},
    })
    assert processor._decode_price_parameter(document) == json.loads(document)
    compressed = processor._PRICE_PARAMETER_COMPRESSED_PREFIX + base64.b64encode(
        gzip.compress(document.encode("utf-8"))
    ).decode("ascii")
    assert processor._decode_price_parameter(compressed) == json.loads(document)
    # Anything else is an error (caught by the caller, which then keeps the
    # last good value or the deployment snapshot), never a silent misprice.
    with pytest.raises(ValueError, match="Unrecognised price parameter encoding"):
        processor._decode_price_parameter("gz0:AAAA")


def test_stale_parameter_value_outlives_a_failed_refresh(monkeypatch, capsys):
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    ssm = _FakeSsmParameters(
        value=json.dumps({
            "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            "fallback": {"input_per_mtok": 40.0, "output_per_mtok": 90.0},
        })
    )

    first = processor._parameter_prices(ssm, 1_000.0)
    assert first == (
        {"m": {"input": 1.0, "output": 2.0}},
        {"input": 40.0, "output": 90.0},
    )
    assert ssm.calls == 1

    # Within the TTL, no new fetch.
    assert processor._parameter_prices(ssm, 1_000.0 + 10) == first
    assert ssm.calls == 1

    # After the TTL a refresh is attempted; when it fails the last good
    # value keeps being served (it is fresher than the env snapshot).
    ssm.error = RuntimeError("parameter store down")
    stale = processor._parameter_prices(
        ssm, 1_000.0 + processor._PRICE_CACHE_TTL_SECONDS + 1
    )
    assert stale == first
    assert ssm.calls == 2
    assert "last good value" in capsys.readouterr().out

    # Failures are retried at most once per retry window.
    processor._parameter_prices(
        ssm, 1_000.0 + processor._PRICE_CACHE_TTL_SECONDS + 2
    )
    assert ssm.calls == 2


# ---------------------------------------------------------------------------
# Workload mode: attribution by application-inference-profile ARN
# ---------------------------------------------------------------------------

PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:"
    "application-inference-profile/abc123xyz"
)


def _workload_env(monkeypatch, *, model="us.anthropic.claude-opus-4-7"):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps(
            {model: {"input_per_mtok": 5.5, "output_per_mtok": 27.5}}
        ),
    )
    monkeypatch.setenv(
        "WORKLOAD_PROFILES_JSON",
        json.dumps(
            {
                "payments": {
                    "workload_id": "workload:payments",
                    "profile_arn": PROFILE_ARN,
                    "model": model,
                }
            }
        ),
    )
    monkeypatch.setenv(
        "DEFAULT_LIMITS_JSON",
        json.dumps(
            {
                "daily": {
                    "usd": 2.5,
                    "input_tokens": 111,
                    "output_tokens": 222,
                },
                "weekly": None,
                "monthly": None,
            }
        ),
    )


def _workload_record(**overrides) -> dict:
    """A direct-invocation record: customer principal, profile-ARN modelId."""
    record = _record(
        model=overrides.pop("model", PROFILE_ARN),
        request_id=overrides.pop("request_id", "workload-req-1"),
        **overrides,
    )
    message = json.loads(record["message"])
    message["identity"] = {
        "arn": "arn:aws:iam::111122223333:role/payments-app"
    }
    record["message"] = json.dumps(message)
    return record


def test_workload_invocation_attributes_by_profile_arn(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)

    result = _run(
        _subscription(
            [_workload_record(input_tokens=1000, output_tokens=100)]
        ),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    assert result["ignored"] == 0
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(
            Key={"user_id": "workload:payments", "window": window}
        )
        .get("Item")
    )
    # Priced by the profile's underlying model, not the fallback:
    # 1000 in * 5.5/M + 100 out * 27.5/M = 8250 micro-USD.
    assert usage["cost_micro"] == 8250
    assert usage["input_tokens"] == 1000


def test_workload_row_is_auto_provisioned_with_deploy_defaults(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)

    _run(_subscription([_workload_record()]), fake_dynamodb, fake_sns)

    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["name"] == "payments"
    assert row["status"] == "active"
    assert row["status_origin"] == "automatic"
    assert row["daily_usd_micro"] == 2_500_000
    assert row["daily_input_tokens"] == 111
    assert row["daily_output_tokens"] == 222
    assert row["version"] == 1


def test_workload_positive_submicro_default_remains_finite(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    monkeypatch.setenv(
        "DEFAULT_LIMITS_JSON",
        json.dumps(
            {
                "daily": {
                    "usd": 0.0000001,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
                "weekly": None,
                "monthly": None,
            }
        ),
    )

    _run(_subscription([_workload_record()]), fake_dynamodb, fake_sns)

    row = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "workload:payments"}
    )["Item"]
    assert row["daily_usd_micro"] == 1


def test_workload_auto_provision_never_overwrites_admin_limits(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    _seed_user(fake_dynamodb, "workload:payments", usd=9.0)

    _run(_subscription([_workload_record()]), fake_dynamodb, fake_sns)

    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["daily_usd_micro"] == 9 * processor.MICRO


def test_unknown_application_profile_is_ignored(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    foreign = (
        "arn:aws:bedrock:us-east-1:111122223333:"
        "application-inference-profile/not-ours"
    )

    result = _run(
        _subscription([_workload_record(model=foreign)]),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 0
    assert result["ignored"] == 1
    assert result["unresolved_sessions"] == []


def test_workload_usage_is_idempotent_per_request_id(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    event = _subscription(
        [
            _workload_record(request_id="dup-1"),
            _workload_record(request_id="dup-1"),
        ]
    )

    result = _run(event, fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    assert result["duplicates"] == 1


def test_workload_over_budget_blocks_the_workload_row(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    monkeypatch.setenv(
        "DEFAULT_LIMITS_JSON",
        json.dumps(
            {
                "daily": {
                    "usd": 0.008,
                    "input_tokens": 111,
                    "output_tokens": 222,
                },
                "weekly": None,
                "monthly": None,
            }
        ),
    )

    result = _run(
        _subscription(
            [_workload_record(input_tokens=1000, output_tokens=100)]
        ),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["status"] == "blocked"
    assert row["status_origin"] == "automatic"
    assert row["status_reason"].startswith("auto: daily USD quota exhausted")


def test_vended_session_via_workload_profile_bills_the_workload(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Deterministic precedence: the profile is the cost object."""
    _workload_env(monkeypatch)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    record = _record(model=PROFILE_ARN, request_id="mixed-1")

    result = _run(_subscription([record]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    workload_usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(
            Key={"user_id": "workload:payments", "window": window}
        )
        .get("Item")
    )
    alice_usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(Key={"user_id": "alice", "window": window})
        .get("Item")
    )
    assert workload_usage is not None
    assert alice_usage is None


def test_responses_api_profile_arn_is_priced_by_base_model(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """The Responses API logs the resolved system inference-profile ARN as
    modelId; the processor reduces it to the profile ID so cross-region
    base-model pricing applies instead of the conservative fallback."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "openai.gpt-5.6-luna": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            }
        }),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model=(
                    "arn:aws:bedrock:us-east-1:111122223333:"
                    "inference-profile/us.openai.gpt-5.6-luna"
                ),
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Base model price (5 + 25), not the 90 USD fallback.
    assert row["cost_micro"] == 30 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "base-model"
    assert emf["FallbackPricedRequests"] == 0


def test_metadata_less_duplicate_record_is_not_metered(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Alongside the token-bearing record, the Responses API emits a second
    record whose input/output metadata is empty. It must not inflate the
    request count or create a ledger row."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")

    empty = _record(request_id="responses-empty")
    message = json.loads(empty["message"])
    message["input"] = {}
    message["output"] = {}
    empty["message"] = json.dumps(message)

    result = _run(
        _subscription([empty, _record(request_id="responses-rich")]),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["requests"] == 1
    assert row["input_tokens"] == 100


# ---------------------------------------------------------------------------
# Multi-dimension pricing: prompt cache read/write, images
# ---------------------------------------------------------------------------


def _cached_record(
    *,
    request_id="cache-1",
    model="anthropic.claude-haiku-4-5-20251001-v1:0",
    input_tokens=11,
    output_tokens=4,
    cache_read=0,
    cache_write=0,
) -> dict:
    """Converse records with a cachePoint place the cache counters beside
    inputTokenCount on the input side."""
    record = _record(
        request_id=request_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    message = json.loads(record["message"])
    message["input"]["cacheReadInputTokenCount"] = cache_read
    message["input"]["cacheWriteInputTokenCount"] = cache_write
    record["message"] = json.dumps(message)
    return record


def _today_row(fake_dynamodb, user_id="alice") -> dict:
    return fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": user_id,
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]


def test_cache_read_and_write_tokens_are_priced_per_dimension(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-haiku-4-5-20251001-v1:0": {
                "input_per_mtok": 1.0,
                "output_per_mtok": 5.0,
                "cache_read_per_mtok": 0.1,
                "cache_write_per_mtok": 1.25,
            }
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _cached_record(
                request_id="write",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_write=1_000_000,
            ),
            _cached_record(
                request_id="read",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read=1_000_000,
            ),
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = _today_row(fake_dynamodb)
    # write: 1 + 5 + 1.25 = 7.25 ; read: 1 + 0.1 = 1.10 ; total 8.35 USD
    assert row["cost_micro"] == 8_350_000
    # Token quotas count uncached input and output tokens: cached tokens
    # are accounted separately, never folded into input_tokens.
    assert row["input_tokens"] == 2_000_000
    assert row["cache_write_tokens"] == 1_000_000
    assert row["cache_read_tokens"] == 1_000_000
    assert row["unpriced_requests"] == 0
    assert "missing_dimensions" not in row
    emf = _emf_records(capsys)
    assert emf[0]["CacheWriteTokens"] == 1_000_000
    assert emf[1]["CacheReadTokens"] == 1_000_000
    assert all(e["FallbackPricedRequests"] == 0 for e in emf)
    assert all(e["UnpricedDimensionRequests"] == 0 for e in emf)


def test_image_generation_is_priced_per_image_not_per_token(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "amazon.nova-canvas-v1:0": {
                "input_per_mtok": 0.0,
                "output_per_mtok": 0.0,
                "per_image": 0.04,
            }
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    record = _record(request_id="canvas-1", model="amazon.nova-canvas-v1:0")
    message = json.loads(record["message"])
    # Real Nova Canvas records carry no token counts. When image data
    # delivery is enabled the body lists the generated images.
    message["input"] = {"inputContentType": "application/json"}
    message["output"] = {
        "outputContentType": "application/json",
        "outputBodyJson": {"images": ["<b64>", "<b64>"]},
    }
    record["message"] = json.dumps(message)

    result = _run(_subscription([record]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    row = _today_row(fake_dynamodb)
    assert row["cost_micro"] == 80_000  # 2 images * $0.04
    assert row["images"] == 2
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0
    assert row["requests"] == 1
    emf = _emf_records(capsys)[0]
    assert emf["ImagesGenerated"] == 2
    assert emf["PriceSource"] == "snapshot"
    assert emf["FallbackPricedRequests"] == 0


def test_image_model_record_without_image_count_is_metered_and_flagged(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """With image data delivery disabled (this stack's default), a Nova
    Canvas record has no token counts and no body. It must still count as a
    request and be flagged as unpriced rather than silently dropped."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "amazon.nova-canvas-v1:0": {
                "input_per_mtok": 0.0,
                "output_per_mtok": 0.0,
                "per_image": 0.04,
            }
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    record = _record(request_id="canvas-blind", model="amazon.nova-canvas-v1:0")
    message = json.loads(record["message"])
    message["input"] = {"inputContentType": "application/json"}
    message["output"] = {"outputContentType": "application/json"}
    record["message"] = json.dumps(message)

    result = _run(_subscription([record]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    assert result["unpriced"] == 1
    row = _today_row(fake_dynamodb)
    assert row["requests"] == 1
    # No image count: the processor assumes the one-image floor, prices it
    # at the catalog rate, and flags the request so the operator knows the
    # figure is a lower bound rather than a measurement.
    assert row["images"] == 1
    assert row["cost_micro"] == 40_000
    assert row["unpriced_requests"] == 1
    assert row["missing_dimensions"] == {"image"}
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "snapshot"
    assert emf["ImagesGenerated"] == 1
    assert emf["MissingDimensions"] == ["image"]
    assert emf["FallbackPricedRequests"] == 1
    assert emf["UnpricedDimensionRequests"] == 1


def test_image_model_record_with_a_count_is_not_flagged_as_assumed(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "amazon.nova-canvas-v1:0": {
                "input_per_mtok": 0.0,
                "output_per_mtok": 0.0,
                "per_image": 0.04,
            }
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    record = _record(request_id="canvas-counted", model="amazon.nova-canvas-v1:0")
    message = json.loads(record["message"])
    message["input"] = {"inputContentType": "application/json"}
    message["output"] = {"outputImageCount": 3}
    record["message"] = json.dumps(message)

    _run(_subscription([record]), fake_dynamodb, fake_sns)

    row = _today_row(fake_dynamodb)
    assert row["images"] == 3
    assert row["cost_micro"] == 120_000
    assert row["unpriced_requests"] == 0
    assert _emf_records(capsys)[0]["FallbackPricedRequests"] == 0


def test_input_output_only_pricing_is_unchanged_regression(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """A model priced with only the input/output pair: cache fields absent
    from the log never appear as missing dimensions and the cost is the
    plain input/output sum."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(_subscription([_record()]), fake_dynamodb, fake_sns)

    row = _today_row(fake_dynamodb)
    assert row["cost_micro"] == 200
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["cache_read_tokens"] == 0
    assert row["cache_write_tokens"] == 0
    assert row["images"] == 0
    assert row["unpriced_requests"] == 0
    assert "missing_dimensions" not in row
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "snapshot"
    assert emf["FallbackPricedRequests"] == 0
    assert emf["UnpricedDimensionRequests"] == 0
    assert emf["MissingDimensions"] == []


def test_dimension_present_in_log_but_absent_from_catalog_is_flagged(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """Known model, input/output-only catalog entry, but the record carries
    cache tokens. The request must not be priced at zero for that
    dimension: it uses the fallback's rate when one exists, is flagged in
    the ledger, and raises the fallback/alarm signal."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-haiku-4-5-20251001-v1:0": {
                "input_per_mtok": 1.0,
                "output_per_mtok": 5.0,
            }
        }),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        json.dumps({
            "input_per_mtok": 15.0,
            "output_per_mtok": 75.0,
            "cache_read_per_mtok": 1.5,
            "cache_write_per_mtok": 18.75,
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    result = _run(
        _subscription([
            _cached_record(
                request_id="mixed",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    assert result["unpriced"] == 1
    row = _today_row(fake_dynamodb)
    # input at the model's own rate (1.0) + cache_read at the FALLBACK rate
    # (1.5), never zero.
    assert row["cost_micro"] == 2_500_000
    assert row["unpriced_requests"] == 1
    assert row["missing_dimensions"] == {"cache_read"}
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "snapshot"
    assert emf["MissingDimensions"] == ["cache_read"]
    assert emf["FallbackPricedRequests"] == 1
    assert emf["UnpricedDimensionRequests"] == 1


def test_missing_dimension_with_no_fallback_rate_is_still_flagged(
    fake_dynamodb, fake_sns, monkeypatch
):
    """An input/output-only fallback and a record with an image count:
    nothing can price the image dimension, so it contributes zero but the
    request is flagged so the undercount is discoverable."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    record = _record(request_id="text-plus-image")
    message = json.loads(record["message"])
    message["output"]["outputImageCount"] = 1
    record["message"] = json.dumps(message)

    _run(_subscription([record]), fake_dynamodb, fake_sns)

    row = _today_row(fake_dynamodb)
    assert row["cost_micro"] == 200  # tokens only
    assert row["images"] == 1
    assert row["unpriced_requests"] == 1
    assert row["missing_dimensions"] == {"image"}


def test_missing_dimensions_accumulate_as_a_set_on_the_daily_row(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    first = _cached_record(
        request_id="r1", model="openai.gpt-oss-20b", cache_read=10
    )
    second = _cached_record(
        request_id="r2", model="openai.gpt-oss-20b", cache_write=10
    )
    third = _cached_record(
        request_id="r3", model="openai.gpt-oss-20b", cache_read=10
    )
    _run(_subscription([first, second, third]), fake_dynamodb, fake_sns)

    row = _today_row(fake_dynamodb)
    assert row["requests"] == 3
    assert row["unpriced_requests"] == 3
    assert row["missing_dimensions"] == {"cache_read", "cache_write"}


def test_price_entry_uses_resolver_rate_keys_and_ignores_unknown_keys():
    """``*_per_mtok`` / ``per_image`` is the only shape the resolver emits;
    anything else in an entry is ignored rather than guessed at."""
    rates = processor._rates_from_entry(
        {
            "input_per_mtok": 1.0,
            "output_per_mtok": 2.0,
            "cache_read_per_mtok": 0.1,
            "per_image": 0.04,
            "unknown": 9,
            "dimensions": {"cache_write": 5.0},
        }
    )
    assert rates == {"input": 1.0, "output": 2.0, "cache_read": 0.1, "image": 0.04}
    with pytest.raises(ValueError, match="input and output"):
        processor._rates_from_entry({"cache_read_per_mtok": 0.1})


# ---------------------------------------------------------------------------
# Shipped catalog pins and geographic profile pricing
# ---------------------------------------------------------------------------

_PRICING_PATH = ROOT / "cdk" / "config" / "model-pricing.json"
_GEO_PREFIXES = ("us", "eu", "apac", "jp", "au")


def _shipped_overrides() -> dict:
    return json.loads(_PRICING_PATH.read_text())["price_overrides"]


def test_every_claude_pin_has_cache_rates_and_geo_variants():
    """Every Claude pin prices prompt-cache reads (10 % of input) and writes
    (125 % of input), and every base Claude model has a pin for each
    geographic profile at the 10 % uplift, so no geographic Claude traffic
    is cache-unpriced or silently derived from the base rate."""
    overrides = _shipped_overrides()
    claude = {
        model_id: entry
        for model_id, entry in overrides.items()
        if "anthropic.claude" in model_id
    }
    assert claude
    for model_id, entry in claude.items():
        rate = entry["input_per_mtok"]
        assert entry["cache_read_per_mtok"] == round(rate * 0.10, 4), model_id
        assert entry["cache_write_per_mtok"] == round(rate * 1.25, 4), model_id
        assert entry["reason"].strip()
    bases = [model_id for model_id in claude if model_id.startswith("anthropic.")]
    assert bases
    for base in bases:
        for prefix in _GEO_PREFIXES:
            geo = claude[f"{prefix}.{base}"]
            assert geo["input_per_mtok"] == round(
                claude[base]["input_per_mtok"] * 1.1, 4
            ), f"{prefix}.{base}"
            assert geo["output_per_mtok"] == round(
                claude[base]["output_per_mtok"] * 1.1, 4
            ), f"{prefix}.{base}"
    nova = overrides["amazon.nova-2-lite-v1:0"]
    assert nova["cache_read_per_mtok"] == 0.0825
    assert nova["cache_write_per_mtok"] == 0.0
    assert "-mantle-" in overrides["xai.grok-4.6"]["reason"]


def test_shipped_geo_claude_pins_resolve_as_snapshot_prices():
    prices = {
        model_id: processor._rates_from_entry(entry)
        for model_id, entry in _shipped_overrides().items()
    }
    fallback = {"input": 15.0, "output": 75.0}
    haiku = "anthropic.claude-haiku-4-5-20251001-v1:0"
    for prefix in _GEO_PREFIXES:
        rates, source = processor._price_for(prices, fallback, f"{prefix}.{haiku}")
        assert source == "snapshot"
        assert rates["input"] == 1.1
        assert rates["cache_read"] == 0.11
    # ``global.`` is priced at the base rate by definition: derived, not
    # flagged, even though the geographic siblings are pinned higher.
    rates, source = processor._price_for(prices, fallback, f"global.{haiku}")
    assert (rates["input"], source) == (1.0, "base-model")


def test_base_model_derivation_is_flagged_when_sibling_geo_pin_differs(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """Only base and ``us.`` pinned (the pre-fix convention): an ``eu.``
    call is still priced from the base model, but the ``us.`` pin proves a
    geographic uplift applies, so the request raises the fallback alarm
    instead of under-counting silently."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            },
            "us.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.5,
                "output_per_mtok": 27.5,
            },
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="eu.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = _today_row(fake_dynamodb)
    assert row["cost_micro"] == 30 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "base-model-mismatch"
    assert emf["FallbackPricedRequests"] == 1
    assert emf["UnpricedDimensionRequests"] == 0
    assert emf["MissingDimensions"] == []


def test_global_profile_derivation_is_not_flagged_by_geo_siblings(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            },
            "us.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.5,
                "output_per_mtok": 27.5,
            },
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="global.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    assert _today_row(fake_dynamodb)["cost_micro"] == 30 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "base-model"
    assert emf["FallbackPricedRequests"] == 0


def test_sibling_geo_pin_equal_to_base_is_not_a_mismatch():
    base = {"input": 1.0, "output": 2.0}
    prices = {"vendor.model": base, "us.vendor.model": dict(base)}
    fallback = {"input": 15.0, "output": 75.0}
    assert processor._price_for(prices, fallback, "eu.vendor.model") == (
        base,
        "base-model",
    )
    # No sibling pins at all: plain derivation, nothing to compare against.
    assert processor._price_for({"vendor.model": base}, fallback, "eu.vendor.model") == (
        base,
        "base-model",
    )


# ---------------------------------------------------------------------------
# Thresholds: multi-level warnings, alert-only budgets, rate limits
# ---------------------------------------------------------------------------


def _thresholds(*entries: tuple[float, str]) -> list[dict]:
    return [{"at_bps": int(at * 10_000), "action": action} for at, action in entries]


def test_multi_threshold_warnings_send_each_level_exactly_once(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(
        fake_dynamodb,
        "alice",
        in_limit=100,
        out_limit=0,
        daily_thresholds=_thresholds((0.5, "warn"), (0.8, "warn"), (1.0, "block")),
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(_subscription([_record(request_id="a", input_tokens=50, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert [m["Subject"] for m in fake_sns.published] == [
        "[bedrock-spend-controls] WARNING alice daily 50%"
    ]
    # Same level again: no duplicate.
    _run(_subscription([_record(request_id="b", input_tokens=10, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1

    _run(_subscription([_record(request_id="c", input_tokens=20, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert fake_sns.published[1]["Subject"] == (
        "[bedrock-spend-controls] WARNING alice daily 80%"
    )
    assert json.loads(fake_sns.published[1]["Message"])["threshold"] == 0.8

    _run(_subscription([_record(request_id="d", input_tokens=20, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 3
    assert fake_sns.published[2]["Subject"] == (
        "[bedrock-spend-controls] BLOCKED alice reason=daily-input_tokens"
    )
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["warning_sent_daily_5000_window"]
    assert user["warning_sent_daily_8000_window"]


def test_one_request_crossing_two_levels_sends_both_once(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(
        fake_dynamodb, "alice", in_limit=100, out_limit=0,
        daily_thresholds=_thresholds((0.5, "warn"), (0.8, "warn"), (1.0, "block")),
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(_subscription([_record(request_id="a", input_tokens=85, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert [m["Subject"] for m in fake_sns.published] == [
        "[bedrock-spend-controls] WARNING alice daily 50%",
        "[bedrock-spend-controls] WARNING alice daily 80%",
    ]
    _run(_subscription([_record(request_id="b", input_tokens=1, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 2


def test_warning_marker_is_released_when_publish_fails(
    fake_dynamodb, monkeypatch
):
    """The marker is claimed before publishing (so concurrent invocations
    send once) but must not survive a failed publish, or the warning is
    lost for the rest of the window. The Logs retry re-sends it."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("WARN_THRESHOLD", "0.8")
    _seed_user(fake_dynamodb, "alice", in_limit=100, out_limit=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    sns = FailingSNS(failures=1)
    event = _subscription([_record(request_id="a", input_tokens=80, output_tokens=0)])

    with pytest.raises(RuntimeError, match="sns down"):
        _run(event, fake_dynamodb, sns)

    assert sns.published == []
    assert "warning_sent_daily_8000_window" not in users.get_item(
        Key={"user_id": "alice"}
    )["Item"]

    # Redelivery: a duplicate for accounting, but the warning goes out now.
    result = _run(event, fake_dynamodb, sns)
    assert result["duplicates"] == 1
    assert [m["Subject"] for m in sns.published] == [
        "[bedrock-spend-controls] WARNING alice daily 80%"
    ]
    assert users.get_item(Key={"user_id": "alice"})["Item"][
        "warning_sent_daily_8000_window"
    ] == _today()

    # And a further request at the same level sends nothing new.
    _run(_subscription([_record(request_id="b", input_tokens=1, output_tokens=0)]),
         fake_dynamodb, sns)
    assert len(sns.published) == 1


def test_warning_marker_claimed_elsewhere_skips_publish(
    fake_dynamodb, fake_sns, monkeypatch
):
    """The conditional claim loses to a marker already at this window or a
    later one (another invocation, or a late log replaying an old window),
    so that invocation publishes nothing and leaves the marker alone."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("WARN_THRESHOLD", "0.8")
    _seed_user(
        fake_dynamodb, "alice", in_limit=100, out_limit=1000,
        warning_sent_daily_8000_window="9999-12-31",
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    result = _run(
        _subscription([_record(request_id="a", input_tokens=80, output_tokens=0)]),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    assert fake_sns.published == []
    assert fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]["warning_sent_daily_8000_window"] == "9999-12-31"


def test_alert_only_budget_warns_but_never_blocks(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(
        fake_dynamodb, "alice", in_limit=100, out_limit=0,
        daily_thresholds=_thresholds((1.0, "warn"), (2.0, "warn")),
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    # 500 % of the limit: warnings at 100 % and 200 %, no block.
    _run(_subscription([_record(request_id="a", input_tokens=500, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "active"
    assert [m["Subject"] for m in fake_sns.published] == [
        "[bedrock-spend-controls] WARNING alice daily 100%",
        "[bedrock-spend-controls] WARNING alice daily 200%",
    ]
    assert "Item" not in fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "REVOCATION#alice"}
    )


def test_block_threshold_above_one_hundred_percent(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(
        fake_dynamodb, "alice", in_limit=100, out_limit=0,
        daily_thresholds=_thresholds((1.0, "warn"), (1.5, "block")),
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])

    _run(_subscription([_record(request_id="a", input_tokens=120, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert users.get_item(Key={"user_id": "alice"})["Item"]["status"] == "active"

    _run(_subscription([_record(request_id="b", input_tokens=30, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"].startswith(
        "auto: daily input tokens quota exhausted at 150%"
    )


def test_row_without_stored_thresholds_uses_deployment_default(
    fake_dynamodb, fake_sns, monkeypatch
):
    """WARN_THRESHOLD warn + block at 100 % for a row with no stored
    thresholds list. Mirrors test_processor_warns_once_then_blocks_at_any_limit."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv("WARN_THRESHOLD", "0.75")
    _seed_user(fake_dynamodb, "alice", in_limit=100, out_limit=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(_subscription([_record(request_id="a", input_tokens=74, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert fake_sns.published == []
    _run(_subscription([_record(request_id="b", input_tokens=1, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1
    assert "WARNING alice daily 75%" in fake_sns.published[0]["Subject"]
    _run(_subscription([_record(request_id="c", input_tokens=25, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"] == (
        f"auto: daily input tokens quota exhausted in "
        f"{datetime.now(timezone.utc).date().isoformat()}"
    )


def test_rpm_breach_blocks_and_recovers_next_minute(
    fake_dynamodb, fake_sns, monkeypatch
):
    fixed = datetime(2026, 9, 9, 12, 0, 30, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FrozenDateTime._now if tz is not None else FrozenDateTime._now.replace(tzinfo=None)

    FrozenDateTime._now = fixed
    monkeypatch.setattr(processor, "datetime", FrozenDateTime)
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(fake_dynamodb, "alice", usd=1000, in_limit=0, out_limit=0, rpm=2, tpm=0)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])

    _run(_subscription([_record(request_id="a", when=fixed)]), fake_dynamodb, fake_sns)
    assert users.get_item(Key={"user_id": "alice"})["Item"]["status"] == "active"
    counter = usage_table.get_item(
        Key={"user_id": "RATE#alice", "window": "2026-09-09T12:00"}
    )["Item"]
    assert counter["requests"] == 1
    assert counter["tokens"] == 150

    _run(_subscription([_record(request_id="b", when=fixed)]), fake_dynamodb, fake_sns)
    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "automatic"
    assert user["status_reason"] == "auto: rpm rate limit reached in minute 2026-09-09T12:00"
    assert fake_sns.published[-1]["Subject"] == (
        "[bedrock-spend-controls] BLOCKED alice reason=rpm"
    )
    payload = json.loads(fake_sns.published[-1]["Message"])
    assert payload["breaches"][0] == {
        "period": "minute",
        "dimension": "rpm",
        "usage": 2,
        "limit": 2,
        "at": 1.0,
        "window_start": "2026-09-09T12:00:00+00:00",
        "resets_at": "2026-09-09T12:01:00+00:00",
    }
    assert payload["rate_usage"]["requests"] == 2

    # Next minute: the automatic block lifts through the broker's
    # refresh_auto_status path (same rule as calendar blocks); here we assert
    # the store-level evaluation is under quota again.
    from app.quota import QuotaStore
    store = QuotaStore(dynamodb=fake_dynamodb)
    later = fixed + timedelta(minutes=1)
    refreshed = store.get_user("alice")
    assert store.evaluate_user_quota(refreshed, later).over_budget is False
    assert store.evaluate_user_quota(refreshed, fixed).over_budget is True


def test_tpm_breach_counts_uncached_input_plus_output_tokens(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", usd=1000, in_limit=0, out_limit=0, rpm=0, tpm=200)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    now = datetime.now(timezone.utc).replace(second=1, microsecond=0)

    # 100 in + 50 out = 150 < 200; cached tokens do not count.
    _run(_subscription([_cached_record(request_id="a", model="openai.gpt-oss-20b",
                                       input_tokens=100, output_tokens=50, cache_read=5000)]),
         fake_dynamodb, fake_sns)
    assert users.get_item(Key={"user_id": "alice"})["Item"]["status"] == "active"
    _run(_subscription([_record(request_id="b", when=now, input_tokens=40, output_tokens=20)]),
         fake_dynamodb, fake_sns)
    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"].startswith("auto: tpm rate limit reached")


def test_rate_counter_not_written_for_subjects_without_rate_limits(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    _run(_subscription([_record()]), fake_dynamodb, fake_sns)
    rate_rows = [
        key for key in fake_dynamodb.Table(os.environ["USAGE_TABLE"]).items
        if str(key[0]).startswith("RATE#")
    ]
    assert rate_rows == []


def test_rate_counter_is_keyed_by_occurrence_minute_not_processing_time(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", rpm=100)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    late = datetime(2026, 9, 9, 8, 15, 5, tzinfo=timezone.utc)
    _run(_subscription([_record(when=late)]), fake_dynamodb, fake_sns)
    assert "Item" in fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "RATE#alice", "window": "2026-09-09T08:15"}
    )


def test_rpm_fires_when_lag_crosses_minute(fake_dynamodb, fake_sns, monkeypatch):
    """A burst at 12:00:50 delivered at 12:01:05 is evaluated against the
    12:00 counter it incremented, not the empty 12:01 one."""
    occurred = datetime(2026, 9, 9, 12, 0, 50, tzinfo=timezone.utc)
    _freeze(monkeypatch, datetime(2026, 9, 9, 12, 1, 5, tzinfo=timezone.utc))
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(fake_dynamodb, "alice", usd=1000, in_limit=0, out_limit=0, rpm=2, tpm=0)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])

    _run(
        _subscription(
            [_record(request_id=f"burst-{index}", when=occurred) for index in range(5)]
        ),
        fake_dynamodb,
        fake_sns,
    )

    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "automatic"
    assert user["status_reason"] == (
        "auto: rpm rate limit reached in minute 2026-09-09T12:00"
    )
    assert usage_table.get_item(
        Key={"user_id": "RATE#alice", "window": "2026-09-09T12:00"}
    )["Item"]["requests"] == 5
    assert "Item" not in usage_table.get_item(
        Key={"user_id": "RATE#alice", "window": "2026-09-09T12:01"}
    )
    blocked = [m for m in fake_sns.published if "BLOCKED" in m["Subject"]]
    assert len(blocked) == 1
    breach = json.loads(blocked[0]["Message"])["breaches"][0]
    assert breach["window_start"] == "2026-09-09T12:00:00+00:00"
    assert breach["usage"] == 2  # the request that crossed the limit


def test_rate_block_does_not_refire_each_minute(
    fake_dynamodb, fake_sns, monkeypatch
):
    """A subject still over its rpm in the next minute stays blocked without
    a new block transaction, REVOCATION# write or SNS message; the stored
    reason keeps naming the minute the block was raised in."""
    frozen = _freeze(monkeypatch, datetime(2026, 9, 9, 12, 0, 30, tzinfo=timezone.utc))
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    _seed_user(fake_dynamodb, "alice", usd=1000, in_limit=0, out_limit=0, rpm=2, tpm=0)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])

    _run(
        _subscription([
            _record(request_id="a", when=frozen._now),
            _record(request_id="b", when=frozen._now),
        ]),
        fake_dynamodb,
        fake_sns,
    )
    first = users.get_item(Key={"user_id": "alice"})["Item"]
    assert first["status"] == "blocked"
    assert first["version"] == 1
    assert len(fake_sns.published) == 1
    revocation = users.get_item(Key={"user_id": "REVOCATION#alice"})["Item"]

    frozen._now = datetime(2026, 9, 9, 12, 1, 30, tzinfo=timezone.utc)
    _run(
        _subscription([
            _record(request_id="c", when=frozen._now),
            _record(request_id="d", when=frozen._now),
        ]),
        fake_dynamodb,
        fake_sns,
    )

    second = users.get_item(Key={"user_id": "alice"})["Item"]
    assert second["status"] == "blocked"
    assert second["version"] == 1
    assert second["status_reason"] == (
        "auto: rpm rate limit reached in minute 2026-09-09T12:00"
    )
    assert second["status_changed_at"] == first["status_changed_at"]
    assert len(fake_sns.published) == 1
    assert users.get_item(Key={"user_id": "REVOCATION#alice"})["Item"] == revocation
    # Usage in the new minute is still counted.
    assert usage_table.get_item(
        Key={"user_id": "RATE#alice", "window": "2026-09-09T12:01"}
    )["Item"]["requests"] == 2


def test_block_scope_ignores_the_window_but_not_the_dimension():
    assert processor._block_scope(
        "auto: rpm rate limit reached in minute 2026-09-09T12:00"
    ) == processor._block_scope(
        "auto: rpm rate limit reached in minute 2026-09-09T12:01"
    )
    assert processor._block_scope(
        "auto: daily USD quota exhausted for model opus in 2026-09-09"
    ) == "auto: daily USD quota exhausted for model opus"
    assert processor._block_scope(
        "auto: rpm rate limit reached in minute 2026-09-09T12:00"
    ) != processor._block_scope(
        "auto: tpm rate limit reached in minute 2026-09-09T12:00"
    )


def test_rate_counter_is_written_in_the_ledger_transaction(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", rpm=100)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    client = RecordingClient(fake_dynamodb)

    processor.handler(
        _subscription([_record()]),
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=client,
        sns=fake_sns,
    )

    [items] = client.transactions
    assert len(items) == 4
    keys = [_decode_map(item["Update"]["Key"])["user_id"] for item in items if "Update" in item]
    assert "RATE#alice" in keys
    assert fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key=processor.rate_row_key("alice", datetime.now(timezone.utc))
    )["Item"]["requests"] == 1


def test_rate_counter_not_lost_on_retry(fake_dynamodb, fake_sns, monkeypatch):
    """A failure after the ledger transaction used to skip the rate counter
    for good (the retry sees the request marker and stops). The counter now
    commits with the ledger, and the retry does not double count."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", rpm=100)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    when = datetime.now(timezone.utc)
    event = _subscription([_record(when=when)])
    counter_key = processor.rate_row_key("alice", when)
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    real_emit = processor._emit_emf

    def explode(*args, **kwargs):
        raise RuntimeError("after commit")

    monkeypatch.setattr(processor, "_emit_emf", explode)
    with pytest.raises(RuntimeError, match="after commit"):
        _run(event, fake_dynamodb, fake_sns)
    assert usage_table.get_item(Key=counter_key)["Item"]["requests"] == 1

    monkeypatch.setattr(processor, "_emit_emf", real_emit)
    result = _run(event, fake_dynamodb, fake_sns)
    assert result["duplicates"] == 1
    assert usage_table.get_item(Key=counter_key)["Item"]["requests"] == 1


# ---------------------------------------------------------------------------
# Per-model budgets (optional second axis)
# ---------------------------------------------------------------------------


def _model_budget(usd: float, **thresholds_kwargs) -> dict:
    """Storage form of one model budget (daily only)."""
    return {
        "daily_limits_enabled": True,
        "daily_usd_micro": int(usd * processor.MICRO),
        "daily_input_tokens": 0,
        "daily_output_tokens": 0,
        "daily_thresholds": thresholds_kwargs.get(
            "thresholds",
            _thresholds((0.8, "warn"), (1.0, "block")),
        ),
        "weekly_limits_enabled": False,
        "monthly_limits_enabled": False,
    }


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_model_scoped_ledger_row_is_written_in_the_same_transaction(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])

    _run(_subscription([_record()]), fake_dynamodb, fake_sns)

    subject_row = usage_table.get_item(
        Key={"user_id": "alice", "window": _today()}
    )["Item"]
    model_row = usage_table.get_item(
        Key={"user_id": "alice#model#openai.gpt-oss-20b", "window": _today()}
    )["Item"]
    for field in ("cost_micro", "input_tokens", "output_tokens", "requests"):
        assert model_row[field] == subject_row[field]
    assert model_row["expires_at"] == subject_row["expires_at"]


def test_duplicate_request_skips_both_subject_and_model_rows(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    event = _subscription([_record()])

    _run(event, fake_dynamodb, fake_sns)
    duplicate = _run(event, fake_dynamodb, fake_sns)

    assert duplicate["duplicates"] == 1
    assert usage_table.get_item(
        Key={"user_id": "alice", "window": _today()}
    )["Item"]["requests"] == 1
    assert usage_table.get_item(
        Key={"user_id": "alice#model#openai.gpt-oss-20b", "window": _today()}
    )["Item"]["requests"] == 1


def test_model_budget_blocks_subject_while_total_budget_has_headroom(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "opus": {"input_per_mtok": 10.0, "output_per_mtok": 10.0},
            "haiku": {"input_per_mtok": 1.0, "output_per_mtok": 1.0},
        }),
    )
    # $100 total, but only $2 of it may go to opus.
    _seed_user(
        fake_dynamodb, "alice", usd=100, in_limit=0, out_limit=0,
        model_budgets={"opus": _model_budget(2.0)},
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])

    # $1.50 of opus: under both.
    _run(_subscription([_record(request_id="a", model="opus",
                                input_tokens=100_000, output_tokens=50_000)]),
         fake_dynamodb, fake_sns)
    assert users.get_item(Key={"user_id": "alice"})["Item"]["status"] == "active"
    # $2.00 total on opus: model budget reached, total is $2 of $100.
    _run(_subscription([_record(request_id="b", model="opus",
                                input_tokens=50_000, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "automatic"
    assert user["status_reason"] == (
        f"auto: daily USD quota exhausted for model opus in {_today()}"
    )
    assert fake_sns.published[-1]["Subject"] == (
        "[bedrock-spend-controls] BLOCKED alice reason=model:opus:daily-usd"
    )
    payload = json.loads(fake_sns.published[-1]["Message"])
    assert payload["breaches"][0]["model_id"] == "opus"
    assert payload["breaches"][0]["usage"] == 2_000_000
    assert "Item" in users.get_item(Key={"user_id": "REVOCATION#alice"})


def test_model_budget_breach_blocks_other_models_too_documented_limitation(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Enforcement is subject-wide: once the opus budget blocks alice, a
    haiku call is still metered but the subject stays blocked. The deny
    primitives (SourceIdentity shards / role inline deny) cannot be made
    model-selective without blowing the shard cap; see README."""
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "opus": {"input_per_mtok": 10.0, "output_per_mtok": 10.0},
            "haiku": {"input_per_mtok": 1.0, "output_per_mtok": 1.0},
        }),
    )
    _seed_user(
        fake_dynamodb, "alice", usd=100, in_limit=0, out_limit=0,
        model_budgets={"opus": _model_budget(1.0)},
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])

    _run(_subscription([_record(request_id="a", model="opus",
                                input_tokens=100_000, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert users.get_item(Key={"user_id": "alice"})["Item"]["status"] == "blocked"

    # A haiku call (no budget) arrives from an already-authorized session.
    _run(_subscription([_record(request_id="b", model="haiku",
                                input_tokens=10, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert "for model opus" in user["status_reason"]
    # ...and its usage still lands in the haiku model ledger.
    assert fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice#model#haiku", "window": _today()}
    )["Item"]["requests"] == 1


def test_model_budget_warn_thresholds_are_tracked_per_model(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({"opus": {"input_per_mtok": 10.0, "output_per_mtok": 10.0}}),
    )
    _seed_user(
        fake_dynamodb, "alice", usd=100, in_limit=0, out_limit=0,
        model_budgets={
            "opus": _model_budget(
                2.0, thresholds=_thresholds((0.5, "warn"), (1.0, "block"))
            )
        },
    )
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(_subscription([_record(request_id="a", model="opus",
                                input_tokens=100_000, output_tokens=0)]),
         fake_dynamodb, fake_sns)  # $1.00 = 50 %
    assert [m["Subject"] for m in fake_sns.published] == [
        "[bedrock-spend-controls] WARNING alice model opus daily 50%"
    ]
    assert json.loads(fake_sns.published[0]["Message"])["model_id"] == "opus"
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["warning_sent_model_opus_daily_5000_window"] == _today()
    # Same level again is not re-sent.
    _run(_subscription([_record(request_id="b", model="opus",
                                input_tokens=1_000, output_tokens=0)]),
         fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1


def test_subject_without_model_budgets_pays_no_extra_reads(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    usage_table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    calls: list[str] = []
    original_query = usage_table.query

    def counting_query(**kwargs):
        calls.append(kwargs["ExpressionAttributeValues"][":user_id"])
        return original_query(**kwargs)

    usage_table.query = counting_query
    _run(_subscription([_record()]), fake_dynamodb, fake_sns)
    # Only the subject ledger is queried during evaluation.
    assert calls == ["alice"]


# ---------------------------------------------------------------------------
# tools/unpriced_usage.py
# ---------------------------------------------------------------------------


def _unpriced_usage_tool():
    spec = importlib.util.spec_from_file_location(
        "unpriced_usage", ROOT / "tools" / "unpriced_usage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unpriced_usage_tool_lists_each_subject_day_once_with_its_models(
    fake_dynamodb,
):
    """The subject row and its ``#model#`` rows describe the same requests;
    the tool prints one line per subject and day and folds the model rows
    into it instead of listing every unpriced request twice."""
    tool = _unpriced_usage_tool()
    table = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    table.put_item(Item={
        "user_id": "alice", "window": "2026-09-09", "requests": 5,
        "unpriced_requests": 3, "missing_dimensions": {"cache_read"},
        "cost_micro": 2_500_000, "cache_read_tokens": 10,
    })
    table.put_item(Item={
        "user_id": "alice#model#haiku", "window": "2026-09-09", "requests": 3,
        "unpriced_requests": 2, "missing_dimensions": {"cache_read"},
    })
    table.put_item(Item={
        "user_id": "alice#model#opus", "window": "2026-09-09", "requests": 2,
        "unpriced_requests": 1, "missing_dimensions": {"cache_read"},
    })
    table.put_item(Item={
        "user_id": "alice", "window": "2026-09-08", "requests": 4,
        "unpriced_requests": 0,
    })
    table.put_item(Item={
        "user_id": "bob#model#haiku", "window": "2026-09-01", "requests": 1,
        "unpriced_requests": 1, "missing_dimensions": {"image"},
    })
    table.put_item(Item={"user_id": "REQUEST#r1", "window": "EVENT", "unpriced_requests": 9})
    table.put_item(Item={"user_id": "RATE#alice", "window": "2026-09-09T12:00", "unpriced_requests": 9})

    rows = tool.collect_rows(table, None)

    assert [(row["user_id"], row["window"]) for row in rows] == [
        ("bob", "2026-09-01"),
        ("alice", "2026-09-09"),
    ]
    alice = rows[1]
    assert alice["requests"] == 5
    assert alice["unpriced_requests"] == 3
    assert alice["missing_dimensions"] == ["cache_read"]
    assert alice["cost_usd"] == 2.5
    assert alice["models"] == ["haiku:2", "opus:1"]
    # A model row with no surviving subject row is still reported.
    assert rows[0]["models"] == ["haiku:1"]
    assert rows[0]["unpriced_requests"] == 1

    assert [row["user_id"] for row in tool.collect_rows(table, "2026-09-09")] == ["alice"]
