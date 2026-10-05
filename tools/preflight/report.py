"""Result and report types shared by the preflight checks and their callers.

A check returns one :class:`CheckResult`; :func:`tools.preflight.run`
collects them into a :class:`Report` that renders either as an aligned text
table for a terminal or as JSON with a stable schema for ``install.sh``,
CodeBuild, and the setup wizard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["pass", "warn", "fail", "skip"]

STATUSES: tuple[Status, ...] = ("pass", "warn", "fail", "skip")

# Shown in the first column of the text report; every label has the same
# width so the table stays aligned.
_LABELS: dict[str, str] = {
    "pass": "PASS",
    "warn": "WARN",
    "fail": "FAIL",
    "skip": "SKIP",
}


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one preflight check.

    ``status`` is one of ``pass``, ``warn``, ``fail``, ``skip``; ``name`` is
    the check's key in :data:`tools.preflight.checks.CHECKS`; ``title`` is a
    short human label; ``detail`` explains what was observed (may span
    several lines); ``fix`` tells the operator what to do about a ``warn`` or
    ``fail`` (empty when nothing is needed).
    """

    status: Status
    name: str
    title: str
    detail: str = ""
    fix: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(STATUSES)}; got {self.status!r}"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class Report:
    """All results of one preflight run plus the target it ran against."""

    results: list[CheckResult] = field(default_factory=list)
    account: str | None = None
    region: str | None = None
    profile: str | None = None

    @property
    def ok(self) -> bool:
        """True when no check failed (warnings and skips do not block)."""
        return not any(result.status == "fail" for result in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(result.status == "warn" for result in self.results)

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUSES}
        for result in self.results:
            counts[result.status] += 1
        return counts

    def summary(self) -> str:
        counts = self.counts()
        verdict = "OK" if self.ok else "FAIL"
        return (
            f"{counts['pass']} passed, {counts['warn']} warnings, "
            f"{counts['fail']} failed, {counts['skip']} skipped -> {verdict}"
        )

    def to_text(self) -> str:
        """Render an aligned table: one line per check, then indented
        detail and fix lines, then a summary line."""
        target = f"account {self.account or 'unknown'}, region {self.region or 'unknown'}"
        if self.profile:
            target += f", profile {self.profile}"
        lines = [f"Preflight checks ({target})", ""]
        width = max((len(result.name) for result in self.results), default=0)
        indent = " " * (2 + 4 + 2 + width + 2)
        for result in self.results:
            label = _LABELS[result.status]
            lines.append(f"  {label}  {result.name.ljust(width)}  {result.title}")
            for detail_line in result.detail.splitlines():
                lines.append(f"{indent}{detail_line}")
            if result.fix:
                fix_lines = result.fix.splitlines()
                lines.append(f"{indent}fix: {fix_lines[0]}")
                for extra in fix_lines[1:]:
                    lines.append(f"{indent}     {extra}")
        lines.append("")
        lines.append(f"Summary: {self.summary()}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "region": self.region,
            "ok": self.ok,
            "results": [result.to_dict() for result in self.results],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Stable schema: ``{"account", "region", "ok", "results": [...]}``
        where each result is ``{"name", "status", "title", "detail", "fix"}``."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)
