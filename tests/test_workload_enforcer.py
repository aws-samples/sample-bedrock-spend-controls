"""Workload enforcement Lambda tests: idempotent IAM Deny convergence."""

import json
import os
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from workload_enforcer import handler as enforcer

ROLE_ARN = "arn:aws:iam::111122223333:role/payments-app"
USER_ARN = "arn:aws:iam::111122223333:user/report-api-key-user"
SCHEDULE_EVENT = {"source": "aws.events"}
STACK_NAME = "BedrockSpendControls"
# _configure pins AWS_REGION=us-east-1 and STACK_NAME.
POLICY_NAME = f"{enforcer.LEGACY_DENY_POLICY_NAME}-us-east-1-{STACK_NAME}"
LEGACY_NAME = enforcer.LEGACY_DENY_POLICY_NAME


def _stream_event(user_id: str) -> dict:
    return {
        "Records": [
            {"dynamodb": {"Keys": {"user_id": {"S": user_id}}}}
        ]
    }


def _no_such_entity(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchEntity", "Message": "missing"}},
        operation,
    )


class FakeIAM:
    def __init__(self, *, fail_put_roles: set[str] | None = None):
        self.role_policies: dict[tuple[str, str], dict] = {}
        self.user_policies: dict[tuple[str, str], dict] = {}
        self.put_role_calls: list[tuple[str, str]] = []
        self.put_user_calls: list[tuple[str, str]] = []
        self.delete_role_calls: list[tuple[str, str]] = []
        self.fail_put_roles = fail_put_roles or set()

    def get_role_policy(self, RoleName, PolicyName):  # noqa: N803
        key = (RoleName, PolicyName)
        if key not in self.role_policies:
            raise _no_such_entity("GetRolePolicy")
        return {"PolicyDocument": self.role_policies[key]}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):  # noqa: N803
        if RoleName in self.fail_put_roles:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "nope"}},
                "PutRolePolicy",
            )
        self.put_role_calls.append((RoleName, PolicyName))
        self.role_policies[(RoleName, PolicyName)] = json.loads(
            PolicyDocument
        )

    def delete_role_policy(self, RoleName, PolicyName):  # noqa: N803
        key = (RoleName, PolicyName)
        self.delete_role_calls.append(key)
        if key not in self.role_policies:
            raise _no_such_entity("DeleteRolePolicy")
        del self.role_policies[key]

    def get_user_policy(self, UserName, PolicyName):  # noqa: N803
        key = (UserName, PolicyName)
        if key not in self.user_policies:
            raise _no_such_entity("GetUserPolicy")
        return {"PolicyDocument": self.user_policies[key]}

    def put_user_policy(self, UserName, PolicyName, PolicyDocument):  # noqa: N803
        self.put_user_calls.append((UserName, PolicyName))
        self.user_policies[(UserName, PolicyName)] = json.loads(
            PolicyDocument
        )

    def delete_user_policy(self, UserName, PolicyName):  # noqa: N803
        key = (UserName, PolicyName)
        if key not in self.user_policies:
            raise _no_such_entity("DeleteUserPolicy")
        del self.user_policies[key]


def _configure(monkeypatch, workloads: dict) -> None:
    monkeypatch.setenv("WORKLOADS_JSON", json.dumps(workloads))
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:alerts")
    # The stack still passes the legacy fixed name; the handler must scope it.
    monkeypatch.setenv("DENY_POLICY_NAME", LEGACY_NAME)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("STACK_NAME", STACK_NAME)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


def _seed_workload_row(
    fake_dynamodb,
    workload_id: str,
    *,
    status: str = "active",
    status_origin: str = "automatic",
    status_reason: str = "",
    daily_usd_micro: int = 5_000_000,
    version: int = 3,
    **period_limits,
) -> None:
    fake_dynamodb.Table(os.environ["USERS_TABLE"]).put_item(
        Item={
            "user_id": workload_id,
            "name": workload_id.split(":", 1)[1],
            "status": status,
            "status_reason": status_reason,
            "status_origin": status_origin,
            "daily_usd_micro": daily_usd_micro,
            "daily_input_tokens": 0,
            "daily_output_tokens": 0,
            "version": version,
            **period_limits,
        }
    )


def _seed_usage(fake_dynamodb, workload_id: str, cost_micro: int) -> None:
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fake_dynamodb.Table(os.environ["USAGE_TABLE"]).put_item(
        Item={
            "user_id": workload_id,
            "window": window,
            "cost_micro": cost_micro,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 1,
        }
    )


def _run(fake_dynamodb, fake_sns, iam, event=SCHEDULE_EVENT):
    return enforcer.handler(
        event, None, dynamodb=fake_dynamodb, iam=iam, sns=fake_sns
    )


