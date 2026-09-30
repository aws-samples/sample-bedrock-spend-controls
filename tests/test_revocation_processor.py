import json
import os
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from revocation_processor import handler as revoker


class FakeIAM:
    def __init__(self, policy_arns, *, fail_create=False):
        self.fail_create = fail_create
        self.policies = {
            arn: {
                "default": "v1",
                "versions": {
                    "v1": {
                        "Document": revoker.deny_policy([]),
                        "CreateDate": datetime(2030, 1, 1, tzinfo=timezone.utc),
                    }
                },
            }
            for arn in policy_arns
        }
        self.created = []
        self.deleted = []

    def get_policy(self, PolicyArn):  # noqa: N803
        return {"Policy": {"DefaultVersionId": self.policies[PolicyArn]["default"]}}

    def get_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        return {"PolicyVersion": self.policies[PolicyArn]["versions"][VersionId]}

    def list_policy_versions(self, PolicyArn):  # noqa: N803
        policy = self.policies[PolicyArn]
        return {
            "Versions": [
                {
                    "VersionId": version_id,
                    "IsDefaultVersion": version_id == policy["default"],
                    "CreateDate": value["CreateDate"],
                }
                for version_id, value in policy["versions"].items()
            ]
        }

    def delete_policy_version(self, PolicyArn, VersionId):  # noqa: N803
        self.deleted.append((PolicyArn, VersionId))
        del self.policies[PolicyArn]["versions"][VersionId]

    def create_policy_version(
        self, PolicyArn, PolicyDocument, SetAsDefault  # noqa: N803
    ):
        if self.fail_create:
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "slow down"}},
                "CreatePolicyVersion",
            )
        policy = self.policies[PolicyArn]
        next_id = f"v{max(int(key[1:]) for key in policy['versions']) + 1}"
        policy["versions"][next_id] = {
            "Document": json.loads(PolicyDocument),
            "CreateDate": datetime.now(timezone.utc),
        }
        if SetAsDefault:
            policy["default"] = next_id
        self.created.append((PolicyArn, json.loads(PolicyDocument)))
        return {"PolicyVersion": {"VersionId": next_id}}

    def current(self, arn):
        policy = self.policies[arn]
        return policy["versions"][policy["default"]]["Document"]


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


def _event(user_id="alice"):
    return {
        "Records": [
            {
                "eventSource": "aws:dynamodb",
                "dynamodb": {
                    "Keys": {"user_id": {"S": f"REVOCATION#{user_id}"}}
                },
            }
        ]
    }


def _seed_user(db, user_id, source_identity, status="blocked"):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={
            "user_id": user_id,
            "status": status,
            "source_identity": source_identity,
        }
    )


def _configure(monkeypatch, policy_arns, max_chars=6144):
    monkeypatch.setenv("REVOCATION_POLICY_ARNS_JSON", json.dumps(policy_arns))
    monkeypatch.setenv("REVOCATION_POLICY_MAX_CHARACTERS", str(max_chars))
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:test")


def _identities(document):
    return document["Statement"][0]["Condition"]["StringEquals"][
        "aws:SourceIdentity"
    ]


def test_deny_document_and_shard_mapping_are_deterministic():
    document = revoker.deny_policy(["b", "a", "a"])
    assert _identities(document) == ["a", "b"]
    assert revoker.shard_for("alice", 4) == revoker.shard_for("alice", 4)
    assert 0 <= revoker.shard_for("alice", 4) < 4


def test_status_event_reconciles_only_blocked_exact_source_identities(
    fake_dynamodb, monkeypatch, capsys
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns)
    _seed_user(fake_dynamodb, "alice", "alice-sanitized", "blocked")
    _seed_user(fake_dynamodb, "bob", "bob-sanitized", "active")
    fake_dynamodb.Table(os.environ["USERS_TABLE"]).put_item(
        Item={"user_id": "SESSION#ignored", "source_identity": "ignored", "status": "blocked"}
    )
    iam = FakeIAM(policy_arns)
    sns = FakeSNS()

    result = revoker.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )

    assert result["blocked_identities"] == 1
    assert _identities(iam.current(policy_arns[0])) == ["alice-sanitized"]
    assert result["updated_shards"] == 1
    emf = json.loads(capsys.readouterr().out)
    assert emf["RevocationSyncSuccess"] == 1


def test_unblock_removes_identity_and_repeated_delivery_is_idempotent(
    fake_dynamodb, monkeypatch
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns)
    _seed_user(fake_dynamodb, "alice", "alice-sanitized", "blocked")
    iam = FakeIAM(policy_arns)
    sns = FakeSNS()
    revoker.handler(_event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns)

    fake_dynamodb.Table(os.environ["USERS_TABLE"]).update_item(
        Key={"user_id": "alice"},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "active"},
    )
    removed = revoker.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )
    repeated = revoker.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )

    assert _identities(iam.current(policy_arns[0])) == [
        revoker._NO_BLOCKED_IDENTITY
    ]
    assert removed["updated_shards"] == 1
    assert repeated["unchanged_shards"] == 1


