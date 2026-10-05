"""setup.py / tools.wizard: the configuration wizard asks every key from
KEY_DOCS, validates answers with the validator cdk synth uses, writes a
deployment file that the shipped profiles would produce, replays saved
answers without prompts, and only ever prints the install command.

Offline: ``builtins.input`` is replaced by a script keyed on the prompt
label, live checks are disabled or driven by a fake preflight runner and a
fake Bedrock client, and ``--deploy`` runs against a fake install.sh.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cdk.stacks.configuration import (  # noqa: E402
    DEFAULTS,
    KEY_DOCS,
    KNOWN_KEYS,
    validate_mapping,
)
from tools.preflight import CheckResult, Context, Report  # noqa: E402
from tools.wizard import deploy as deploy_module  # noqa: E402
from tools.wizard import flow, live, summary  # noqa: E402
from tools.wizard.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main  # noqa: E402
from tools.wizard.console import Console  # noqa: E402

CONFIG_DIR = ROOT / "cdk" / "config"
DEMO = json.loads((CONFIG_DIR / "demo.json").read_text(encoding="utf-8"))
PRODUCTION = json.loads((CONFIG_DIR / "production.json").read_text(encoding="utf-8"))
ACCOUNT = "123456789012"
REGION = "us-east-1"
PYTHON = sys.executable

PRODUCTION_ANSWERS = {
    "jwt_issuer": "https://login.corp.test",
    "allowed_model_arns": "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
    "invoker_principal_arns": "arn:aws:iam::123456789012:role/BrokerInvoker",
    "alert_email": "alerts@corp.test",
}

_LABEL = re.compile(r"^\s*(?P<label>.+?)(?: \[[^\]]*\]| \(example: .*\))?: $")


def label_of(prompt: str) -> str:
    match = _LABEL.match(prompt)
    return match.group("label") if match else prompt.strip()


class Script:
    """Scripted answers keyed by prompt label. A string answers every time
    the label comes up; a list is consumed in order and then falls back to
    Enter (the default). Labels not listed get Enter."""

    def __init__(self, answers: dict[str, str | list[str]] | None = None, *, limit: int = 300):
        self.answers = {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in (answers or {}).items()
        }
        self.limit = limit
        self.prompts: list[str] = []

    @property
    def labels(self) -> list[str]:
        return [label_of(prompt) for prompt in self.prompts]

    def __call__(self, prompt: str) -> str:
        print(prompt, end="")  # what input() does with a real terminal
        self.prompts.append(prompt)
        if len(self.prompts) > self.limit:
            raise AssertionError(f"too many prompts; last: {self.prompts[-5:]}")
        queue = self.answers.get(label_of(prompt))
        if isinstance(queue, list):
            return queue.pop(0) if queue else ""
        return queue if queue is not None else ""


def never_asks(prompt: str) -> str:
    raise AssertionError(f"unexpected prompt {prompt!r}")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An output directory shaped like cdk/config/ (model-pricing.json
    resolves relative to the deployment file)."""
    directory = tmp_path / "config"
    directory.mkdir()
    shutil.copy(CONFIG_DIR / "model-pricing.json", directory / "model-pricing.json")
    return directory


def run_wizard(
    monkeypatch,
    capsys,
    config_dir: Path,
    answers: dict | None = None,
    *extra: str,
    template: str = "demo",
    output_name: str | None = None,
    live_checks: bool = False,
):
    script = Script(answers)
    monkeypatch.setattr("builtins.input", script)
    output = config_dir / (output_name or f"{template}.local.json")
    argv = ["--profile-template", template, "--output", str(output), *extra]
    if not live_checks:
        argv.append("--no-live-checks")
    code = main(argv)
    captured = capsys.readouterr()
    data = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
    return code, captured, data, script


# --- demo and production templates -----------------------------------------------


def test_demo_defaults_reproduce_the_shipped_profile(monkeypatch, capsys, config_dir):
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir, {"alert_email": "ops@corp.test"}
    )
    assert code == EXIT_OK, captured.err
    assert data == {**DEMO, "alert_email": "ops@corp.test"}
    assert validate_mapping(data, base_dir=config_dir, account=ACCOUNT) == []
    text = (config_dir / "demo.local.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "admin_jwt_claim"') and text.endswith("}\n")
    assert list(data) == sorted(data)

    out = captured.out
    assert "Live checks: off." in out
    assert "== Identity provider (1/11) ==" in out and "== Operations (11/11) ==" in out
    # Non-advanced keys of every section were asked; advanced ones were not.
    asked = set(script.labels)
    advanced = {doc.name for doc in KEY_DOCS if doc.advanced}
    assert not asked & advanced
    for name in ("jwt_issuer", "admin_ui", "manage_invocation_logging", "allowed_model_arns",
                 "invoker_principal_arns", "warn_threshold", "permission_lease_seconds",
                 "usage_retention_days", "alert_email", "reconciliation_enabled"):
        assert name in asked
    # Keys that only apply with a bring-your-own issuer or unmanaged logging are skipped.
    assert not asked & {"jwt_audience", "jwt_jwks_url", "admin_ui_client_id", "invocation_log_group_name"}
    assert "Configure advanced settings for enforcement?" in asked
    assert "Wrote" in out and "alert_email" in out and "(changed from template)" in out


