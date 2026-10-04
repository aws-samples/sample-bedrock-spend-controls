import json
from datetime import datetime, timezone

import pytest

from qualification.lease_revocation_probe import (
    BEDROCK_ACTIONS,
    ProbeConfig,
    ProbeResult,
    dry_run_report,
    lease_policy,
    role_account_and_name,
    source_identity_deny_policy,
    timing_summary,
    validate_probe_result,
)

ROLE_ARN = "arn:aws:iam::111122223333:role/quota-sandbox-role"
POLICY_ARN = "arn:aws:iam::111122223333:policy/quota-sandbox-revocation"


def _config(**overrides) -> ProbeConfig:
    values = {
        "role_arn": ROLE_ARN,
        "model_id": "provider.test-model",
        "region": "us-east-1",
    }
    values.update(overrides)
    return ProbeConfig(**values)


def test_lease_policy_is_compact_time_bounded_and_cannot_broaden_role():
    expires_at = datetime(2030, 1, 1, 12, 30, tzinfo=timezone.utc)

    document = lease_policy(expires_at)
    statement = document["Statement"][0]

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == list(BEDROCK_ACTIONS)
    assert statement["Resource"] == "*"
    assert statement["Condition"] == {
        "DateLessThan": {"aws:CurrentTime": "2030-01-01T12:30:00Z"}
    }
    assert len(json.dumps(document, separators=(",", ":"))) < 2_048


def test_lease_policy_requires_timezone_aware_deadline():
    with pytest.raises(ValueError, match="timezone-aware"):
        lease_policy(datetime(2030, 1, 1))


def test_source_identity_deny_is_targeted_and_deterministic():
    document = source_identity_deny_policy(["user-b", "user-a", "user-a"])
    statement = document["Statement"][0]

    assert statement["Effect"] == "Deny"
    assert statement["Action"] == list(BEDROCK_ACTIONS)
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceIdentity": ["user-a", "user-b"]}
    }
    with pytest.raises(ValueError, match="at least one"):
        source_identity_deny_policy([])


def test_live_probe_requires_exact_account_and_role_confirmation():
    with pytest.raises(ValueError, match="expected-account-id"):
        _config(execute=True).validate()
    with pytest.raises(ValueError, match="confirm-role-arn"):
        _config(execute=True, expected_account_id="111122223333").validate()

    with pytest.raises(ValueError, match="pre-attached managed policy"):
        _config(
            execute=True,
            expected_account_id="111122223333",
            confirm_role_arn=ROLE_ARN,
        ).validate()

    _config(
        execute=True,
        expected_account_id="111122223333",
        confirm_role_arn=ROLE_ARN,
        managed_policy_arn=POLICY_ARN,
        confirm_managed_policy_arn=POLICY_ARN,
    ).validate()


def test_dry_run_describes_all_mutating_operations_without_calling_aws():
    report = dry_run_report(_config())

    assert report["mode"] == "dry-run"
    assert report["target"]["account_id"] == "111122223333"
    assert report["target"]["role_name"] == "quota-sandbox-role"
    operations = " ".join(report["planned_operations"])
    assert "iam:CreatePolicyVersion" in operations
    assert "iam:SetDefaultPolicyVersion" in operations
    assert set(report["lease_policies"]) == {"60", "300"}
    assert all(
        value["compact_characters"] < 2_048
        for value in report["lease_policies"].values()
    )


def test_timing_summary_reports_absolute_sample_distribution():
    assert timing_summary([]) == {"samples": 0}
    assert timing_summary([1.0, 2.0, 3.0, 4.0, 10.0]) == {
        "samples": 5,
        "min_seconds": 1.0,
        "p50_seconds": 3.0,
        "p95_seconds": 10.0,
        "max_seconds": 10.0,
    }


def test_role_arn_validation_rejects_non_role_resources():
    assert role_account_and_name(ROLE_ARN) == (
        "111122223333",
        "quota-sandbox-role",
    )
    with pytest.raises(ValueError, match="IAM role ARN"):
        role_account_and_name("arn:aws:iam::111122223333:user/alice")


