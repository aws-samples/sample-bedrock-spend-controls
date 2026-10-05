"""Command line of the setup wizard.

    python setup.py [--profile-template demo|production]
        [--output cdk/config/<name>.local.json] [--profile AWS_PROFILE]
        [--region REGION] [--answers FILE] [--save-answers FILE] [--yes]
        [--deploy] [--no-live-checks]

Exit status: 0 when the configuration was written (and, with ``--deploy``,
the deploy succeeded or was declined), 1 when the values were rejected, an
answer was missing, the run was aborted, or the deploy failed, 2 for a usage
error (bad arguments, an unreadable or unknown-key ``--answers`` file, or a
missing CDK Python environment).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tools.preflight.context import REPO_ROOT

from .console import (
    Aborted,
    Console,
    MissingAnswer,
    parse_bool_text,
    parse_float,
    parse_int,
    parse_list,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

TEMPLATES = ("demo", "production")
DEFAULT_CONFIG_DIR = REPO_ROOT / "cdk" / "config"


class UsageError(Exception):
    """Reported on stderr with exit status 2."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python setup.py",
        description=(
            "Interactive configuration wizard for Bedrock Spend Controls. Asks every deployment "
            "key section by section, validates each answer exactly as cdk synth does, checks "
            "answers against the target AWS account when credentials are available, and writes "
            "the deployment file (plus cdk/config/workloads.json when workloads are configured)."
        ),
        epilog=(
            "Enter keeps the value in brackets, '-' clears a value. Exit status: 0 written, "
            "1 rejected or aborted, 2 usage error."
        ),
    )
    parser.add_argument(
        "--profile-template",
        choices=TEMPLATES,
        default="demo",
        metavar="demo|production",
        help="shipped deployment file whose values are the defaults (default: demo)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help=(
            "deployment file to write; an existing file is edited (its values become the "
            "defaults). Default: cdk/config/<template>.local.json"
        ),
    )
    parser.add_argument(
        "--profile", metavar="NAME", help="AWS CLI profile for live checks and deploy"
    )
    parser.add_argument(
        "--region",
        metavar="REGION",
        help="target Region (default: AWS_REGION, then the profile's region)",
    )
    parser.add_argument(
        "--answers",
        metavar="FILE",
        help="JSON object of key: value answers used as the defaults (with --yes: as the answers)",
    )
    parser.add_argument(
        "--save-answers",
        metavar="FILE",
        help="write the answers given to this JSON file for a later --answers --yes replay",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="ask nothing: accept the defaults and --answers, and confirm the deploy",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="after writing: run the full preflight, then install.sh (printed first, confirmed)",
    )
    parser.add_argument(
        "--no-live-checks",
        action="store_true",
        help="never call AWS while asking (also the case when no credentials are found)",
    )
    return parser


def coerce_answer(doc: Any, value: Any) -> Any:
    """Answers files may hold strings for typed keys ("true", "35", "a,b")."""
    if not isinstance(value, str) or doc is None:
        return value
    kind = getattr(doc, "type", "string")
    if kind == "bool":
        return parse_bool_text(value)
    if kind == "int":
        return parse_int(value)
    if kind == "float":
        return parse_float(value)
    if kind == "string_list":
        return parse_list(value)
    if kind == "object" and value.strip().startswith("{"):
        return json.loads(value)
    return value


def load_answers(path: Path, known_keys: frozenset[str], describe_key: Any) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"--answers: cannot read {path}: {exc.strerror or exc}") from None
    except json.JSONDecodeError as exc:
        raise UsageError(f"--answers: {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise UsageError("--answers: the file must contain a JSON object of key: value answers")
    unknown = sorted(set(data) - known_keys)
    if unknown:
        raise UsageError(
            "--answers: unknown deployment keys: " + ", ".join(unknown)
            + " (known keys: " + ", ".join(sorted(known_keys)) + ")"
        )
    answers: dict[str, Any] = {}
    for key, value in data.items():
        try:
            doc = describe_key(key)
        except KeyError:
            doc = None
        try:
            answers[key] = coerce_answer(doc, value)
        except ValueError as exc:
            raise UsageError(f"--answers: {key}: {exc}") from None
    return answers


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except UsageError as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Aborted as exc:
        print(f"setup: aborted ({exc}).", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\nsetup: interrupted.", file=sys.stderr)
        return EXIT_FAILED


def _run(args: argparse.Namespace) -> int:
    try:
        from cdk.stacks.configuration import KNOWN_KEYS, describe_key, validate_mapping

        from . import flow
    except ImportError as exc:
        raise UsageError(
            f"cannot import cdk/stacks/configuration.py ({exc}); install the Python requirements "
            "first: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt"
        ) from None
    from .deploy import deploy
    from .live import LiveChecks
    from .summary import render_summary

    template = args.profile_template
    output = Path(args.output) if args.output else DEFAULT_CONFIG_DIR / f"{template}.local.json"
    output_dir = output.resolve().parent
    answers = load_answers(Path(args.answers), KNOWN_KEYS, describe_key) if args.answers else {}
    console = Console(interactive=not args.yes)

    live = None
    if not args.no_live_checks:
        live = LiveChecks.connect(
            console, profile=args.profile, region=args.region, output_dir=output_dir
        )
    try:
        wizard = flow.Wizard(console, template=template, output=output, live=live, answers=answers)
    except flow.SetupError as exc:
        raise UsageError(str(exc)) from None

    try:
        values = wizard.run()
    except MissingAnswer as exc:
        print(
            f"setup: no answer for '{exc}' and the template only has an example value; "
            "add it to --answers or run without --yes.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except flow.ValidationFailed as exc:
        print(f"setup: configuration rejected: {exc}", file=sys.stderr)
        return EXIT_FAILED

    # Workloads: the roster lives inline while asking; on disk it is a file
    # next to the deployment file, referenced by its relative path.
    roster = values.get("workloads") if isinstance(values.get("workloads"), dict) else None
    output_dir.mkdir(parents=True, exist_ok=True)
    if roster is not None:
        write_json(output_dir / flow.WORKLOADS_FILE, roster)
        values["workloads"] = flow.WORKLOADS_FILE
    written = wizard.written_values()
    errors = validate_mapping(written, base_dir=output_dir, account=wizard.account)
    if errors:  # cannot happen after run(); guards the file we are about to write
        print(f"setup: configuration rejected: {errors[0]}", file=sys.stderr)
        return EXIT_FAILED
    write_json(output, written)
    console.say()
    wrote = f"Wrote {output}"
    if roster is not None:
        wrote += f" and {output_dir / flow.WORKLOADS_FILE}"
    console.say(wrote)
    if args.save_answers:
        write_json(Path(args.save_answers), wizard.answered)
        console.say(
            f"Saved answers to {args.save_answers} "
            f"(replay: --answers {args.save_answers} --yes)"
        )

    region = live.region if live is not None else args.region
    for line in render_summary(
        written,
        changed=wizard.changed_keys(written),
        output_dir=output_dir,
        region=region,
        flagged_models=wizard.flagged_models,
        roster=roster,
    ):
        console.say(line)

    if not args.deploy:
        return EXIT_OK
    try:
        return deploy(
            console,
            output=output,
            profile=args.profile,
            region=args.region,
            acknowledge_logging_overwrite=wizard.acknowledged_logging_overwrite,
            yes=args.yes,
            admin_ui=flow.truthy(written.get("admin_ui")),
        )
    except Aborted as exc:
        print(f"setup: aborted ({exc}); {output} was written but not deployed.", file=sys.stderr)
        return EXIT_FAILED