def test_production_requires_issuer_and_audience(monkeypatch, capsys, config_dir):
    answers = {
        **PRODUCTION_ANSWERS,
        # Enter on the example issuer is refused; '-' clears the audience,
        # which the validator rejects, so the identity group is asked again.
        "jwt_issuer": ["", "https://login.corp.test"],
        "jwt_audience": ["-", "spend-controls"],
    }
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir, answers, template="production"
    )
    assert code == EXIT_OK, captured.err
    out = captured.out
    assert "jwt_issuer (example: https://idp.example.com)" in out
    assert "allowed_model_arns (example: arn:aws:bedrock:*:111122223333:inference-profile/us.anthropic.claude-sonnet-5, ...)" in out
    assert "A value is required here." in out
    assert "Error: jwt_audience is required with a bring-your-own jwt_issuer" in out
    assert script.labels.count("jwt_issuer") == 3 and script.labels.count("jwt_audience") == 2
    assert data["jwt_issuer"] == "https://login.corp.test"
    assert data["jwt_audience"] == "spend-controls"
    assert data["allowed_model_arns"] == [PRODUCTION_ANSWERS["allowed_model_arns"]]
    assert data["invoker_principal_arns"] == [PRODUCTION_ANSWERS["invoker_principal_arns"]]
    assert data["alert_email"] == "alerts@corp.test"
    assert data["manage_invocation_logging"] is False
    assert data["invocation_log_group_name"] == PRODUCTION["invocation_log_group_name"]
    assert validate_mapping(data, base_dir=config_dir, account=ACCOUNT) == []
    # Everything else is the production profile, unchanged.
    for key, value in PRODUCTION.items():
        if key not in ("jwt_issuer", "jwt_audience", "allowed_model_arns",
                       "invoker_principal_arns", "alert_email"):
            assert data[key] == value, key


def test_production_example_values_cannot_be_kept_with_yes(monkeypatch, capsys, config_dir):
    monkeypatch.setattr("builtins.input", never_asks)
    code, captured, data, _ = run_wizard(
        monkeypatch, capsys, config_dir, None, "--yes", template="production"
    )
    assert code == EXIT_FAILED and data is None
    assert "no answer for 'jwt_issuer'" in captured.err


def test_invalid_integer_is_asked_again_with_the_validator_message(monkeypatch, capsys, config_dir):
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir, {"usage_retention_days": ["abc", "10", "40"]}
    )
    assert code == EXIT_OK, captured.err
    assert "Expected an integer, got 'abc'." in captured.out
    assert "Error: usage_retention_days must be at least 31" in captured.out
    assert script.labels.count("usage_retention_days") == 3
    assert data["usage_retention_days"] == 40


def test_rejected_answer_is_not_offered_as_the_retry_default(monkeypatch, capsys, config_dir):
    # A value the validator rejects must not replace the suggestion shown
    # in brackets on the next attempt; Enter then keeps the template value.
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir,
        {"allowed_model_arns": ["not-an-arn", ""], "alert_email": "ops@example.org"},
    )
    assert code == EXIT_OK, captured.err
    prompts = [p for p in script.prompts if label_of(p) == "allowed_model_arns"]
    assert len(prompts) == 2
    assert "[*]" in prompts[1] and "not-an-arn" not in prompts[1]
    assert data["allowed_model_arns"] == ["*"]


def test_advanced_section_opens_on_request(monkeypatch, capsys, config_dir):
    answers = {
        "Configure advanced settings for enforcement?": "y",
        "vended_ttl_seconds": "900",
        "reserve_enforcement_concurrency": "n",
    }
    code, captured, data, script = run_wizard(monkeypatch, capsys, config_dir, answers)
    assert code == EXIT_OK, captured.err
    assert {"vended_ttl_seconds", "refresh_overlap_seconds", "reserve_enforcement_concurrency"} <= set(script.labels)
    assert "log_retention_days" not in script.labels  # other sections stay closed
    assert data["vended_ttl_seconds"] == 900
    assert data["reserve_enforcement_concurrency"] is False
    assert "reserve_enforcement_concurrency" in captured.out


