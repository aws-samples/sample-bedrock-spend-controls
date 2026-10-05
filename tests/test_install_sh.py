"""install.sh: syntax, help, and the dry run's command plan.

The dry run is driven with fake ``aws``, ``npx`` and ``npm`` executables on
PATH (shims that only echo their arguments) so nothing contacts AWS or
installs anything. The real ``node`` and ``python3`` are used because the
script checks their versions locally.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"

FLAGS = (
    "--profile",
    "--region",
    "--config",
    "--alert-email",
    "--admin-email",
    "--acknowledge-logging-overwrite",
    "--yes",
    "--skip-smoke",
    "--skip-preflight",
    "--destroy",
    "--dry-run",
    "--help",
)
PHASES = (
    "preflight",
    "build",
    "bootstrap",
    "synth",
    "diff",
    "deploy",
    "outputs",
    "smoke",
    "done",
)


@pytest.fixture(scope="module")
def shims(tmp_path_factory) -> Path:
    """A PATH prefix whose aws/npx/npm print their argv and exit 0."""
    directory = tmp_path_factory.mktemp("shims")
    for tool in ("aws", "npx", "npm"):
        shim = directory / tool
        shim.write_text(f'#!/bin/sh\necho "SHIM {tool} $*"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


def _run(args: list[str], *, shims: Path | None = None, env_extra: dict | None = None,
         cwd: Path = ROOT) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        # The script defaults --profile to AWS_PROFILE; keep the plan deterministic.
        if key not in {"AWS_PROFILE", "ALERT_EMAIL", "ADMIN_EMAIL", "SMOKE_MODEL"}
    }
    env["AWS_REGION"] = "us-east-1"
    env["HOME"] = env.get("HOME", str(cwd))
    if shims is not None:
        env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, str(INSTALL_SH), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_script_is_executable_bash_with_strict_mode():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text.splitlines()[:25] or "set -euo pipefail" in text
    assert INSTALL_SH.stat().st_mode & stat.S_IXUSR
    # Portability to the stock macOS bash 3.2: no associative arrays, no
    # case-modification expansions, no mapfile (comments excluded).
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "declare -A" not in code
    assert "mapfile" not in code
    assert not re.search(r"\$\{[A-Za-z_]+,,\}", code)


def test_bash_n_accepts_the_script():
    completed = subprocess.run(
        [BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_help_lists_every_flag_and_exits_zero():
    completed = _run(["--help"])
    assert completed.returncode == 0, completed.stderr
    for flag in FLAGS:
        assert flag in completed.stdout, flag
    assert "-h" in completed.stdout
    assert "curl" not in completed.stderr
    # --help needs no AWS CLI, Node or Region: it must work on a bare host.
    bare = subprocess.run(
        [BASH, str(INSTALL_SH), "-h"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare.returncode == 0, bare.stderr
    assert "Usage: install.sh" in bare.stdout


def test_unknown_option_and_missing_alert_email_fail_before_any_command(shims):
    unknown = _run(["--bogus"], shims=shims)
    assert unknown.returncode == 1
    assert "unknown option: --bogus" in unknown.stderr
    assert "+ " not in unknown.stdout

    missing = _run(["--dry-run", "--yes", "--skip-preflight"], shims=shims)
    assert missing.returncode == 1
    assert "--alert-email is required" in missing.stderr

    malformed = _run(["--dry-run", "--yes", "--alert-email", "not an address"], shims=shims)
    assert malformed.returncode == 1
    assert "--alert-email must be one address" in malformed.stderr

    no_config = _run(["--dry-run", "--yes", "--config", "nope.json"], shims=shims)
    assert no_config.returncode == 1
    assert "config file not found" in no_config.stderr


def test_dry_run_prints_the_phase_plan_without_touching_aws(shims):
    completed = _run(
        ["--dry-run", "--config", "demo", "--alert-email", "a@example.com",
         "--skip-preflight", "--yes"],
        shims=shims,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    # Shims echo "SHIM <tool>" when executed; a dry run must not execute them.
    assert "SHIM" not in out
    assert "dry run" in out

    headers = [f"==> [{index}/9] {name}" for index, name in enumerate(PHASES, start=1)]
    positions = [out.index(header) for header in headers]
    assert positions == sorted(positions)
    assert "skipped: --skip-preflight" in out

    commands = [line for line in out.splitlines() if line.startswith("+ ")]
    joined = "\n".join(commands)
    # build: console, CDK virtualenv, CDK CLI.
    assert re.search(r"\+ \(cd \S*/admin-ui && npm ci\)", joined)
    assert re.search(r"\+ \(cd \S*/admin-ui && npm run build\)", joined)
    assert re.search(r"\+ \S*python\S* -m venv \S*/cdk/\.venv", joined) or Path(ROOT, "cdk", ".venv").exists()
    assert re.search(r"cdk/\.venv/bin/pip install .*-r \S*/cdk/requirements\.txt", joined)
    assert re.search(r"\+ \(cd \S*/cdk && npm ci\)", joined)
    # bootstrap runs because the preflight was skipped.
    assert re.search(
        r"\+ \(cd \S*/cdk && npx cdk bootstrap aws://\d{12}/us-east-1 "
        r"-c deployment_config=\S*demo\.json -c alert_email=a@example\.com -c admin_email=a@example\.com\)", joined)
    context = (
        r"-c deployment_config=\S*cdk/config/demo\.json "
        r"-c alert_email=a@example\.com -c admin_email=a@example\.com"
    )
    assert re.search(r"npx cdk synth " + context + r" --quiet", joined)
    assert re.search(r"npx cdk diff " + context, joined)
    deploy = re.search(r"npx cdk deploy --require-approval never " + context + r"\)", joined)
    assert deploy, joined
    assert "-c manage_invocation_logging" not in joined
    # outputs and smoke test.
    assert "aws cloudformation describe-stacks --stack-name BedrockSpendControls --output json" in joined
    assert ".install-outputs.env" in out
    assert re.search(
        r"\.venv-examples/bin/python \S*/tools/smoke_test\.py --region us-east-1 "
        r"--stack BedrockSpendControls",
        joined,
    )
    # Secrets never appear; the admin email defaults to the alert email.
    assert "check a@example.com for the temporary password" in out
    assert "quota-admin" in out
    # Summary table.
    summary = out.split("Summary", 1)[1]
    assert re.search(r"preflight\s+SKIPPED", summary)
    for name in PHASES[1:]:
        assert re.search(rf"{name}\s+OK", summary), name


def test_dry_run_honours_profile_admin_email_and_skips(shims):
    completed = _run(
        ["--dry-run", "--profile", "demo-profile", "--region", "eu-west-1",
         "--alert-email", "alerts@example.com", "--admin-email", "admin@example.com",
         "--skip-preflight", "--skip-smoke", "--yes", "--acknowledge-logging-overwrite"],
        shims=shims,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert "-c alert_email=alerts@example.com -c admin_email=admin@example.com" in out
    assert "--region eu-west-1" not in out.split("==> [8/9] smoke")[1].split("==>")[0]
    assert "skipped: --skip-smoke" in out
    assert re.search(r"smoke\s+SKIPPED", out.split("Summary", 1)[1])
    assert "--destroy --profile demo-profile --region eu-west-1" in out


def test_dry_run_preflight_command_is_printed_not_executed(shims):
    completed = _run(
        ["--dry-run", "--alert-email", "a@example.com", "--yes",
         "--acknowledge-logging-overwrite"],
        shims=shims,
    )
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    preflight = out.split("==> [1/9] preflight", 1)[1].split("==> [2/9]", 1)[0]
    # The CDK virtualenv (plus the pinned boto3/httpx) is prepared first so the
    # checks can import cdk/stacks/configuration.py and validate like synth.
    assert re.search(r"\+ \S*python\S* -m venv \S*/cdk/\.venv", preflight) or Path(ROOT, "cdk", ".venv").exists()
    assert re.search(
        r"cdk/\.venv/bin/pip install .*-r \S*/cdk/requirements\.txt "
        r"-r \S*/examples/requirements\.txt",
        preflight,
    )
    assert re.search(
        r"\+ \S*/cdk/\.venv/bin/python -m tools\.preflight --config \S*demo\.json "
        r"--region us-east-1 --json --set alert_email=a@example\.com "
        r"--set admin_email=a@example\.com --acknowledge-logging-overwrite",
        preflight,
    )
    assert "the preflight was not executed" in preflight
    # Not created twice: the build phase reuses the virtualenv.
    build = out.split("==> [2/9] build", 1)[1].split("==> [3/9]", 1)[0]
    assert "-m venv" not in build
    # The smoke test keeps its own virtualenv from examples/requirements.txt.
    smoke = out.split("==> [8/9] smoke", 1)[1].split("==> [9/9]", 1)[0]
    # On a machine where an earlier run already created the virtualenv the
    # script skips `python -m venv`; accept either form.
    assert re.search(r"-m venv \S*/\.venv-examples", smoke) or Path(ROOT, ".venv-examples").exists()


def test_destroy_dry_run_lists_retained_resources(shims):
    completed = _run(["--destroy", "--dry-run", "--yes"], shims=shims)
    assert completed.returncode == 0, completed.stderr
    out = completed.stdout
    assert re.search(r"npx cdk destroy --force -c deployment_config=\S*demo\.json\)", out)
    assert "/bedrock/spend-controls/model-invocations" in out
    assert "delete-model-invocation-logging-configuration" in out
    assert "BedrockLoggingRole" in out
    assert "SHIM" not in out


def test_node_requirement_is_explained_not_installed(shims, tmp_path):
    old_node = tmp_path / "node"
    old_node.write_text("#!/bin/sh\necho v18.20.4\n", encoding="utf-8")
    old_node.chmod(0o755)
    completed = _run(
        ["--dry-run", "--alert-email", "a@example.com", "--yes"],
        shims=shims,
        env_extra={"PATH": f"{tmp_path}{os.pathsep}{shims}{os.pathsep}{os.environ['PATH']}"},
    )
    assert completed.returncode == 1
    assert "Node.js 20 or newer is required (found: v18.20.4)" in completed.stderr
    assert "nvm install" in completed.stderr
    assert "does not install software" in completed.stderr
    assert "+ " not in completed.stdout


def test_shellcheck_if_available():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    completed = subprocess.run(
        [shellcheck, "--shell=bash", "--severity=warning", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
