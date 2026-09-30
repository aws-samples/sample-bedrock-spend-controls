"""Reconciliation Lambda tests: ledger vs Cost Explorer, once a day."""

import json
import os
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from reconciliation_processor import handler as reconciler

REGION = "us-east-1"
DAY = "2026-09-12"


class FakeCostExplorer:
    """Returns fixed amounts per (filter signature) and records calls."""

    def __init__(self, *, aggregate: float = 0.0, by_tag: dict | None = None, error=None):
        self.aggregate = aggregate
        self.by_tag = by_tag or {}
        self.error = error
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        parts = kwargs["Filter"]["And"]
        tag = next((p["Tags"] for p in parts if "Tags" in p), None)
        amount = self.aggregate if tag is None else self.by_tag.get(tag["Values"][0], 0.0)
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": DAY, "End": "2026-09-13"},
                    "Total": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}},
                    "Groups": [],
                    "Estimated": False,
                }
            ]
        }


class FakeSNS:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


def _seed(usage_table, user_id: str, window: str, cost_usd: float) -> None:
    usage_table.put_item(
        Item={"user_id": user_id, "window": window, "cost_micro": int(cost_usd * reconciler.MICRO), "requests": 1}
    )


def _env(monkeypatch, *, workloads: dict | None = None, lag: int = 2) -> None:
    monkeypatch.setenv("USAGE_TABLE", os.environ["USAGE_TABLE"])
    monkeypatch.setenv("RECONCILE_REGION", REGION)
    monkeypatch.setenv("RECONCILE_LAG_DAYS", str(lag))
    monkeypatch.setenv("USAGE_RETENTION_DAYS", "35")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:alerts")
    monkeypatch.setenv("WORKLOADS_JSON", json.dumps(workloads or {}))


def _emf(capsys) -> list[dict]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in line
    ]


def test_aggregate_delta_is_computed_from_subject_rows_only(fake_dynamodb, monkeypatch, capsys):
    _env(monkeypatch)
    usage = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    # Subject rows for the reconciled day: alice 3.00 + workload 2.00 = 5.00.
    _seed(usage, "alice", DAY, 3.0)
    _seed(usage, "workload:payments", DAY, 2.0)
    # Excluded: per-model rows (would double count), other days, markers.
    _seed(usage, "alice#model#opus", DAY, 3.0)
    _seed(usage, "alice", "2026-09-11", 99.0)
    usage.put_item(Item={"user_id": "REQUEST#abc", "window": "EVENT", "expires_at": 1})
    usage.put_item(Item={"user_id": "RATE#alice", "window": "2026-09-12T10:00", "requests": 5, "tokens": 10})
    ce = FakeCostExplorer(aggregate=5.5)

    result = reconciler.handler({"day": DAY}, None, dynamodb=fake_dynamodb, ce=ce, sns=FakeSNS())

    assert result["aggregate"] == {
        "estimated_usd": 5.0,
        "billed_usd": 5.5,
        "delta_usd": 0.5,
        "delta_percent": pytest.approx(9.091, abs=0.001),
    }
    # CE was filtered to Bedrock service names and the deployment region.
    filt = ce.calls[0]["Filter"]["And"]
    assert {"Amazon Bedrock", "Amazon Bedrock Service"} == set(filt[0]["Dimensions"]["Values"])
    assert filt[1]["Dimensions"]["Values"] == [REGION]
    assert ce.calls[0]["Granularity"] == "DAILY"
    assert ce.calls[0]["TimePeriod"] == {"Start": DAY, "End": "2026-09-13"}
    # Stored for the admin API.
    stored = usage.get_item(Key={"user_id": f"RECONCILE#{DAY}", "window": DAY})["Item"]
    assert stored["result"]["aggregate"]["billed_usd"] == pytest.approx(5.5)
    assert stored["expires_at"] > int(datetime.now(timezone.utc).timestamp())
    emf = _emf(capsys)
    aggregate = next(e for e in emf if e["Scope"] == "aggregate")
    assert aggregate["ReconciliationEstimatedUSD"] == 5.0
    assert aggregate["ReconciliationBilledUSD"] == 5.5
    assert aggregate["ReconciliationDeltaUSD"] == 0.5
    assert aggregate["ReconciliationDeltaPercent"] == pytest.approx(9.091, abs=0.001)
    assert aggregate["ReconciliationRuns"] == 1


def test_default_day_is_today_minus_lag(fake_dynamodb, monkeypatch):
    _env(monkeypatch, lag=3)
    ce = FakeCostExplorer(aggregate=0.0)
    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, ce=ce, sns=FakeSNS())
    expected = (datetime.now(timezone.utc).date().toordinal() - 3)
    assert datetime.fromisoformat(result["day"]).date().toordinal() == expected
    assert result["aggregate"]["delta_percent"] is None  # nothing on either side