def test_default_limits_object_prompt(monkeypatch, capsys, config_dir):
    answers = {
        "Change the default limits?": "y",
        "daily usd": "5",
        "daily input_tokens": "2000000",
        # First attempt is rejected by the validator (not increasing), then fixed.
        "daily thresholds": ["100:block,50:warn", "50:warn,80:warn,100:block"],
        "Enable weekly limits?": "y",
        "weekly usd": "0",
        "weekly input_tokens": "50000000",
        "weekly output_tokens": "10000000",
    }
    code, captured, data, script = run_wizard(monkeypatch, capsys, config_dir, answers)
    assert code == EXIT_OK, captured.err
    assert "0 means unlimited" in captured.out
    assert "strictly increasing" in captured.out
    assert script.labels.count("daily thresholds") == 2
    assert "Enable monthly limits?" in script.labels
    assert data["default_limits"] == {
        "daily": {
            "usd": 5.0,
            "input_tokens": 2000000,
            "output_tokens": 200000,
            "thresholds": [
                {"at": 0.5, "action": "warn"},
                {"at": 0.8, "action": "warn"},
                {"at": 1.0, "action": "block"},
            ],
        },
        "weekly": {"usd": 0.0, "input_tokens": 50000000, "output_tokens": 10000000},
        "monthly": None,
    }
    assert validate_mapping(data, base_dir=config_dir) == []


def test_edit_mode_takes_defaults_from_the_existing_output(monkeypatch, capsys, config_dir):
    output = config_dir / "demo.local.json"
    output.write_text(json.dumps({**DEMO, "usage_retention_days": 60, "alert_email": "ops@corp.test"}))
    code, captured, data, _ = run_wizard(monkeypatch, capsys, config_dir, {})
    assert code == EXIT_OK, captured.err
    assert "editing" in captured.out and "usage_retention_days [60]" in captured.out
    assert data["usage_retention_days"] == 60 and data["alert_email"] == "ops@corp.test"
    assert "(changed from template)" not in captured.out


def test_end_of_input_aborts_without_writing(monkeypatch, capsys, config_dir):
    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    output = config_dir / "demo.local.json"
    code = main(["--output", str(output), "--no-live-checks"])
    assert code == EXIT_FAILED
    assert "aborted (end of input)" in capsys.readouterr().err
    assert not output.exists()


# --- workloads -----------------------------------------------------------------------


def test_workloads_are_written_next_to_the_output(monkeypatch, capsys, config_dir):
    answers = {
        "Configure workloads?": "y",
        "Add a workload?": ["y", "y"],
        "workload name": ["Bad Name", "payments-batch"],
        "workload model (foundation-model or inference-profile ID)": "us.anthropic.claude-opus-4-7",
        "workload role_arn (the application's IAM role; empty = metered and alerted only)":
            f"arn:aws:iam::{ACCOUNT}:role/payments-batch-app",
    }
    code, captured, data, _ = run_wizard(monkeypatch, capsys, config_dir, answers)
    assert code == EXIT_OK, captured.err
    assert "Error: workloads[0].name must match" in captured.out
    assert data["workloads"] == "workloads.json"
    roster = json.loads((config_dir / "workloads.json").read_text(encoding="utf-8"))
    assert roster == {
        "workloads": [
            {
                "name": "payments-batch",
                "model": "us.anthropic.claude-opus-4-7",
                "role_arn": f"arn:aws:iam::{ACCOUNT}:role/payments-batch-app",
            }
        ]
    }
    assert validate_mapping(data, base_dir=config_dir, account=ACCOUNT) == []
    assert "Wrote" in captured.out and str(config_dir / "workloads.json") in captured.out
    assert "Workload owners" in captured.out
    assert f"arn:aws:iam::{ACCOUNT}:role/payments-batch-app" in captured.out  # in the SCP exception