def test_blocked_workload_gets_the_inline_deny(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(
        fake_dynamodb,
        "workload:payments",
        status="blocked",
        status_reason="auto: quota exhausted in 2026-09-08",
    )
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=6_000_000)
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["attached"] == 1
    key = ("payments-app", POLICY_NAME)
    assert iam.role_policies[key]["Statement"][0]["Effect"] == "Deny"
    assert (
        "bedrock:InvokeModel"
        in iam.role_policies[key]["Statement"][0]["Action"]
    )

    repeat = _run(fake_dynamodb, fake_sns, iam)
    assert repeat["attached"] == 0
    assert repeat["unchanged"] == 1
    assert len(iam.put_role_calls) == 1  # idempotent: no rewrite


def test_active_workload_gets_residual_deny_removed(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(fake_dynamodb, "workload:payments", status="active")
    iam = FakeIAM()
    iam.role_policies[("payments-app", POLICY_NAME)] = (
        enforcer.deny_policy()
    )

    result = _run(fake_dynamodb, fake_sns, iam)
    assert result["detached"] == 1
    assert iam.role_policies == {}

    repeat = _run(fake_dynamodb, fake_sns, iam)
    assert repeat["detached"] == 0
    assert repeat["unchanged"] == 1  # NoSuchEntity tolerated


def test_automatic_block_lifts_when_window_resets(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    # Blocked yesterday; today's usage row does not exist => under budget.
    _seed_workload_row(
        fake_dynamodb,
        "workload:payments",
        status="blocked",
        status_origin="automatic",
    )
    iam = FakeIAM()
    iam.role_policies[("payments-app", POLICY_NAME)] = (
        enforcer.deny_policy()
    )

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["unblocked"] == 1
    assert result["detached"] == 1
    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["status"] == "active"
    assert row["status_reason"] == (
        "auto: current calendar periods are under quota"
    )
    assert row["version"] == 4


def test_daily_reset_does_not_lift_exhausted_monthly_workload(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(
        fake_dynamodb,
        "workload:payments",
        status="blocked",
        status_origin="automatic",
        status_reason="auto: monthly USD quota exhausted",
        monthly_limits_enabled=True,
        monthly_usd_micro=5_000_000,
        monthly_input_tokens=0,
        monthly_output_tokens=0,
    )
    now = datetime.now(timezone.utc)
    fake_dynamodb.Table(os.environ["USAGE_TABLE"]).put_item(
        Item={
            "user_id": "workload:payments",
            "window": now.replace(day=1).date().isoformat(),
            "cost_micro": 6_000_000,
            "requests": 1,
        }
    )
    iam = FakeIAM()
    iam.role_policies[("payments-app", POLICY_NAME)] = (
        enforcer.deny_policy()
    )

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["unblocked"] == 0
    assert result["blocked_workloads"] == 1
    row = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "workload:payments"}
    )["Item"]
    assert row["status"] == "blocked"
    assert row["version"] == 3


def test_manual_admin_block_never_auto_lifts(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(
        fake_dynamodb,
        "workload:payments",
        status="blocked",
        status_origin="admin",
        status_reason="incident freeze",
    )
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["unblocked"] == 0
    assert result["attached"] == 1
    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["status"] == "blocked"


def test_still_over_budget_stays_blocked(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(
        fake_dynamodb,
        "workload:payments",
        status="blocked",
        status_origin="automatic",
        daily_usd_micro=5_000_000,
    )
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=5_000_000)
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["unblocked"] == 0
    assert result["attached"] == 1


def test_blocked_without_role_is_skipped_and_alerted(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:reports": {"name": "reports", "role_arn": ""}},
    )
    _seed_workload_row(
        fake_dynamodb, "workload:reports", status="blocked"
    )
    _seed_usage(fake_dynamodb, "workload:reports", cost_micro=9_000_000)
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["skipped_not_ready"] == 1
    assert iam.put_role_calls == []
    assert any(
        "WITHOUT ENFORCEMENT" in message.get("Subject", "")
        for message in fake_sns.published
    )


def test_missing_row_means_active_and_detaches_residue(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    iam = FakeIAM()
    iam.role_policies[("payments-app", POLICY_NAME)] = (
        enforcer.deny_policy()
    )

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["detached"] == 1


def test_event_gating_ignores_non_workload_stream_records(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    iam = FakeIAM()

    ignored = _run(
        fake_dynamodb, fake_sns, iam, event=_stream_event("alice")
    )
    assert ignored == {
        "enforced": False,
        "reason": "no-workload-change",
    }

    processed = _run(
        fake_dynamodb,
        fake_sns,
        iam,
        event=_stream_event("workload:payments"),
    )
    assert processed["enforced"] is True


def test_iam_failure_alerts_raises_and_still_converges_others(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {
            "workload:broken": {
                "name": "broken",
                "role_arn": "arn:aws:iam::111122223333:role/broken-app",
            },
            "workload:payments": {
                "name": "payments",
                "role_arn": ROLE_ARN,
            },
        },
    )
    _seed_workload_row(fake_dynamodb, "workload:broken", status="blocked")
    _seed_usage(fake_dynamodb, "workload:broken", cost_micro=9_000_000)
    _seed_workload_row(
        fake_dynamodb, "workload:payments", status="blocked"
    )
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=9_000_000)
    iam = FakeIAM(fail_put_roles={"broken-app"})

    with pytest.raises(RuntimeError, match="1 workload"):
        _run(fake_dynamodb, fake_sns, iam)

    # The healthy workload converged despite the failure.
    assert ("payments-app", POLICY_NAME) in (
        iam.role_policies
    )
    assert any(
        "ENFORCEMENT FAILED" in message.get("Subject", "")
        for message in fake_sns.published
    )


def test_iam_user_principals_use_the_user_policy_variant(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Bedrock long-term API keys attach to IAM users (Phase 0 finding)."""
    _configure(
        monkeypatch,
        {"workload:reports": {"name": "reports", "role_arn": USER_ARN}},
    )
    _seed_workload_row(fake_dynamodb, "workload:reports", status="blocked")
    _seed_usage(fake_dynamodb, "workload:reports", cost_micro=9_000_000)
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["attached"] == 1
    assert iam.put_user_calls == [
        ("report-api-key-user", POLICY_NAME)
    ]
    assert iam.put_role_calls == []


def test_principal_parsing_handles_paths_and_rejects_garbage():
    assert enforcer._principal(
        "arn:aws:iam::1:role/service/teams/payments-app"
    ) == ("role", "payments-app")
    assert enforcer._principal("arn:aws:iam::1:user/x") == ("user", "x")
    with pytest.raises(ValueError):
        enforcer._principal("arn:aws:iam::1:group/admins")


def test_no_workloads_configured_is_a_noop(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("WORKLOADS_JSON", "{}")
    result = _run(fake_dynamodb, fake_sns, FakeIAM())
    assert result == {
        "enforced": False,
        "reason": "no-workloads-configured",
    }


def test_shared_role_blocked_and_active_keeps_deny(
    fake_dynamodb, fake_sns, monkeypatch
):
    """H-7: two workloads on one role; the active one must not strip the
    blocked one's Deny. The role converges once, from the union."""
    _configure(
        monkeypatch,
        {
            "workload:payments": {"name": "payments", "role_arn": ROLE_ARN},
            "workload:refunds": {"name": "refunds", "role_arn": ROLE_ARN},
        },
    )
    _seed_workload_row(fake_dynamodb, "workload:payments", status="blocked")
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=9_000_000)
    _seed_workload_row(fake_dynamodb, "workload:refunds", status="active")
    iam = FakeIAM()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["attached"] == 1
    assert result["detached"] == 0
    assert result["blocked_workloads"] == 1
    assert ("payments-app", POLICY_NAME) in iam.role_policies
    assert result["roles"] == [
        {
            "role_arn": ROLE_ARN,
            "workload_ids": ["workload:payments", "workload:refunds"],
            "blocked": True,
            "changed": True,
            "legacy_removed": False,
        }
    ]
    assert len(iam.put_role_calls) == 1  # one PutRolePolicy per principal

    repeat = _run(fake_dynamodb, fake_sns, iam)
    assert repeat["unchanged"] == 1
    assert ("payments-app", POLICY_NAME) in iam.role_policies
    assert iam.delete_role_calls == [
        ("payments-app", LEGACY_NAME),
        ("payments-app", LEGACY_NAME),
    ]

    # Only once no workload on the role is blocked does the Deny go away.
    _seed_workload_row(fake_dynamodb, "workload:payments", status="active")
    lifted = _run(fake_dynamodb, fake_sns, iam)
    assert lifted["detached"] == 1
    assert iam.role_policies == {}


def test_legacy_policy_name_is_migrated_to_the_scoped_name(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(fake_dynamodb, "workload:payments", status="blocked")
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=9_000_000)
    iam = FakeIAM()
    iam.role_policies[("payments-app", LEGACY_NAME)] = enforcer.deny_policy()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["attached"] == 1
    assert result["roles"][0]["legacy_removed"] is True
    assert list(iam.role_policies) == [("payments-app", POLICY_NAME)]
    # Attach first, then delete the legacy name: never a window with no Deny.
    assert iam.put_role_calls == [("payments-app", POLICY_NAME)]
    assert iam.delete_role_calls == [("payments-app", LEGACY_NAME)]


def test_legacy_policy_is_removed_when_converging_to_no_deny(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {"workload:payments": {"name": "payments", "role_arn": ROLE_ARN}},
    )
    _seed_workload_row(fake_dynamodb, "workload:payments", status="active")
    iam = FakeIAM()
    iam.role_policies[("payments-app", LEGACY_NAME)] = enforcer.deny_policy()

    result = _run(fake_dynamodb, fake_sns, iam)

    assert result["detached"] == 1
    assert result["roles"][0] == {
        "role_arn": ROLE_ARN,
        "workload_ids": ["workload:payments"],
        "blocked": False,
        "changed": False,
        "legacy_removed": True,
    }
    assert iam.role_policies == {}


def test_policy_name_is_scoped_per_stack_and_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("STACK_NAME", "Spend/Controls")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

    # The legacy fixed value is never used verbatim.
    monkeypatch.setenv("DENY_POLICY_NAME", LEGACY_NAME)
    assert enforcer.deny_policy_name() == (
        f"{LEGACY_NAME}-eu-west-1-Spend-Controls"
    )
    # An explicit, non-legacy name from the stack wins.
    monkeypatch.setenv("DENY_POLICY_NAME", "my-scoped-deny")
    assert enforcer.deny_policy_name() == "my-scoped-deny"
    # Without STACK_NAME the function name scopes the policy.
    monkeypatch.delenv("DENY_POLICY_NAME")
    monkeypatch.delenv("STACK_NAME")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "Stack-WorkloadEnforcerFn-ABC")
    assert enforcer.deny_policy_name() == (
        f"{LEGACY_NAME}-eu-west-1-Stack-WorkloadEnforcerFn-ABC"
    )
    # IAM caps inline policy names at 128 characters.
    monkeypatch.setenv("STACK_NAME", "x" * 200)
    assert len(enforcer.deny_policy_name()) == 128
    assert enforcer.deny_policy_name() != LEGACY_NAME


def test_row_read_failure_marks_unknown_keeps_deny_and_continues(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    """H-4: a DynamoDB error on one workload must not abort the others nor
    lift a Deny on a role whose status could not be read."""
    broken_role = "arn:aws:iam::111122223333:role/broken-app"
    _configure(
        monkeypatch,
        {
            "workload:broken": {"name": "broken", "role_arn": broken_role},
            "workload:payments": {"name": "payments", "role_arn": ROLE_ARN},
        },
    )
    _seed_workload_row(fake_dynamodb, "workload:broken", status="blocked")
    _seed_workload_row(fake_dynamodb, "workload:payments", status="blocked")
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=9_000_000)
    users_table = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    original_get_item = users_table.get_item

    def flaky_get_item(Key, **kwargs):  # noqa: N803
        if Key["user_id"] == "workload:broken":
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException",
                           "Message": "slow down"}},
                "GetItem",
            )
        return original_get_item(Key=Key, **kwargs)

    monkeypatch.setattr(users_table, "get_item", flaky_get_item)
    iam = FakeIAM()
    iam.role_policies[("broken-app", POLICY_NAME)] = enforcer.deny_policy()

    with pytest.raises(RuntimeError, match="1 workload"):
        _run(fake_dynamodb, fake_sns, iam)

    # The healthy workload converged; the unreadable one kept its Deny.
    assert ("payments-app", POLICY_NAME) in iam.role_policies
    assert ("broken-app", POLICY_NAME) in iam.role_policies
    assert iam.delete_role_calls == [("payments-app", LEGACY_NAME)]
    emf = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in line
    ]
    failure = next(e for e in emf if "WorkloadEnforcementFailure" in e)
    assert failure["WorkloadEnforcementFailure"] == 1
    assert failure["result"]["skipped_unknown_status"] == 1
    assert failure["failures"][0]["workload_id"] == "workload:broken"
    assert failure["failures"][0]["status"] == "unknown"
    assert any(
        "ENFORCEMENT FAILED" in message.get("Subject", "")
        for message in fake_sns.published
    )


def test_shared_role_attaches_even_when_a_sibling_row_is_unreadable(
    fake_dynamodb, fake_sns, monkeypatch
):
    _configure(
        monkeypatch,
        {
            "workload:payments": {"name": "payments", "role_arn": ROLE_ARN},
            "workload:refunds": {"name": "refunds", "role_arn": ROLE_ARN},
        },
    )
    _seed_workload_row(fake_dynamodb, "workload:payments", status="blocked")
    _seed_usage(fake_dynamodb, "workload:payments", cost_micro=9_000_000)
    users_table = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    original_get_item = users_table.get_item

    def flaky_get_item(Key, **kwargs):  # noqa: N803
        if Key["user_id"] == "workload:refunds":
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}},
                "GetItem",
            )
        return original_get_item(Key=Key, **kwargs)

    monkeypatch.setattr(users_table, "get_item", flaky_get_item)
    iam = FakeIAM()

    with pytest.raises(RuntimeError):
        _run(fake_dynamodb, fake_sns, iam)

    assert ("payments-app", POLICY_NAME) in iam.role_policies