def test_probe_result_requires_pre_deadline_access_and_isolation():
    valid = ProbeResult(
        role_chaining_caller=True,
        one_hour_session_succeeded=True,
        above_one_hour_rejected=True,
        lease_results=[
            {
                "lease_seconds": 60,
                "allowed_before_deadline": True,
                "denied_after_deadline": True,
            }
        ],
        isolation_preserved=True,
    )
    validate_probe_result(valid)
    assert valid.passed

    denied_too_early = ProbeResult(
        role_chaining_caller=True,
        one_hour_session_succeeded=True,
        above_one_hour_rejected=True,
        lease_results=[
            {
                "lease_seconds": 60,
                "allowed_before_deadline": False,
                "denied_after_deadline": True,
            }
        ],
        isolation_preserved=True,
    )
    with pytest.raises(RuntimeError, match="lacked pre-deadline access"):
        validate_probe_result(denied_too_early)

    isolation_failed = ProbeResult(
        role_chaining_caller=True,
        one_hour_session_succeeded=True,
        above_one_hour_rejected=True,
        lease_results=[
            {
                "lease_seconds": 60,
                "allowed_before_deadline": True,
                "denied_after_deadline": True,
            }
        ],
        isolation_preserved=False,
    )
    with pytest.raises(RuntimeError, match="control identity"):
        validate_probe_result(isolation_failed)


# ---------------------------------------------------------------------------
# M-13: qualification tags, placeholder policy, version ownership, and the
# stack qualification script.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from qualification import lease_stack_qualification  # noqa: E402
from qualification.lease_revocation_probe import (  # noqa: E402
    PROBE_PLACEHOLDER_SID,
    QUALIFICATION_TAG_KEY,
    LiveProbe,
    check_policy_tags,
    check_qualification_tags,
    is_probe_placeholder,
    lease_session_seconds,
    probe_placeholder_policy,
    select_removable_probe_version,
)

QUALIFIED = [{"Key": QUALIFICATION_TAG_KEY, "Value": "true"}]


def _live_config(**overrides) -> ProbeConfig:
    return _config(
        execute=True,
        expected_account_id="111122223333",
        confirm_role_arn=ROLE_ARN,
        managed_policy_arn=POLICY_ARN,
        confirm_managed_policy_arn=POLICY_ARN,
        **overrides,
    )


def test_qualification_tag_is_required_on_roles_and_policies():
    check_qualification_tags(QUALIFIED, resource="role")
    check_qualification_tags([{"Key": QUALIFICATION_TAG_KEY, "Value": "TRUE"}], resource="role")
    with pytest.raises(RuntimeError, match="sandbox role is not tagged"):
        check_qualification_tags([], resource="role")
    with pytest.raises(RuntimeError, match="sandbox policy is not tagged"):
        check_qualification_tags(
            [{"Key": QUALIFICATION_TAG_KEY, "Value": "false"}], resource="policy"
        )
    with pytest.raises(RuntimeError, match="not tagged"):
        check_qualification_tags([{"Key": "Environment", "Value": "sandbox"}], resource="role")


def test_cloudformation_managed_policies_are_refused():
    check_policy_tags(QUALIFIED)
    with pytest.raises(RuntimeError, match="CloudFormation"):
        check_policy_tags(
            QUALIFIED + [{"Key": "aws:cloudformation:stack-name", "Value": "Prod"}]
        )
    # The stack guard fires before the tag guard: a stack-owned policy is
    # refused even when someone also tagged it for qualification.
    with pytest.raises(RuntimeError, match="CloudFormation"):
        check_policy_tags([{"Key": "aws:cloudformation:stack-id", "Value": "x"}])
    with pytest.raises(RuntimeError, match="sandbox policy is not tagged"):
        check_policy_tags([{"Key": "Environment", "Value": "sandbox"}])