def test_existing_roster_can_be_kept_or_dropped(monkeypatch, capsys, config_dir):
    (config_dir / "workloads.json").write_text(json.dumps({
        "workloads": [
            {"name": "keep-me", "model": "anthropic.claude-haiku-4-5-20251001-v1:0"},
            {"name": "drop-me", "model": "anthropic.claude-haiku-4-5-20251001-v1:0"},
        ]
    }))
    output = config_dir / "demo.local.json"
    output.write_text(json.dumps({**DEMO, "workloads": "workloads.json"}))
    answers = {
        "Keep workload 'keep-me' (anthropic.claude-haiku-4-5-20251001-v1:0)?": "y",
        "Keep workload 'drop-me' (anthropic.claude-haiku-4-5-20251001-v1:0)?": "n",
    }
    code, captured, data, script = run_wizard(monkeypatch, capsys, config_dir, answers)
    assert code == EXIT_OK, captured.err
    assert "Configure workloads?" in script.labels  # default yes: the roster exists
    roster = json.loads((config_dir / "workloads.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in roster["workloads"]] == ["keep-me"]
    assert data["workloads"] == "workloads.json"


# --- answers files ----------------------------------------------------------------


def test_answers_replay_with_yes_needs_no_input(monkeypatch, capsys, config_dir, tmp_path):
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({
        "alert_email": "ops@corp.test",
        "usage_retention_days": "60",  # strings are coerced to the key's type
        "retain_tables_on_delete": "true",
        "workloads": {"workloads": [{"name": "batch", "model": "anthropic.claude-sonnet-5"}]},
    }))
    monkeypatch.setattr("builtins.input", never_asks)
    output = config_dir / "demo.local.json"
    code = main(["--output", str(output), "--answers", str(answers_file), "--yes", "--no-live-checks"])
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["usage_retention_days"] == 60 and data["retain_tables_on_delete"] is True
    assert data["alert_email"] == "ops@corp.test" and data["workloads"] == "workloads.json"
    assert json.loads((config_dir / "workloads.json").read_text())["workloads"][0]["name"] == "batch"
    assert "Summary" in captured.out


def test_answers_with_unknown_keys_or_bad_values_are_usage_errors(monkeypatch, capsys, config_dir, tmp_path):
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"colour": "red", "alert_email": "x@corp.test"}))
    output = config_dir / "demo.local.json"
    assert main(["--output", str(output), "--answers", str(answers_file), "--yes", "--no-live-checks"]) == EXIT_USAGE
    assert "unknown deployment keys: colour" in capsys.readouterr().err

    answers_file.write_text(json.dumps({"usage_retention_days": "lots"}))
    assert main(["--output", str(output), "--answers", str(answers_file), "--yes", "--no-live-checks"]) == EXIT_USAGE
    assert "usage_retention_days: Expected an integer" in capsys.readouterr().err

    answers_file.write_text("[]")
    assert main(["--output", str(output), "--answers", str(answers_file), "--yes", "--no-live-checks"]) == EXIT_USAGE
    assert "JSON object" in capsys.readouterr().err
    assert not output.exists()


def test_rejected_answers_fail_a_yes_run(monkeypatch, capsys, config_dir, tmp_path):
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"usage_retention_days": 10}))
    monkeypatch.setattr("builtins.input", never_asks)
    output = config_dir / "demo.local.json"
    code = main(["--output", str(output), "--answers", str(answers_file), "--yes", "--no-live-checks"])
    assert code == EXIT_FAILED
    assert "usage_retention_days must be at least 31" in capsys.readouterr().err
    assert not output.exists()


def test_save_answers_round_trip(monkeypatch, capsys, config_dir, tmp_path):
    saved = tmp_path / "saved.json"
    answers = {
        "alert_email": "ops@corp.test",
        "usage_retention_days": "45",
        "Configure advanced settings for retention?": "y",
        "log_retention_days": "30",
        "Configure workloads?": "y",
        "Add a workload?": ["y"],
        "workload name": "reports",
        "workload model (foundation-model or inference-profile ID)": "anthropic.claude-haiku-4-5-20251001-v1:0",
    }
    code, captured, first, _ = run_wizard(
        monkeypatch, capsys, config_dir, answers, "--save-answers", str(saved)
    )
    assert code == EXIT_OK, captured.err
    assert "Saved answers to" in captured.out
    recorded = json.loads(saved.read_text(encoding="utf-8"))
    assert recorded["usage_retention_days"] == 45 and recorded["log_retention_days"] == 30
    assert recorded["workloads"] == {
        "workloads": [{"name": "reports", "model": "anthropic.claude-haiku-4-5-20251001-v1:0"}]
    }
    assert set(recorded) <= KNOWN_KEYS

    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    shutil.copy(CONFIG_DIR / "model-pricing.json", replay_dir / "model-pricing.json")
    monkeypatch.setattr("builtins.input", never_asks)
    output = replay_dir / "demo.local.json"
    code = main(["--output", str(output), "--answers", str(saved), "--yes", "--no-live-checks"])
    assert code == EXIT_OK, capsys.readouterr().err
    second = json.loads(output.read_text(encoding="utf-8"))
    assert second == first
    assert json.loads((replay_dir / "workloads.json").read_text()) == recorded["workloads"]


