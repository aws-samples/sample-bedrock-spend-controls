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
