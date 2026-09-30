"""Enforcement dispatcher tests: single stream consumer, async fan-out."""

import json

from enforcement_dispatcher import handler as dispatcher


class FakeLambda:
    def __init__(self):
        self.invocations: list[tuple[str, str, dict]] = []

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        self.invocations.append(
            (FunctionName, InvocationType, json.loads(Payload))
        )
        return {"StatusCode": 202}


def _stream(user_ids):
    return {
        "Records": [
            {"dynamodb": {"Keys": {"user_id": {"S": user_id}}}}
            for user_id in user_ids
        ]
    }


def _configure(monkeypatch, *, revocation="rev-fn", workload="wl-fn"):
    monkeypatch.setenv("REVOCATION_FUNCTION_NAME", revocation)
    monkeypatch.setenv("WORKLOAD_ENFORCER_FUNCTION_NAME", workload)


def test_revocation_sentinels_dispatch_the_revocation_processor(monkeypatch):
    _configure(monkeypatch)
    fake = FakeLambda()

    result = dispatcher.handler(
        _stream(["REVOCATION#alice"]), None, lambda_client=fake
    )

    assert result["dispatched"] == ["revocation"]
    assert fake.invocations == [
        ("rev-fn", "Event", {"source": "enforcement-dispatch"})
    ]


def test_workload_rows_dispatch_the_workload_enforcer(monkeypatch):
    _configure(monkeypatch)
    fake = FakeLambda()

    result = dispatcher.handler(
        _stream(["workload:payments"]), None, lambda_client=fake
    )

    assert result["dispatched"] == ["workload"]
    assert fake.invocations[0][0] == "wl-fn"


def test_mixed_batch_dispatches_each_processor_once(monkeypatch):
    _configure(monkeypatch)
    fake = FakeLambda()

    result = dispatcher.handler(
        _stream(
            [
                "REVOCATION#alice",
                "REVOCATION#bob",
                "workload:payments",
                "workload:reports",
            ]
        ),
        None,
        lambda_client=fake,
    )

    assert result["dispatched"] == ["revocation", "workload"]
    assert len(fake.invocations) == 2  # once per processor, not per record


def test_workload_dispatch_skipped_when_not_configured(monkeypatch):
    _configure(monkeypatch, workload="")
    fake = FakeLambda()

    result = dispatcher.handler(
        _stream(["workload:payments", "REVOCATION#alice"]),
        None,
        lambda_client=fake,
    )

    assert result["dispatched"] == ["revocation"]
    assert len(fake.invocations) == 1


def test_unrelated_keys_dispatch_nothing(monkeypatch):
    _configure(monkeypatch)
    fake = FakeLambda()

    result = dispatcher.handler(
        _stream(["alice", "SESSION#abc", "CONFIG#ENFORCEMENT"]),
        None,
        lambda_client=fake,
    )

    assert result["dispatched"] == []
    assert fake.invocations == []