# --- summary --------------------------------------------------------------------------


def test_summary_lists_tasks_for_other_teams_conditionally(monkeypatch, capsys, config_dir):
    code, captured, _, _ = run_wizard(monkeypatch, capsys, config_dir, {"alert_email": "ops@corp.test"})
    assert code == EXIT_OK
    out = captured.out
    assert "Tasks for other teams" in out
    assert "1. Organization or security team: prevent bypass" in out
    assert '"Sid": "RequireQuotaBrokerForBedrockRuntime"' in out
    assert "register the console redirect URI" not in out
    assert "allow calls to the broker" not in out
    assert "Estimated monthly cost" in out and "USD/month for scenario 'Demo" in out

    answers = {
        **PRODUCTION_ANSWERS,
        "admin_ui": "y",
        "admin_jwt_claim": "groups",
        "admin_jwt_value": "bedrock-admins",
        "admin_ui_client_id": "spa-client",
    }
    code, captured, data, _ = run_wizard(
        monkeypatch, capsys, config_dir, answers, template="production"
    )
    assert code == EXIT_OK, captured.err
    out = captured.out
    assert data["admin_ui"] is True and data["admin_ui_client_id"] == "spa-client"
    assert "Identity provider team: register the console redirect URI" in out
    assert "AdminUiCallbackUrl" in out and "'spa-client'" in out
    assert "Identity provider team: token claims" in out and "'groups' containing 'bedrock-admins'" in out
    assert "Owners of the invoking roles: allow calls to the broker" in out
    assert PRODUCTION_ANSWERS["invoker_principal_arns"] in out
    assert "USD/month for scenario 'Production" in out


def test_scp_comes_from_deployment_md_with_workload_roles_added():
    document = summary.scp_document(ROOT)
    statement = document["Statement"][0]
    assert statement["Sid"] == "RequireQuotaBrokerForBedrockRuntime"
    assert statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] == summary.SCP_ROLE_PLACEHOLDER
    assert document == summary.FALLBACK_SCP  # the embedded copy matches the document

    role = f"arn:aws:iam::{ACCOUNT}:role/payments"
    document = summary.scp_document(ROOT, extra_principals=[role])
    assert document["Statement"][0]["Condition"]["ArnNotEquals"]["aws:PrincipalArn"] == [
        summary.SCP_ROLE_PLACEHOLDER, role,
    ]
    # Without DEPLOYMENT.md the embedded copy is used.
    assert summary.scp_document(ROOT / "nowhere") == summary.FALLBACK_SCP


def test_tasks_mention_models_the_live_check_flagged():
    tasks = summary.tasks_for_other_teams(
        {**DEMO, "jwt_issuer": ""}, region=REGION,
        flagged_models=["anthropic.claude-sonnet-5", "inference profile us.anthropic.claude-opus-4-7"],
    )
    titles = [title for title, _ in tasks]
    assert "Bedrock account owner: enable model access" in titles
    body = dict(tasks)["Bedrock account owner: enable model access"]
    assert "anthropic.claude-sonnet-5" in body and "us-east-1" in body


# --- --deploy ----------------------------------------------------------------------------


def ok_report(*names: str) -> Report:
    return Report(
        results=[CheckResult("pass", name, name, "fine") for name in names or ("toolchain",)],
        account=ACCOUNT, region=REGION,
    )


@pytest.fixture
def fake_install(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """An install.sh in a temporary repository root that records its arguments."""
    root = tmp_path / "root"
    root.mkdir()
    marker = root / "ran.txt"
    script = root / "install.sh"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {marker}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(deploy_module, "find_install_script", lambda root=None: script)
    return script, marker


def test_deploy_prints_the_command_and_waits_for_confirmation(monkeypatch, capsys, config_dir, fake_install):
    script, marker = fake_install
    monkeypatch.setattr(deploy_module, "run_for_config", lambda *args, **kwargs: ok_report())
    code, captured, data, answers = run_wizard(
        monkeypatch, capsys, config_dir, {"Run it now?": "n"},
        "--deploy", "--profile", "prod-admin", "--region", REGION,
    )
    assert code == EXIT_OK, captured.err
    assert data is not None
    out = captured.out
    assert "Preflight checks on the written configuration" in out and "Summary: 1 passed" in out
    expected = (
        f"{script} --config {config_dir / 'demo.local.json'} --profile prod-admin --region {REGION}"
    )
    assert f"Deploy command:\n  {expected}" in out
    assert "Not deployed; run the command above when ready." in out
    assert "Run it now?" in answers.labels
    assert not marker.exists()


def test_deploy_with_yes_runs_install_sh(monkeypatch, capsys, config_dir, tmp_path, fake_install):
    script, marker = fake_install
    monkeypatch.setattr(deploy_module, "run_for_config", lambda *args, **kwargs: ok_report())
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"alert_email": "ops@corp.test"}))
    monkeypatch.setattr("builtins.input", never_asks)
    output = config_dir / "demo.local.json"
    code = main(["--output", str(output), "--answers", str(answers_file), "--yes", "--deploy",
                 "--no-live-checks", "--region", REGION])
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    assert marker.read_text().split("\n")[:5] == ["--config", str(output), "--region", REGION, "--yes"]


