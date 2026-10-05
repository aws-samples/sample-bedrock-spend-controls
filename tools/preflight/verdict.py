"""The installer's reading of a preflight JSON report.

``python -m tools.preflight.verdict REPORT.json`` prints three words,
``<verdict> <warnings> <bootstrap>``, that install.sh acts on:

- ``verdict``: ``ok`` or ``fail``. A failed ``bootstrap`` check does not
  fail the verdict: the installer bootstraps the environment itself in its
  next phase, so a fresh account is the expected case, not an error.
- ``warnings``: ``warn`` when a check the installer cannot resolve on its
  own warned, else ``clean``. ``admin_ui_build`` is ignored because the
  build phase produces the console right after the preflight, and the
  ``allowed_model_arns: "*"`` advisory is ignored because the demo profile
  sets it on purpose (the deploy-time synth warning still shows it).
- ``bootstrap``: the status of the ``bootstrap`` check (``missing`` when it
  did not run), which decides whether ``cdk bootstrap`` is attempted.

``--render`` prints the same table the preflight prints, followed by the
installer's verdict instead of the raw summary.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from .report import CheckResult, Report

# Checks whose outcome the installer resolves itself in a later phase.
INSTALLER_RESOLVES = frozenset({"bootstrap", "admin_ui_build"})
# A warning the demo profile triggers by design.
UNRESTRICTED_MODELS_PREFIX = "allowed_model_arns contains '*'"


def _informational(result: Mapping[str, Any]) -> bool:
    return (
        result["name"] in INSTALLER_RESOLVES
        or (
            result["name"] == "bedrock_model_access"
            and result["status"] == "warn"
            and str(result.get("detail", "")).startswith(UNRESTRICTED_MODELS_PREFIX)
        )
    )


def install_verdict(report: Mapping[str, Any]) -> tuple[str, str, str]:
    results = list(report.get("results", []))
    blocking = [r["name"] for r in results if r["status"] == "fail" and not _informational(r)]
    warnings = [r["name"] for r in results if r["status"] == "warn" and not _informational(r)]
    statuses = {r["name"]: r["status"] for r in results}
    return (
        "fail" if blocking else "ok",
        "warn" if warnings else "clean",
        statuses.get("bootstrap", "missing"),
    )


def render(report: Mapping[str, Any]) -> str:
    """The preflight table plus the installer's verdict line."""
    results = [
        CheckResult(
            status=r["status"], name=r["name"], title=r["title"],
            detail=r.get("detail", ""), fix=r.get("fix", ""),
        )
        for r in report.get("results", [])
    ]
    table = Report(results, account=report.get("account"), region=report.get("region")).to_text()
    table = table.rsplit("\nSummary:", 1)[0]
    verdict, warnings, bootstrap = install_verdict(report)
    handled = [
        r.name for r in results
        if r.name in INSTALLER_RESOLVES and r.status in ("fail", "warn")
    ]
    note = ""
    if handled:
        note = f" ({', '.join(handled)}: handled by the next phases)"
    line = "Installer verdict: " + ("OK" if verdict == "ok" else "FAIL") + note
    if verdict == "ok" and warnings == "warn":
        line += "; warnings need a look"
    return f"{table}\n{line}\n"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    do_render = "--render" in args
    args = [arg for arg in args if arg != "--render"]
    if len(args) != 1:
        print("usage: python -m tools.preflight.verdict [--render] REPORT.json", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as handle:
        report = json.load(handle)
    if do_render:
        sys.stdout.write(render(report))
    else:
        print(" ".join(install_verdict(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
