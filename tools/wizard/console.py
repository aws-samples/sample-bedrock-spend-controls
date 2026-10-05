"""Terminal input and output for the setup wizard.

Everything the wizard prints or asks goes through :class:`Console`, built on
``input()`` alone (no third-party prompt library). Tests drive it by
monkeypatching ``builtins.input`` and reading stdout. With ``--yes`` the
console runs non-interactively: every question returns its default, and a
question without one raises :class:`MissingAnswer` instead of blocking.

Conventions shown to the operator: Enter keeps the value in brackets, a lone
``-`` clears a text or list value, Ctrl-C (or end of input) aborts.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Callable
from typing import TextIO, TypeVar

T = TypeVar("T")

YES = frozenset({"y", "yes", "true", "1", "on"})
NO = frozenset({"n", "no", "false", "0", "off"})
CLEAR = "-"


def _stdin_is_terminal() -> bool:
    stdin = sys.stdin
    try:
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError):
        return False


class Aborted(Exception):
    """The operator ended the session (end of input or Ctrl-C)."""


class MissingAnswer(Exception):
    """A non-interactive run reached a question without a usable default."""


class Console:
    """Prompts and messages; ``interactive=False`` answers every question
    with its default (``--yes`` and ``--answers`` replays)."""

    def __init__(self, *, interactive: bool = True, out: TextIO | None = None) -> None:
        self.interactive = interactive
        self._out = out
        self.prompts = 0  # questions actually put to the operator

    @property
    def out(self) -> TextIO:
        # Resolved per call so pytest's capsys replacement of sys.stdout is seen.
        return self._out or sys.stdout

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def read(self, prompt: str) -> str:
        """One stripped line from the operator; end of input aborts the run."""
        self.prompts += 1
        try:
            raw = builtins.input(prompt)
        except EOFError:
            self.say()
            raise Aborted("end of input") from None
        except KeyboardInterrupt:
            self.say()
            raise Aborted("interrupted") from None
        if not _stdin_is_terminal():
            # Piped or scripted answers are not echoed by input(); show them
            # so the transcript reads like an interactive session.
            self.say(raw.strip())
        return raw.strip()

    # --- questions -------------------------------------------------------------

    @staticmethod
    def _prompt(label: str, default: str | None, example: str | None) -> str:
        if default is not None:
            shown = default if default != "" else "none"
            return f"  {label} [{shown}]: "
        if example:
            return f"  {label} (example: {example}): "
        return f"  {label}: "

    def ask_text(
        self,
        label: str,
        default: str | None,
        *,
        example: str | None = None,
        required: bool = False,
    ) -> str:
        """Free text. Enter keeps ``default`` (shown in brackets), ``-`` clears
        the value, and a ``required`` question without a default repeats until
        something is typed. ``example`` is shown when there is no default."""
        while True:
            if not self.interactive:
                if default is not None:
                    return default
                if required:
                    raise MissingAnswer(label)
                return ""
            text = self.read(self._prompt(label, default, example))
            if text == "":
                if default is not None:
                    return default
                if required:
                    self.say("  A value is required here.")
                    continue
                return ""
            if text == CLEAR:
                return ""
            return text

    def ask_bool(self, label: str, default: bool | None) -> bool:
        """Yes/no. Enter keeps ``default``; ``None`` forces an explicit answer."""
        while True:
            if not self.interactive:
                if default is None:
                    raise MissingAnswer(label)
                return default
            if default is None:
                choices = "y/n"
            else:
                choices = "Y/n" if default else "y/N"
            text = self.read(f"  {label} [{choices}]: ").lower()
            if text == "" and default is not None:
                return default
            if text in YES:
                return True
            if text in NO:
                return False
            self.say("  Please answer y or n.")

    def ask_value(
        self,
        label: str,
        default: str | None,
        parse: Callable[[str], T],
        *,
        example: str | None = None,
        required: bool = False,
    ) -> T:
        """:meth:`ask_text` followed by ``parse``; a ``ValueError`` shows its
        message and asks again (or propagates when non-interactive)."""
        while True:
            text = self.ask_text(label, default, example=example, required=required)
            try:
                return parse(text)
            except ValueError as exc:
                if not self.interactive:
                    raise
                self.say(f"  {exc}")


# --- parsers for typed answers -------------------------------------------------


def parse_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(f"Expected an integer, got {text!r}.") from None


def parse_float(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError:
        raise ValueError(f"Expected a number, got {text!r}.") from None


def parse_non_negative_int(text: str) -> int:
    value = parse_int(text)
    if value < 0:
        raise ValueError("Expected 0 or a positive integer.")
    return value


def parse_non_negative_float(text: str) -> float:
    value = parse_float(text)
    if value < 0:
        raise ValueError("Expected 0 or a positive number.")
    return value


def parse_list(text: str) -> list[str]:
    """Comma-separated entries, blanks dropped; ``''`` is the empty list."""
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_bool_text(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in YES:
        return True
    if lowered in NO:
        return False
    raise ValueError(f"Expected true or false, got {text!r}.")


def render(value: object) -> str | None:
    """A JSON value as the default text shown in brackets (None stays None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    if isinstance(value, float) and value.is_integer():
        return str(value)
    return str(value)