def test_deploy_stops_when_preflight_fails(monkeypatch, capsys, config_dir, fake_install):
    _, marker = fake_install
    failed = Report(
        results=[CheckResult("fail", "bootstrap", "CDK bootstrap", "missing", "run cdk bootstrap")],
        account=ACCOUNT, region=REGION,
    )
    monkeypatch.setattr(deploy_module, "run_for_config", lambda *args, **kwargs: failed)
    code, captured, data, _ = run_wizard(monkeypatch, capsys, config_dir, {}, "--deploy")
    assert code == EXIT_FAILED and data is not None  # the file is written, the deploy is not
    assert "Preflight failed" in captured.out and "Deploy command" not in captured.out
    assert not marker.exists()


def test_deploy_without_install_sh_prints_the_equivalent_commands(monkeypatch, capsys, config_dir):
    monkeypatch.setattr(deploy_module, "find_install_script", lambda root=None: None)
    monkeypatch.setattr(deploy_module, "run_for_config", lambda *args, **kwargs: ok_report())
    code, captured, _, _ = run_wizard(
        monkeypatch, capsys, config_dir, {}, "--deploy", "--profile", "demo", "--region", REGION,
    )
    assert code == EXIT_OK, captured.err
    out = captured.out
    assert "install.sh is not available" in out
    assert "export AWS_PROFILE=demo" in out and f"export AWS_REGION={REGION}" in out
    assert "(cd admin-ui && npm ci && npm run build)" in out  # demo enables the console
    assert f"npx cdk deploy -c deployment_config={config_dir / 'demo.local.json'}" in out


def test_find_install_script_checks_the_root_then_path(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    on_path = tmp_path / "bin"
    on_path.mkdir()
    monkeypatch.setenv("PATH", str(on_path))
    assert deploy_module.find_install_script(root) is None
    script = on_path / "install.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert deploy_module.find_install_script(root) == script
    (root / "install.sh").write_text("#!/bin/sh\n")
    assert deploy_module.find_install_script(root) == root / "install.sh"
    assert deploy_module.deployment_config_argument(ROOT / "cdk" / "config" / "x.local.json", ROOT) == os.path.join(
        "config", "x.local.json")


# --- live checks ---------------------------------------------------------------------------


class FakeBedrock:
    """The four Bedrock calls the wizard makes, answered from canned data."""

    def __init__(self, *, log_group: str | None = None):
        self.log_group = log_group
        self.calls: list[str] = []

    def get_model_invocation_logging_configuration(self):
        self.calls.append("logging")
        if not self.log_group:
            return {}
        return {"loggingConfig": {"cloudWatchConfig": {"logGroupName": self.log_group}}}

    def list_foundation_models(self):
        self.calls.append("models")
        return {"modelSummaries": [
            {"modelId": "anthropic.claude-sonnet-5", "modelName": "Claude Sonnet 5",
             "providerName": "Anthropic", "inferenceTypesSupported": ["INFERENCE_PROFILE"],
             "modelLifecycle": {"status": "ACTIVE"}},
            {"modelId": "amazon.nova-lite-v1:0", "modelName": "Nova Lite", "providerName": "Amazon",
             "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "ACTIVE"}},
            {"modelId": "old.model-v1", "modelName": "Old", "providerName": "Old",
             "inferenceTypesSupported": ["ON_DEMAND"], "modelLifecycle": {"status": "LEGACY"}},
            {"modelId": "provisioned.only", "modelName": "Provisioned", "providerName": "P",
             "inferenceTypesSupported": ["PROVISIONED"], "modelLifecycle": {"status": "ACTIVE"}},
        ]}

    def list_inference_profiles(self, **kwargs):
        self.calls.append("profiles")
        return {"inferenceProfileSummaries": [
            {"inferenceProfileId": "us.anthropic.claude-sonnet-5",
             "inferenceProfileName": "US Claude Sonnet 5", "status": "ACTIVE"},
        ]}

    def get_inference_profile(self, inferenceProfileIdentifier):
        self.calls.append(f"profile:{inferenceProfileIdentifier}")
        return {"models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5"},
            {"modelArn": "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-5"},
        ]}


