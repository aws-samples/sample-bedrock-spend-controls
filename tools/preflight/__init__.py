"""Preflight checks for deploying Bedrock Spend Controls.

One implementation of every pre-deployment check, shared by ``install.sh``,
the one-click CodeBuild installer and the ``setup.py`` wizard. Everything is
read-only (stdlib + boto3) and reuses the CDK app's own configuration
validator so the checks and ``cdk synth`` never disagree.

Programmatic use::

    from tools.preflight import Options, run_for_config

    report = run_for_config("cdk/config/demo.json", profile="demo", region="us-east-1",
                            options=Options(acknowledge_logging_overwrite=True))
    print(report.to_text())
    if not report.ok:
        ...

Command line: ``python -m tools.preflight --help``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .checks import CHECKS, REQUIRED_ACTIONS, TITLES, execute, run
from .cli import main
from .context import Context, Options, PreflightError, build_context
from .report import CheckResult, Report

__all__ = [
    "CHECKS",
    "CheckResult",
    "Context",
    "Options",
    "PreflightError",
    "REQUIRED_ACTIONS",
    "Report",
    "TITLES",
    "build_context",
    "execute",
    "main",
    "run",
    "run_for_config",
]


def run_for_config(
    path: Path | str,
    profile: str | None = None,
    region: str | None = None,
    options: Options | None = None,
    *,
    account_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    checks: Iterable[str] | None = None,
) -> Report:
    """Load ``path`` (a ``cdk/config/*.json`` file), validate it and run the
    checks. Raises :class:`PreflightError` for usage or configuration
    errors; AWS errors never raise, they become ``warn``/``fail`` results."""
    context = build_context(
        path,
        profile=profile,
        region=region,
        account_id=account_id,
        overrides=overrides,
        options=options,
    )
    return run(context, checks)