def test_placeholder_policy_matches_nobody_and_is_recognised():
    document = probe_placeholder_policy()
    statement = document["Statement"][0]
    assert statement["Sid"] == PROBE_PLACEHOLDER_SID
    assert statement["Effect"] == "Deny"
    assert is_probe_placeholder(document)
    assert is_probe_placeholder(json.loads(json.dumps(document)))
    # IAM may return a single statement / string action instead of lists.
    assert is_probe_placeholder(
        {"Version": "2012-10-17", "Statement": {**statement, "Resource": ["*"]}}
    )
    assert not is_probe_placeholder(source_identity_deny_policy(["someone-real"]))
    assert not is_probe_placeholder({"Version": "2012-10-17", "Statement": []})
    assert not is_probe_placeholder(
        {"Version": "2012-10-17", "Statement": [{**statement, "Effect": "Allow"}]}
    )
    assert not is_probe_placeholder(
        {
            "Version": "2012-10-17",
            "Statement": [{**statement, "Condition": {"StringEquals": {"aws:SourceIdentity": "alice"}}}],
        }
    )


def test_version_room_only_removes_versions_the_probe_created():
    versions = [
        {"VersionId": "v1", "IsDefaultVersion": False, "CreateDate": 1},
        {"VersionId": "v2", "IsDefaultVersion": False, "CreateDate": 2},
        {"VersionId": "v3", "IsDefaultVersion": True, "CreateDate": 3},
        {"VersionId": "v4", "IsDefaultVersion": False, "CreateDate": 4},
        {"VersionId": "v5", "IsDefaultVersion": False, "CreateDate": 5},
    ]
    assert select_removable_probe_version(versions, ["v5", "v4"]) == "v4"
    assert select_removable_probe_version(versions, ["v3", "v5"]) == "v5"
    assert select_removable_probe_version(versions[:4], ["v1"]) is None  # room already
    with pytest.raises(RuntimeError, match="none of them were created by this probe"):
        select_removable_probe_version(versions, [])
    with pytest.raises(RuntimeError, match="none of them were created by this probe"):
        select_removable_probe_version(versions, ["v3"])  # only the default is ours


class _StubIam:
    def __init__(self, *, role_tags, policy_tags, document, attached=True):
        self.role_tags = role_tags
        self.policy_tags = policy_tags
        self.document = document
        self.attached = attached
        self.deleted: list[str] = []
        self.versions = [
            {"VersionId": "v1", "IsDefaultVersion": True, "CreateDate": 1},
            {"VersionId": "v2", "IsDefaultVersion": False, "CreateDate": 2},
            {"VersionId": "v3", "IsDefaultVersion": False, "CreateDate": 3},
            {"VersionId": "v4", "IsDefaultVersion": False, "CreateDate": 4},
            {"VersionId": "v5", "IsDefaultVersion": False, "CreateDate": 5},
        ]

    def list_entities_for_policy(self, **_):
        return {"PolicyRoles": [{"RoleName": "quota-sandbox-role"}] if self.attached else []}

    def get_policy(self, **_):
        return {"Policy": {"DefaultVersionId": "v1"}}

    def get_policy_version(self, **_):
        return {"PolicyVersion": {"Document": self.document}}

    def list_role_tags(self, **_):
        return {"Tags": self.role_tags}

    def list_policy_tags(self, **_):
        return {"Tags": self.policy_tags}

    def list_policy_versions(self, **_):
        return {"Versions": self.versions}

    def delete_policy_version(self, *, VersionId, **_):
        self.deleted.append(VersionId)


class _StubSts:
    pass