def fake_live(monkeypatch, config_dir: Path, bedrock: FakeBedrock, canned: dict[str, CheckResult]):
    """A LiveChecks over an offline Context whose preflight runner returns
    canned results; records which checks ran against which values."""
    context = Context(session=None, region=REGION, account=ACCOUNT, config_dir=config_dir, root=ROOT)
    context.client_cache["bedrock"] = bedrock
    runs: list[tuple[str, dict]] = []

    def fake_run(ctx, names):
        name = list(names)[0]
        runs.append((name, dict(ctx.raw)))
        result = canned.get(name) or CheckResult("pass", name, name, f"{name} looks fine")
        return Report(results=[result], account=ctx.account, region=ctx.region)

    monkeypatch.setattr(live, "run", fake_run)
    checks = live.LiveChecks(Console(), context)
    monkeypatch.setattr(live.LiveChecks, "connect", classmethod(lambda cls, console, **kwargs: checks))
    return runs


def test_live_check_results_are_shown_and_drive_the_defaults(monkeypatch, capsys, config_dir):
    bedrock = FakeBedrock(log_group="/aws/bedrock/modelinvocations")
    canned = {
        "region_support": CheckResult("pass", "region_support", "Region support",
                                      "us-east-1 (aws) supports Bedrock, the Lambda Web Adapter layer, Cognito and CloudFront"),
        "bedrock_model_access": CheckResult(
            "fail", "bedrock_model_access", "Bedrock model access",
            "not available: anthropic.claude-sonnet-5: agreement=NOT_AVAILABLE, authorization=NOT_AUTHORIZED, region=AVAILABLE\n"
            "available: amazon.nova-lite-v1:0",
            "enable model access in the Bedrock console (Model access) for the listed models",
        ),
    }
    runs = fake_live(monkeypatch, config_dir, bedrock, canned)
    answers = {
        "allowed_model_arns": "2, 3, arn:aws:bedrock:*::foundation-model/meta.llama4-scout",
        "Keep these models anyway (access can be enabled later)?": "y",
        "alert_email": "ops@corp.test",
    }
    code, captured, data, script = run_wizard(monkeypatch, capsys, config_dir, answers, live_checks=True)
    assert code == EXIT_OK, captured.err
    out = captured.out
    assert f"Live checks: on (account {ACCOUNT}, region {REGION})." in out
    assert "PASS  Region support" in out and "supports Bedrock, the Lambda Web Adapter layer" in out
    assert [name for name, _ in runs][:1] == ["region_support"]

    # Invocation logging already configured: false + the existing group are the defaults.
    assert "currently delivers to CloudWatch log group /aws/bedrock/modelinvocations" in out
    assert "manage_invocation_logging [y/N]" in out
    assert "invocation_log_group_name [/aws/bedrock/modelinvocations]" in out
    assert data["manage_invocation_logging"] is False
    assert data["invocation_log_group_name"] == "/aws/bedrock/modelinvocations"
    assert ("invocation_logging", dict) and any(name == "invocation_logging" for name, _ in runs)

    # The model menu: legacy and provisioned-only models are hidden; an
    # inference profile adds its underlying foundation models.
    assert "  1) anthropic.claude-sonnet-5" in out and "  2) amazon.nova-lite-v1:0" in out
    assert "  3) us.anthropic.claude-sonnet-5" in out and "old.model-v1" not in out
    assert "provisioned.only" not in out
    assert data["allowed_model_arns"] == [
        "arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0",
        f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/us.anthropic.claude-sonnet-5",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
        "arn:aws:bedrock:*::foundation-model/meta.llama4-scout",
    ]
    model_runs = [values for name, values in runs if name == "bedrock_model_access"]
    assert model_runs and model_runs[0]["allowed_model_arns"] == data["allowed_model_arns"]
    assert "FAIL  Bedrock model access" in out and "fix: enable model access" in out
    # The flagged model becomes a task for the account owner.
    assert "Bedrock account owner: enable model access" in out
    assert "not enabled in us-east-1: anthropic.claude-sonnet-5" in out
    assert validate_mapping(data, base_dir=config_dir, account=ACCOUNT) == []


