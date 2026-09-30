import json
import os
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from emergency_processor import handler as emergency


class FakeIAM:
    def __init__(self, arn, *, fail=False):
        self.arn = arn
        self.fail = fail
        self.default = "v1"
        self.versions = {
            "v1": {
                "Document": emergency.emergency_policy(False),
                "CreateDate": datetime(2030, 1, 1, tzinfo=timezone.utc),
            }
        }

    def get_policy(self, PolicyArn):  # noqa: N803
        assert PolicyArn == self.arn
        return {"Policy": {"DefaultVersionId": self.default}}

    def get_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        return {"PolicyVersion": self.versions[VersionId]}

    def list_policy_versions(self, PolicyArn):  # noqa: N803
        return {
            "Versions": [
                {
                    "VersionId": key,
                    "IsDefaultVersion": key == self.default,
                    "CreateDate": value["CreateDate"],
                }
                for key, value in self.versions.items()
            ]
        }

    def delete_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        del self.versions[VersionId]

    def create_policy_version(
        self, PolicyArn, PolicyDocument, SetAsDefault  # noqa: N803
    ):
        if self.fail:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "CreatePolicyVersion",
            )
        next_id = f"v{max(int(key[1:]) for key in self.versions) + 1}"
        self.versions[next_id] = {
            "Document": json.loads(PolicyDocument),
            "CreateDate": datetime.now(timezone.utc),
        }
        if SetAsDefault:
            self.default = next_id

    def current(self):
        return self.versions[self.default]["Document"]


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


def _event():
    return {
        "Records": [
            {
                "dynamodb": {
                    "Keys": {
                        "user_id": {"S": "CONFIG#EMERGENCY_STOP"}
                    }
                }
            }
        ]
    }


def _configure(monkeypatch):
    arn = "arn:aws:iam::111122223333:policy/emergency"
    monkeypatch.setenv("EMERGENCY_POLICY_ARN", arn)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:test")
    return arn


def _put_state(db, *, active):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={
            "user_id": "CONFIG#EMERGENCY_STOP",
            "state": "activating" if active else "recovering",
            "desired_active": active,
            "generation": 1,
            "actor": "admin@example.com",
            "reason": "exercise",
            "request_id": "request-1",
        }
    )


def test_active_policy_is_unconditional_and_inactive_is_noop():
    active = emergency.emergency_policy(True)["Statement"][0]
    inactive = emergency.emergency_policy(False)["Statement"][0]
    assert "Condition" not in active
    assert inactive["Condition"]["StringEquals"]["aws:SourceIdentity"] == [
        emergency._NO_EMERGENCY
    ]


def test_activation_applies_role_wide_deny_and_marks_state(
    fake_dynamodb, monkeypatch
):
    arn = _configure(monkeypatch)
    _put_state(fake_dynamodb, active=True)
    iam = FakeIAM(arn)
    sns = FakeSNS()

    result = emergency.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )

    assert result["desired_active"] is True
    assert "Condition" not in iam.current()["Statement"][0]
    state = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "CONFIG#EMERGENCY_STOP"}
    )["Item"]
    assert state["state"] == "active"
    assert state["applied_generation"] == 1
    assert "ACTIVE" in sns.published[0]["Subject"]

    applied_at = state["applied_at"]
    repeated = emergency.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )
    stable = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "CONFIG#EMERGENCY_STOP"}
    )["Item"]
    assert repeated["reason"] == "already-applied"
    assert repeated["applied"] is False
    assert stable["applied_at"] == applied_at
    assert len(sns.published) == 1


def test_recovery_removes_deny_before_marking_inactive(
    fake_dynamodb, monkeypatch
):
    arn = _configure(monkeypatch)
    _put_state(fake_dynamodb, active=False)
    iam = FakeIAM(arn)
    iam.versions["v1"]["Document"] = emergency.emergency_policy(True)

    emergency.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=FakeSNS()
    )

    assert "Condition" in iam.current()["Statement"][0]
    state = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "CONFIG#EMERGENCY_STOP"}
    )["Item"]
    assert state["state"] == "inactive"


def test_opposite_transition_converges_latest_generation_before_ack(
    fake_dynamodb, monkeypatch
):
    arn = _configure(monkeypatch)
    _put_state(fake_dynamodb, active=False)

    class RacingIAM(FakeIAM):
        def __init__(self, policy_arn):
            super().__init__(policy_arn)
            self.raced = False
            self.versions["v1"]["Document"] = emergency.emergency_policy(True)

        def create_policy_version(self, *args, **kwargs):
            result = super().create_policy_version(*args, **kwargs)
            if not self.raced:
                self.raced = True
                fake_dynamodb.Table(os.environ["USERS_TABLE"]).update_item(
                    Key={"user_id": "CONFIG#EMERGENCY_STOP"},
                    UpdateExpression=(
                        "SET desired_active = :desired, generation = :generation, #s = :state"
                    ),
                    ExpressionAttributeNames={"#s": "state"},
                    ExpressionAttributeValues={
                        ":desired": True,
                        ":generation": 2,
                        ":state": "activating",
                    },
                )
            return result

    iam = RacingIAM(arn)
    result = emergency.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=FakeSNS()
    )

    assert result["desired_active"] is True
    assert result["generation"] == 2
    assert "Condition" not in iam.current()["Statement"][0]
    state = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "CONFIG#EMERGENCY_STOP"}
    )["Item"]
    assert state["state"] == "active"


def test_iam_failure_keeps_gate_pending_and_retries(
    fake_dynamodb, monkeypatch
):
    arn = _configure(monkeypatch)
    _put_state(fake_dynamodb, active=True)
    sns = FakeSNS()

    with pytest.raises(ClientError, match="denied"):
        emergency.handler(
            _event(),
            None,
            dynamodb=fake_dynamodb,
            iam=FakeIAM(arn, fail=True),
            sns=sns,
        )

    state = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "CONFIG#EMERGENCY_STOP"}
    )["Item"]
    assert state["state"] == "activating"
    assert "FAILED" in sns.published[0]["Subject"]


def test_unrelated_stream_event_is_ignored(fake_dynamodb, monkeypatch):
    arn = _configure(monkeypatch)
    assert emergency.handler(
        {"Records": [{"dynamodb": {"Keys": {"user_id": {"S": "alice"}}}}]},
        None,
        dynamodb=fake_dynamodb,
        iam=FakeIAM(arn),
        sns=FakeSNS(),
    ) == {"applied": False, "reason": "no-emergency-change"}
