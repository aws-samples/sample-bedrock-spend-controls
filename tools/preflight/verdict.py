"""Turn a preflight JSON report into the three words install.sh acts on.

``python -m tools.preflight.verdict report.json`` prints
``<verdict> <warnings> <bootstrap>``:

- ``verdict``: ``ok`` or ``fail``. A failed ``bootstrap`` check does not
  fail the verdict: the installer bootstraps the environment itself in its
  next phase, so a fresh account is the expected case, not an error.
- ``warnings``: ``warn`` when any check the installer cannot resolve on its
  own warned, else ``clean``. ``admin_ui_build`` is ignored because the
  build phase produces the console right after the preflight.
- ``bootstrap``: the status of the ``bootstrap`` check (``missing`` when it
  did not run), which decides whether ``cdk bootstrap`` is attempted.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

# Checks whose outcome the installer resolves itself in a later phase.
INSTALLER_RESOLVES = frozenset({"bootstrap", "admin_ui_build"})


def install_verdict(report: Mapping[str, Any]) -> tuple[str, str, str]:
    statuses = {result["name"]: result["status"] for result in report.get("results", [])}
    blocking = [
        name for name, status in statuses.items()
        if status == "fail" and name not in INSTALLER_RESOLVES
    ]
    warnings = [
        name for name, status in statuses.items()
        if status == "warn" and name not in INSTALLER_RESOLVES
    ]
    return (
        "fail" if blocking else "ok",
        "warn" if warnings else "clean",
        statuses.get("bootstrap", "missing"),
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m tools.preflight.verdict REPORT.json", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as handle:
        report = json.load(handle)
    print(" ".join(install_verdict(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
