"""``--deploy``: the full preflight on the written file, then ``install.sh``.

The install script (``install.sh`` at the repository root, or on PATH) is
only ever started after the command has been printed and the operator has
confirmed it (``--yes`` confirms). When the script is not present, the
equivalent manual commands from DEPLOYMENT.md are printed instead.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.preflight import Options, PreflightError, run_for_config
from tools.preflight.context import REPO_ROOT

from .console import Console

INSTALL_SCRIPT = "install.sh"
EXIT_OK = 0
EXIT_FAILED = 1


def find_install_script(root: Path = REPO_ROOT) -> Path | None:
    candidate = root / INSTALL_SCRIPT
    if candidate.is_file():
        return candidate
    found = shutil.which(INSTALL_SCRIPT)
    return Path(found) if found else None


def install_command(
    script: Path,
    *,
    output: Path,
    profile: str | None,
    region: str | None,
    acknowledge_logging_overwrite: bool,
    yes: bool,
) -> list[str]:
    command = [str(script), "--config", str(output.resolve())]
    if profile:
        command += ["--profile", profile]
    if region:
        command += ["--region", region]
    if acknowledge_logging_overwrite:
        command.append("--acknowledge-logging-overwrite")
    if yes:
        command.append("--yes")
    return command


def deployment_config_argument(output: Path, root: Path) -> str:
    """The ``-c deployment_config=`` value as seen from ``cdk/``."""
    try:
        return str(output.resolve().relative_to((root / "cdk").resolve()))
    except ValueError:
        return str(output.resolve())


def equivalent_commands(
    *, output: Path, root: Path, profile: str | None, region: str | None, admin_ui: bool
) -> list[str]:
    """The manual deployment from DEPLOYMENT.md for this configuration."""
    config = deployment_config_argument(output, root)
    lines: list[str] = []
    if profile:
        lines.append(f"export AWS_PROFILE={shlex.quote(profile)}")
    if region:
        quoted = shlex.quote(region)
        lines.append(f"export AWS_REGION={quoted} AWS_DEFAULT_REGION={quoted}")
    if admin_ui:
        lines.append("(cd admin-ui && npm ci && npm run build)")
    lines += [
        "cd cdk",
        "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && npm ci",
        "npx cdk bootstrap   # once per account and Region",
        f"npx cdk synth  -c deployment_config={shlex.quote(config)}",
        f"npx cdk deploy -c deployment_config={shlex.quote(config)}",
    ]
    return lines


def deploy(
    console: Console,
    *,
    output: Path,
    root: Path = REPO_ROOT,
    profile: str | None,
    region: str | None,
    acknowledge_logging_overwrite: bool,
    yes: bool,
    admin_ui: bool,
    run_command: Callable[..., Any] = subprocess.run,
) -> int:
    """Preflight the written file; on success print (and, once confirmed,
    run) the install command. Returns the process exit status."""
    console.say()
    console.say("Preflight checks on the written configuration:")
    try:
        report = run_for_config(
            output,
            profile=profile,
            region=region,
            options=Options(acknowledge_logging_overwrite=acknowledge_logging_overwrite),
        )
    except PreflightError as exc:
        console.say(f"  preflight: {exc}")
        return EXIT_FAILED
    console.say(report.to_text())
    if not report.ok:
        console.say("Preflight failed; fix the findings above and run setup.py --deploy again.")
        return EXIT_FAILED

    script = find_install_script(root)
    if script is None:
        console.say(
            f"{INSTALL_SCRIPT} is not available in this checkout; run these commands instead "
            "(from the repository root):"
        )
        for line in equivalent_commands(
            output=output, root=root, profile=profile, region=region, admin_ui=admin_ui
        ):
            console.say(f"  {line}")
        return EXIT_OK

    command = install_command(
        script,
        output=output,
        profile=profile,
        region=region,
        acknowledge_logging_overwrite=acknowledge_logging_overwrite,
        yes=yes,
    )
    console.say("Deploy command:")
    console.say(f"  {shlex.join(command)}")
    if not yes and not console.ask_bool("Run it now?", False):
        console.say("Not deployed; run the command above when ready.")
        return EXIT_OK
    completed = run_command(command, cwd=str(root), check=False)
    return int(getattr(completed, "returncode", 0) or 0)