def test_identities_are_distributed_across_policy_shards(
    fake_dynamodb, monkeypatch
):
    policy_arns = [
        "arn:aws:iam::111122223333:policy/shard-0",
        "arn:aws:iam::111122223333:policy/shard-1",
    ]
    _configure(monkeypatch, policy_arns)
    identities = [f"identity-{index}" for index in range(20)]
    for index, identity in enumerate(identities):
        _seed_user(fake_dynamodb, f"user-{index}", identity)
    iam = FakeIAM(policy_arns)

    result = revoker.handler(
        {"source": "aws.events"},
        None,
        dynamodb=fake_dynamodb,
        iam=iam,
        sns=FakeSNS(),
    )

    applied = set()
    for arn in policy_arns:
        values = _identities(iam.current(arn))
        applied.update(value for value in values if value != revoker._NO_BLOCKED_IDENTITY)
    assert applied == set(identities)
    assert sum(shard["identities"] for shard in result["shards"]) == 20


def test_policy_overflow_fails_open_for_that_shard_and_alerts(
    fake_dynamodb, monkeypatch
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns, max_chars=300)
    for index in range(10):
        _seed_user(fake_dynamodb, f"user-{index}", "x" * 60 + str(index))
    iam = FakeIAM(policy_arns)
    iam.policies[policy_arns[0]]["versions"]["v1"]["Document"] = (
        revoker.deny_policy(["existing-blocked"])
    )
    sns = FakeSNS()

    result = revoker.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )

    assert result["overflow_shards"] == [0]
    assert _identities(iam.current(policy_arns[0])) == ["existing-blocked"]
    assert "CAPACITY EXCEEDED" in sns.published[0]["Subject"]


def test_iam_failure_alerts_and_raises_for_stream_retry(
    fake_dynamodb, monkeypatch
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns)
    _seed_user(fake_dynamodb, "alice", "alice-sanitized")
    iam = FakeIAM(policy_arns, fail_create=True)
    sns = FakeSNS()

    with pytest.raises(ClientError, match="slow down"):
        revoker.handler(
            _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
        )

    assert "SYNC FAILED" in sns.published[0]["Subject"]


def test_managed_policy_version_rotation_deletes_oldest_non_default(
    fake_dynamodb, monkeypatch
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns)
    _seed_user(fake_dynamodb, "alice", "alice-sanitized")
    iam = FakeIAM(policy_arns)
    policy = iam.policies[policy_arns[0]]
    for number in range(2, 6):
        policy["versions"][f"v{number}"] = {
            "Document": revoker.deny_policy([]),
            "CreateDate": datetime(2030, 1, number, tzinfo=timezone.utc),
        }
    policy["default"] = "v5"

    revoker.handler(
        _event(), None, dynamodb=fake_dynamodb, iam=iam, sns=FakeSNS()
    )

    assert iam.deleted == [(policy_arns[0], "v1")]
    assert _identities(iam.current(policy_arns[0])) == ["alice-sanitized"]


def test_unrelated_stream_records_are_ignored(
    fake_dynamodb, monkeypatch
):
    policy_arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, policy_arns)
    result = revoker.handler(
        {
            "Records": [
                {"dynamodb": {"Keys": {"user_id": {"S": "SESSION#x"}}}}
            ]
        },
        None,
        dynamodb=fake_dynamodb,
        iam=FakeIAM(policy_arns),
        sns=FakeSNS(),
    )
    assert result == {"reconciled": False, "reason": "no-status-change"}


def test_dispatch_source_triggers_reconciliation(fake_dynamodb, monkeypatch):
    """The enforcement dispatcher's async invoke must pass the gate."""
    arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, arns)
    iam = FakeIAM(arns)
    _seed_user(fake_dynamodb, "alice", "alice-session")

    result = revoker.handler(
        {"source": "enforcement-dispatch"},
        None,
        dynamodb=fake_dynamodb,
        iam=iam,
        sns=FakeSNS(),
    )

    assert result["reconciled"] is True
    assert _identities(iam.current(arns[0])) == ["alice-session"]


def test_block_then_unblock_race_converges_to_row_state(
    fake_dynamodb, monkeypatch
):
    """Race guarantee: the deny shards converge FROM the user row.

    The lease-refusal path and the revocation path never fight: both derive
    from the same row, whose writes are version-guarded. Block -> the shard
    gains the identity; window-reset unblock (refresh_auto_status flips the
    row and rewrites the sentinel) -> the next run strips it. Repeats are
    no-ops either way.
    """
    arns = ["arn:aws:iam::111122223333:policy/shard-0"]
    _configure(monkeypatch, arns)
    iam = FakeIAM(arns)
    sns = FakeSNS()
    _seed_user(fake_dynamodb, "alice", "alice-session", status="blocked")

    blocked_run = revoker.handler(
        _event("alice"), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )
    assert blocked_run["blocked_identities"] == 1
    assert _identities(iam.current(arns[0])) == ["alice-session"]

    # Window reset: the gateway's refresh_auto_status flips the row to
    # active and rewrites the REVOCATION# sentinel (fast path re-fires).
    _seed_user(fake_dynamodb, "alice", "alice-session", status="active")
    unblocked_run = revoker.handler(
        _event("alice"), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )
    assert unblocked_run["blocked_identities"] == 0
    assert _identities(iam.current(arns[0])) == [
        "__no_blocked_quota_identity__"
    ]

    # Convergence is idempotent: replaying either event changes nothing.
    replay = revoker.handler(
        _event("alice"), None, dynamodb=fake_dynamodb, iam=iam, sns=sns
    )
    assert replay["updated_shards"] == 0