def test_live_probe_refuses_untagged_cfn_managed_or_non_placeholder_targets():
    placeholder = probe_placeholder_policy()
    kwargs = {"iam_client": None, "sts_client": _StubSts()}

    with pytest.raises(RuntimeError, match="sandbox role is not tagged"):
        kwargs["iam_client"] = _StubIam(role_tags=[], policy_tags=QUALIFIED, document=placeholder)
        LiveProbe(_live_config(), **kwargs)
    with pytest.raises(RuntimeError, match="sandbox policy is not tagged"):
        kwargs["iam_client"] = _StubIam(role_tags=QUALIFIED, policy_tags=[], document=placeholder)
        LiveProbe(_live_config(), **kwargs)
    with pytest.raises(RuntimeError, match="CloudFormation"):
        kwargs["iam_client"] = _StubIam(
            role_tags=QUALIFIED,
            policy_tags=QUALIFIED + [{"Key": "aws:cloudformation:stack-name", "Value": "Prod"}],
            document=placeholder,
        )
        LiveProbe(_live_config(), **kwargs)
    with pytest.raises(RuntimeError, match="placeholder"):
        kwargs["iam_client"] = _StubIam(
            role_tags=QUALIFIED,
            policy_tags=QUALIFIED,
            document=source_identity_deny_policy(["blocked-production-user"]),
        )
        LiveProbe(_live_config(), **kwargs)
    with pytest.raises(RuntimeError, match="not attached"):
        kwargs["iam_client"] = _StubIam(
            role_tags=QUALIFIED, policy_tags=QUALIFIED, document=placeholder, attached=False
        )
        LiveProbe(_live_config(), **kwargs)

    iam = _StubIam(role_tags=QUALIFIED, policy_tags=QUALIFIED, document=placeholder)
    probe = LiveProbe(_live_config(), iam_client=iam, sts_client=_StubSts())
    # Five pre-existing versions, none created by the probe: refuse to delete.
    with pytest.raises(RuntimeError, match="none of them were created by this probe"):
        probe._make_version_room()
    assert iam.deleted == []
    probe._probe_versions.append("v4")
    probe._make_version_room()
    assert iam.deleted == ["v4"]
    assert probe._probe_versions == []


def test_lease_durations_allow_900_and_keep_sts_keys_alive_past_the_deadline():
    _config(lease_seconds=(60, 300, 900)).validate()
    with pytest.raises(ValueError, match="60, 300, or 900"):
        _config(lease_seconds=(120,)).validate()
    assert lease_session_seconds(60) == 900
    assert lease_session_seconds(300) == 900
    assert lease_session_seconds(900) == 1200
    report = dry_run_report(_config(lease_seconds=(60, 300, 900)))
    assert set(report["lease_policies"]) == {"60", "300", "900"}
    operations = " ".join(report["planned_operations"])
    assert "iam:ListRoleTags" in operations and "iam:ListPolicyTags" in operations
    assert report["prerequisites"]["placeholder_policy_document"] == probe_placeholder_policy()
    assert QUALIFICATION_TAG_KEY in report["prerequisites"]["required_tag"]


# -- lease_stack_qualification.py --------------------------------------------


def test_stack_qualification_defaults_to_a_dry_run_plan():
    args = lease_stack_qualification._parser().parse_args(["--stack-name", "Demo"])
    assert args.live is False
    assert args.lease_seconds == 300
    plan = lease_stack_qualification.run(args)
    assert plan["mode"] == "dry-run"
    assert plan["accepted_effective_lease_seconds"] == [240, 305]
    assert "quota_row" in plan["cleanup"]
    assert "deleted if this run created it" in plan["cleanup"]["cognito_user"]

    live = lease_stack_qualification._parser().parse_args(["--stack-name", "Demo", "--live"])
    with pytest.raises(ValueError, match="--profile is required with --live"):
        lease_stack_qualification.run(live)
    with pytest.raises(SystemExit):
        lease_stack_qualification._parser().parse_args(["--lease-seconds", "120"])


def test_stack_qualification_lease_windows_follow_the_dial():
    assert lease_stack_qualification.expected_lease_window(60) == (30, 65)
    assert lease_stack_qualification.expected_lease_window(300) == (240, 305)
    assert lease_stack_qualification.expected_lease_window(900) == (840, 905)
    with pytest.raises(ValueError):
        lease_stack_qualification.expected_lease_window(120)


def test_stack_qualification_runs_as_a_script_from_the_repository_root():
    root = Path(lease_stack_qualification.__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "qualification/lease_stack_qualification.py",
         "--stack-name", "Demo", "--lease-seconds", "900", "--keep-user"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["accepted_effective_lease_seconds"] == [840, 905]
    assert plan["cleanup"]["cognito_user"] == "kept (--keep-user)"