def test_delta_percent_is_bounded_to_plus_minus_100():
    # Relative to the larger side, never to the bill alone.
    assert reconciler._delta(5.0, 5.5) == (pytest.approx(0.5), pytest.approx(9.0909, abs=0.001))
    assert reconciler._delta(5.5, 5.0) == (pytest.approx(-0.5), pytest.approx(-9.0909, abs=0.001))
    # The case that used to read -72,000,000 %: a real ledger, a near-zero bill.
    _, percent = reconciler._delta(5.0, 0.000007)
    assert -100.0 <= percent < -99.99
    # Bill with nothing metered (unmetered callers) is the mirror image.
    assert reconciler._delta(0.0, 3.0) == (pytest.approx(3.0), pytest.approx(100.0))
    # Ledger with a zero bill (credits / inactive tag) is -100, not undefined.
    assert reconciler._delta(2.0, 0.0) == (pytest.approx(-2.0), pytest.approx(-100.0))
    # Only a fully empty day has no ratio.
    assert reconciler._delta(0.0, 0.0) == (0.0, None)


def test_per_workload_uses_tag_filter_and_flags_inactive_tag(fake_dynamodb, monkeypatch, capsys):
    workloads = {
        "workload:payments": {"name": "payments", "profile_arn": "arn:p", "role_arn": ""},
        "workload:reports": {"name": "reports", "profile_arn": "arn:r", "role_arn": ""},
    }
    _env(monkeypatch, workloads=workloads)
    usage = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    _seed(usage, "workload:payments", DAY, 2.0)
    _seed(usage, "workload:reports", DAY, 1.0)
    # payments is tagged and billed; reports has ledger spend but CE sees
    # nothing for its tag -> tag almost certainly not activated.
    ce = FakeCostExplorer(aggregate=3.3, by_tag={"payments": 2.1})
    sns = FakeSNS()

    result = reconciler.handler({"day": DAY}, None, dynamodb=fake_dynamodb, ce=ce, sns=sns)

    payments, reports = result["workloads"]
    assert payments["name"] == "payments"
    assert payments["estimated_usd"] == 2.0 and payments["billed_usd"] == 2.1
    assert payments["tag_inactive"] is False
    assert reports["tag_inactive"] is True
    assert result["tag_inactive_workloads"] == ["reports"]
    tag_calls = [c for c in ce.calls if any("Tags" in p for p in c["Filter"]["And"])]
    assert [c["Filter"]["And"][2]["Tags"] for c in tag_calls] == [
        {"Key": "bedrock-spend-controls-workload", "Values": ["payments"], "MatchOptions": ["EQUALS"]},
        {"Key": "bedrock-spend-controls-workload", "Values": ["reports"], "MatchOptions": ["EQUALS"]},
    ]
    emf = _emf(capsys)
    by_workload = {e["Workload"]: e for e in emf if e.get("Scope") == "workload"}
    assert by_workload["payments"]["ReconciliationTagInactive"] == 0
    assert by_workload["reports"]["ReconciliationTagInactive"] == 1
    assert by_workload["reports"]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Workload"]]
    assert len(sns.published) == 1
    assert "cost-allocation tag not active" in sns.published[0]["Subject"]
    assert json.loads(sns.published[0]["Message"])["workloads"] == ["reports"]


def test_workload_with_no_spend_and_no_bill_is_not_flagged(fake_dynamodb, monkeypatch):
    _env(monkeypatch, workloads={"workload:idle": {"name": "idle", "profile_arn": "a", "role_arn": ""}})
    result = reconciler.handler({"day": DAY}, None, dynamodb=fake_dynamodb, ce=FakeCostExplorer(), sns=FakeSNS())
    assert result["workloads"][0]["tag_inactive"] is False
    assert result["tag_inactive_workloads"] == []


def test_cost_explorer_failure_emits_failure_metric_and_raises(fake_dynamodb, monkeypatch, capsys):
    _env(monkeypatch)
    error = ClientError({"Error": {"Code": "DataUnavailableException", "Message": "not yet"}}, "GetCostAndUsage")
    sns = FakeSNS()
    with pytest.raises(ClientError):
        reconciler.handler({"day": DAY}, None, dynamodb=fake_dynamodb, ce=FakeCostExplorer(error=error), sns=sns)
    assert any(e.get("ReconciliationFailure") == 1 for e in _emf(capsys))
    assert "RECONCILIATION FAILED" in sns.published[0]["Subject"]
    assert "Item" not in fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": f"RECONCILE#{DAY}", "window": DAY}
    )


def test_service_names_can_be_overridden(fake_dynamodb, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("CE_SERVICE_NAMES_JSON", '["Amazon Bedrock", "Claude 3 (Amazon Bedrock Edition)"]')
    ce = FakeCostExplorer(aggregate=1.0)
    result = reconciler.handler({"day": DAY}, None, dynamodb=fake_dynamodb, ce=ce, sns=FakeSNS())
    assert result["service_names"] == ["Amazon Bedrock", "Claude 3 (Amazon Bedrock Edition)"]
    assert ce.calls[0]["Filter"]["And"][0]["Dimensions"]["Values"] == result["service_names"]
