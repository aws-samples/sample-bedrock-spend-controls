"""Command-line interface for the preflight checks.

    python -m tools.preflight --config cdk/config/demo.json [--profile P]
        [--region R] [--account-id A] [--set key=value ...]
        [--acknowledge-logging-overwrite] [--sample-jwt TOKEN|@file]
        [--app-role-arn ARN] [--checks a,b] [--json]

Exit status: 0 when no check failed, 1 when at least one failed, 2 for a
usage or configuration error (the configuration validator's messages are
printed to stderr).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checks import CHECKS, run
from .context import (
    DEFAULT_CONFIG_PATH,
    Options,
    PreflightError,
    build_context,
    configuration_error,
    parse_overrides,
)

EXIT_OK = 0
EXIT_FAILED_CHECKS = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.preflight",
        description=(
            "Read-only checks that a deployment of Bedrock Spend Controls will "
            "succeed in this account and Region: toolchain, credentials, CDK "
            "bootstrap, Bedrock model access, invocation logging ownership, Lambda "
            "concurrency, OIDC issuer, invoker principals, SCP bypass, cost "
            "estimate, Region support and the admin console build."
        ),
        epilog=(
            "Checks: " + ", ".join(CHECKS) + ". Exit status 0 = all checks passed "
            "(warnings allowed), 1 = at least one check failed, 2 = usage or "
            "configuration error."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        metavar="PATH",
        help="deployment JSON file (default: cdk/config/demo.json)",
    )
    parser.add_argument("--profile", metavar="NAME", help="AWS CLI profile to use")
    parser.add_argument(
        "--region",
        metavar="REGION",
        help="target Region (default: AWS_REGION, then the profile's region)",
    )
    parser.add_argument(
        "--account-id",
        metavar="ID",
        help="expected AWS account; the credentials check fails on a mismatch",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "override a deployment key before validation (repeatable); lists are "
            "comma-separated, JSON is accepted for objects and arrays"
        ),
    )
    parser.add_argument(
        "--acknowledge-logging-overwrite",
        action="store_true",
        help=(
            "accept that manage_invocation_logging=true overwrites the Region's "
            "existing Bedrock invocation logging configuration (fail -> warn)"
        ),
    )
    parser.add_argument(
        "--sample-jwt",
        metavar="TOKEN|@FILE",
        help="a token from your IdP whose claims the oidc_issuer check inspects (never verified or sent)",
    )
    parser.add_argument(
        "--app-role-arn",
        metavar="ARN",
        help="application role to simulate bedrock:InvokeModel for (scp_bypass check)",
    )
    parser.add_argument(
        "--require-admin-ui-build",
        action="store_true",
        help="treat a missing admin-ui/dist as a failure instead of a warning",
    )
    parser.add_argument(
        "--checks",
        metavar="NAME[,NAME...]",
        help="run only these checks (default: all, in the listed order)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON ({account, region, ok, results[]})",
    )
    return parser


def read_sample_jwt(value: str | None) -> str | None:
    """``--sample-jwt`` accepts the token itself or ``@path`` to a file."""
    if not value:
        return None
    if value.startswith("@"):
        path = Path(value[1:]).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PreflightError(f"--sample-jwt: cannot read {path}: {exc.strerror or exc}") from None
    return value.strip()


def parse_check_names(value: str | None) -> list[str] | None:
    if not value:
        return None
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        raise PreflightError(
            f"unknown check(s): {', '.join(unknown)}; available: {', '.join(CHECKS)}"
        )
    return names or None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        names = parse_check_names(args.checks)
        options = Options(
            acknowledge_logging_overwrite=args.acknowledge_logging_overwrite,
            sample_jwt=read_sample_jwt(args.sample_jwt),
            app_role_arn=args.app_role_arn or None,
            admin_ui_build_required=args.require_admin_ui_build,
        )
        context = build_context(
            Path(args.config),
            profile=args.profile,
            region=args.region,
            account_id=args.account_id,
            overrides=parse_overrides(args.overrides),
            options=options,
        )
    except PreflightError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return EXIT_USAGE
    skipped = configuration_error()
    if skipped:
        print(
            "preflight: configuration validation skipped, cdk/stacks/configuration.py "
            f"could not be imported ({skipped}); install cdk/requirements.txt to enable it",
            file=sys.stderr,
        )
    report = run(context, names)
    print(report.to_json() if args.json else report.to_text())
    return EXIT_OK if report.ok else EXIT_FAILED_CHECKS