def test_failed_live_check_lets_the_operator_change_the_answer(monkeypatch, capsys, config_dir):
    bedrock = FakeBedrock()
    canned = {
        "invoker_principals": CheckResult("fail", "invoker_principals", "Invoker principals",
                                          f"missing: arn:aws:iam::{ACCOUNT}:role/Nope",
                                          "create the principal or remove it"),
    }
    runs = fake_live(monkeypatch, config_dir, bedrock, canned)
    answers = {
        "invoker_principal_arns": [f"arn:aws:iam::{ACCOUNT}:role/Nope", "-"],
        "Keep these principals anyway?": "n",
    }
    code, captured, data, script = run_wizard(monkeypatch, capsys, config_dir, answers, live_checks=True)
    assert code == EXIT_OK, captured.err
    assert "FAIL  Invoker principals" in captured.out
    assert script.labels.count("invoker_principal_arns") == 2
    assert data["invoker_principal_arns"] == []
    assert len([name for name, _ in runs if name == "invoker_principals"]) == 1  # not re-run for an empty list
    # No logging configured: managed logging stays the default and no overwrite question is asked.
    assert "No Bedrock invocation logging is configured" in captured.out
    assert "Accept overwriting" not in captured.out and data["manage_invocation_logging"] is True


def test_live_checks_are_skipped_without_credentials(monkeypatch, capsys, config_dir):
    class NoCredentials:
        region_name = REGION

        def get_credentials(self):
            return None

    monkeypatch.setattr(live, "make_session", lambda profile, region: NoCredentials())
    code, captured, data, _ = run_wizard(monkeypatch, capsys, config_dir, {}, "--region", REGION,
                                         live_checks=True)
    assert code == EXIT_OK, captured.err
    assert "Live checks off: no AWS credentials found" in captured.out
    assert data == DEMO


# --- helpers and contract --------------------------------------------------------------


def test_parse_thresholds_and_rendering():
    assert flow.parse_thresholds("") is None
    assert flow.parse_thresholds("50:warn, 100:block") == [
        {"at": 0.5, "action": "warn"}, {"at": 1.0, "action": "block"},
    ]
    assert flow.render_thresholds([{"at": 0.5, "action": "warn"}, {"at": 0.8, "action": "warn"}]) == "50:warn,80:warn"
    with pytest.raises(ValueError, match="percent:warn or percent:block"):
        flow.parse_thresholds("50:alert")
    with pytest.raises(ValueError, match="percentage"):
        flow.parse_thresholds("fifty:warn")


def test_grouping_and_placeholders():
    groups = [[doc.name for doc in group] for group in flow.grouped(
        [doc for doc in KEY_DOCS if doc.section in ("identity", "logging") and not doc.advanced]
    )]
    assert ["jwt_issuer", "jwt_audience"] in groups
    assert ["manage_invocation_logging", "invocation_log_group_name"] in groups
    assert ["jwt_user_claim"] in groups
    assert flow.is_placeholder(PRODUCTION["jwt_issuer"]) and flow.is_placeholder(PRODUCTION["allowed_model_arns"])
    assert not flow.is_placeholder(PRODUCTION["jwt_audience"]) and not flow.is_placeholder(DEMO["default_limits"])


def test_every_known_key_has_a_wizard_type_handler(config_dir):
    wizard = flow.Wizard(Console(interactive=False), template="demo", output=config_dir / "x.json")
    for doc in KEY_DOCS:
        assert doc.name in KNOWN_KEYS
        if doc.name == "workloads":
            continue
        value = wizard.prompt_value(doc)  # non-interactive: the default
        expected = wizard.values.get(doc.name, DEFAULTS.get(doc.name))
        assert value == expected, doc.name


@pytest.mark.skipif("admin_email" not in KNOWN_KEYS, reason="admin_email key not present")
def test_admin_email_is_asked_only_for_the_cognito_console(monkeypatch, capsys, config_dir):
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir, {"admin_email": "admin@corp.test"}
    )
    assert code == EXIT_OK, captured.err
    assert "admin_email" in script.labels and data["admin_email"] == "admin@corp.test"
    code, captured, data, script = run_wizard(
        monkeypatch, capsys, config_dir, PRODUCTION_ANSWERS, template="production"
    )
    assert code == EXIT_OK, captured.err
    assert "admin_email" not in script.labels and "admin_email" not in data


def test_setup_py_runs_from_the_repository_root():
    completed = subprocess.run(
        [PYTHON, "setup.py", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for option in ("--profile-template", "--output", "--answers", "--save-answers", "--yes",
                   "--deploy", "--no-live-checks"):
        assert option in completed.stdout
