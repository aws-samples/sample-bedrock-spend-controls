"""The preflight checks.

Each check is a function ``(Context) -> CheckResult`` registered in
:data:`CHECKS` (a fixed order). :func:`run` executes a selection and turns
AWS API errors into results instead of tracebacks: an access-denied error
becomes a ``warn`` whose fix names the IAM action to grant (see
:data:`REQUIRED_ACTIONS`), any other error becomes a ``fail``. Checks only
read: no call here creates, changes, or deletes anything.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .context import Context
from .report import CheckResult, Report

# --- constants -------------------------------------------------------------

TITLES: dict[str, str] = {
    "toolchain": "Local toolchain",
    "credentials": "AWS credentials",
    "bootstrap": "CDK bootstrap",
    "bedrock_model_access": "Bedrock model access",
    "invocation_logging": "Bedrock invocation logging",
    "lambda_concurrency": "Lambda concurrency",
    "oidc_issuer": "OIDC issuer",
    "invoker_principals": "Invoker principals",
    "scp_bypass": "SCP bypass",
    "quotas_cost": "Cost estimate",
    "region_support": "Region support",
    "admin_ui_build": "Admin console build",
}

# Minimum read-only IAM actions each check needs. Rendered in
# docs/installer.md so the preflight can run under a ReadOnly role.
REQUIRED_ACTIONS: dict[str, tuple[str, ...]] = {
    "toolchain": (),
    "credentials": ("sts:GetCallerIdentity",),
    "bootstrap": ("cloudformation:DescribeStacks", "ssm:GetParameter"),
    "bedrock_model_access": (
        "bedrock:GetFoundationModelAvailability",
        "bedrock:GetInferenceProfile",
    ),
    "invocation_logging": (
        "bedrock:GetModelInvocationLoggingConfiguration",
        "logs:DescribeLogGroups",
        "logs:FilterLogEvents",
    ),
    "lambda_concurrency": ("lambda:GetAccountSettings",),
    "oidc_issuer": (),
    "invoker_principals": ("iam:GetRole", "iam:GetUser"),
    "scp_bypass": ("iam:SimulatePrincipalPolicy",),
    "quotas_cost": (),
    "region_support": ("lambda:GetLayerVersion",),
    "admin_ui_build": (),
}

ACCESS_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "AuthorizationError",
        "NotAuthorized",
        "UnauthorizedException",
        "UnauthorizedOperation",
        "UnrecognizedClientException",
    }
)

NODE_MIN = (20,)
PYTHON_MIN = (3, 12)
AWS_CLI_MAJOR = 2
SUBPROCESS_TIMEOUT_SECONDS = 20

BOOTSTRAP_STACK_NAME = "CDKToolkit"
BOOTSTRAP_VERSION_PARAMETER = "/cdk-bootstrap/hnb659fds/version"
# Oldest bootstrap template this sample is tested against (CDK CLI 2.130+).
BOOTSTRAP_MIN_VERSION = 21
# Template version shipped with the CDK CLI pinned in cdk/package.json
# (aws-cdk 2.1137 / aws-cdk-lib 2.261). Older bootstraps still deploy but
# miss newer roles and permissions; the check warns below this.
BOOTSTRAP_CURRENT_VERSION = 28

# Log group the stack creates when it manages invocation logging; see
# cdk/stacks/spend_controls_stack.py (BedrockInvocationLogs).
MANAGED_INVOCATION_LOG_GROUP = "/bedrock/spend-controls/model-invocations"
LOG_ACTIVITY_WINDOW_SECONDS = 24 * 3600

# The stack pins the five enforcement workers at one reserved execution
# each; Lambda keeps at least 10 unreserved for everything else.
ENFORCEMENT_RESERVED_CONCURRENCY = 5
LAMBDA_MIN_UNRESERVED = 10

HTTP_TIMEOUT_SECONDS = 10

# Cross-Region inference-profile ID prefixes; keep in sync with
# cdk/stacks/spend_controls_stack.py (_CR_PROFILE_PREFIXES).
_CR_PROFILE_PREFIXES = frozenset(
    {"us", "eu", "apac", "jp", "au", "ca", "sa", "global", "us-gov"}
)

# Fallback list of Regions where account 753240598075 publishes the public
# Lambda Web Adapter layer the stack references (LambdaAdapterLayerX86), used
# only when lambda:GetLayerVersion cannot answer for the target Region. The
# non-opt-in entries were confirmed with GetLayerVersionByArn (version 30);
# opt-in Regions follow the upstream README. Deployments elsewhere must
# publish the layer themselves and set adapter_layer_arn.
ADAPTER_LAYER_REGIONS = frozenset(
    {
        "af-south-1",
        "ap-east-1",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-south-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ca-central-1",
        "ca-west-1",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
    }
)
# CloudFront exists in these partitions only (the admin console needs it).
CLOUDFRONT_PARTITIONS = frozenset({"aws", "aws-cn"})
# Fallbacks when the bundled botocore endpoint data has no entry.
_COGNITO_FALLBACK_REGIONS = ADAPTER_LAYER_REGIONS | {"us-gov-west-1", "us-gov-east-1"}
_BEDROCK_FALLBACK_REGIONS = ADAPTER_LAYER_REGIONS - {"ap-east-1"}

_REGION_RE = re.compile(r"^[a-z]{2}(?:-gov|-iso[a-z]?)?-[a-z]+-\d$")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


# --- shared helpers ----------------------------------------------------------


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _error_message(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Message", "")) or str(exc)


def _is_access_denied(exc: ClientError) -> bool:
    code = _error_code(exc)
    if code in ACCESS_DENIED_CODES:
        return True
    return "not authorized" in _error_message(exc).lower()


def _access_denied_fix(name: str) -> str:
    actions = REQUIRED_ACTIONS.get(name) or ("the listed read-only actions",)
    return f"grant {', '.join(actions)} to run this check"


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _which(tool: str) -> str | None:
    return shutil.which(tool)


def _run_version(argv: list[str]) -> str | None:
    """Run ``argv`` (an argument list, never a shell) and return its first
    line of output, or ``None`` when the tool fails or is missing."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    return output.splitlines()[0] if output else ""


def _python_version() -> tuple[int, int, int]:
    return sys.version_info[:3]


def _parse_version(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def _arn_parts(arn: str) -> dict[str, str]:
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return {}
    return {
        "partition": parts[1],
        "service": parts[2],
        "region": parts[3],
        "account": parts[4],
        "resource": parts[5],
    }


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Return a JWT's claims WITHOUT verifying the signature (the check only
    inspects claim names and values; the broker does the verification)."""
    segments = token.strip().split(".")
    if len(segments) != 3:
        raise ValueError("a JWT has three dot-separated segments")
    payload = segments[1] + "=" * (-len(segments[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"payload is not base64url JSON: {exc}") from None
    if not isinstance(claims, dict):
        raise ValueError("payload is not a JSON object")
    return claims


def _fetch_json(url: str, purpose: str) -> Any:
    """GET ``url`` (https only) and parse the JSON body."""
    if not url.lower().startswith("https://"):
        raise ValueError(f"{purpose} must be an https:// URL, got {url!r}")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "bedrock-spend-controls-preflight",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{purpose} returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"{purpose} could not be fetched: {reason}") from None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{purpose} is not valid JSON: {exc}") from None


def _service_regions(
    ctx: Context, service: str, fallback: frozenset[str], *, partition: str | None = None
) -> frozenset[str]:
    """Regions offering ``service`` in a partition, from the endpoint data
    bundled with botocore (``fallback`` when it has no entry)."""
    try:
        regions = ctx.session.get_available_regions(
            service, partition_name=partition or ctx.partition
        )
    except Exception:
        regions = []
    regions = {region for region in regions if _REGION_RE.match(region)}
    return frozenset(regions) if regions else fallback


# --- checks ------------------------------------------------------------------


def check_toolchain(ctx: Context) -> CheckResult:
    """Node 20+, npm, Python 3.12+ with pip, AWS CLI v2 and git on PATH."""
    found: list[str] = []
    problems: list[str] = []
    fixes: list[str] = []

    def external(tool: str, minimum: tuple[int, ...] | None, label: str, fix: str) -> None:
        if _which(tool) is None:
            problems.append(f"{label} not found on PATH")
            fixes.append(fix)
            return
        output = _run_version([tool, "--version"])
        version = _parse_version(output)
        if output is None or version is None:
            problems.append(f"{label} is installed but '{tool} --version' failed")
            fixes.append(fix)
            return
        shown = output.strip()
        if minimum is not None and version[: len(minimum)] < minimum:
            problems.append(f"{label} {shown} is older than the required {'.'.join(map(str, minimum))}")
            fixes.append(fix)
        found.append(f"{tool}: {shown}")

    external("node", NODE_MIN, "Node.js", "install Node.js 20 or newer (for example: nvm install 20)")
    external("npm", None, "npm", "install npm (it ships with Node.js)")

    python = _python_version()
    python_text = ".".join(map(str, python))
    if python[:2] < PYTHON_MIN:
        problems.append(f"Python {python_text} is older than the required {'.'.join(map(str, PYTHON_MIN))}")
        fixes.append("run the installer with Python 3.12 or newer (python3.12 -m venv .venv)")
    pip = _run_version([sys.executable, "-m", "pip", "--version"])
    if pip is None:
        problems.append(f"pip is not available for {sys.executable}")
        fixes.append(f"run: {sys.executable} -m ensurepip --upgrade")
    found.append(f"python: {python_text}" + (f" ({pip.split(' from ')[0]})" if pip else ""))

    external("aws", (AWS_CLI_MAJOR,), "AWS CLI", "install AWS CLI v2 (https://aws.amazon.com/cli/)")
    external("git", None, "git", "install git")

    detail = "\n".join(found)
    if problems:
        return CheckResult(
            "fail", "toolchain", TITLES["toolchain"],
            "\n".join(problems + found), "\n".join(dict.fromkeys(fixes)),
        )
    return CheckResult("pass", "toolchain", TITLES["toolchain"], detail)


# STS rejects every token sent to a Regional endpoint of an opt-in Region
# the account has not enabled, with the same codes an invalid key gets.
_DISABLED_REGION_CODES = frozenset({"InvalidClientTokenId", "UnrecognizedClientException"})
# Where to retry GetCallerIdentity to tell "bad credentials" from "Region
# not enabled": a Region every account in the partition can reach.
_HOME_REGION = {"aws": "us-east-1", "aws-cn": "cn-north-1", "aws-us-gov": "us-gov-west-1"}

# Checks that call Regional AWS endpoints and are therefore pointless (and
# noisy) once the credentials check has found the Region disabled.
REGIONAL_CHECKS = frozenset(
    {
        "bootstrap",
        "bedrock_model_access",
        "invocation_logging",
        "lambda_concurrency",
        "invoker_principals",
        "scp_bypass",
        "region_support",
    }
)


def check_credentials(ctx: Context) -> CheckResult:
    """``sts:GetCallerIdentity``: who deploys, into which account/Region."""
    try:
        identity = ctx.identity()
    except ClientError as exc:
        if _error_code(exc) not in _DISABLED_REGION_CODES:
            raise
        home = _HOME_REGION.get(ctx.partition, "us-east-1")
        if home == ctx.region:
            raise
        try:
            probe = ctx.clients("sts", region=home).get_caller_identity()
        except ClientError:
            raise exc from None  # the credentials really are bad
        account = probe["Account"]
        if not ctx.account:
            ctx.account = account
        ctx.region_disabled = True
        return CheckResult(
            "fail", "credentials", TITLES["credentials"],
            f"{probe['Arn']}\naccount {account} has not enabled Region {ctx.region} "
            "(an opt-in Region): its endpoints reject every request",
            f"enable the Region (aws account enable-region --region-name {ctx.region}, "
            "then wait a few minutes) or deploy in a Region the account already has",
        )
    arn = identity["Arn"]
    account = identity["Account"]
    detail = f"{arn}\naccount {account}, region {ctx.region}, partition {ctx.partition}"
    if ctx.expected_account and ctx.expected_account != account:
        return CheckResult(
            "fail", "credentials", TITLES["credentials"],
            f"{detail}\n--account-id {ctx.expected_account} does not match the credentials' account {account}",
            "use a profile for the target account, or drop --account-id",
        )
    principal = arn.split(":", 5)[-1].lower()
    if "readonly" in principal or "viewonly" in principal:
        return CheckResult(
            "warn", "credentials", TITLES["credentials"],
            f"{detail}\nthe principal name suggests read-only access; preflight works but cdk deploy will not",
            "deploy with credentials that can create the stack's resources (or assume the CDK bootstrap roles)",
        )
    return CheckResult("pass", "credentials", TITLES["credentials"], detail)


def _pinned_cdk_lib(ctx: Context) -> str:
    try:
        text = (ctx.root / "cdk" / "requirements.txt").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"aws-cdk-lib==([\w.]+)", text)
    return match.group(1) if match else "unknown"


def check_bootstrap(ctx: Context) -> CheckResult:
    """``CDKToolkit`` exists and its template version is recent enough."""
    cloudformation = ctx.clients("cloudformation")
    fix_bootstrap = (
        f"run: npx cdk bootstrap aws://{ctx.account or '<account>'}/{ctx.region} "
        "(from the cdk/ directory)"
    )
    try:
        stacks = cloudformation.describe_stacks(StackName=BOOTSTRAP_STACK_NAME)["Stacks"]
    except ClientError as exc:
        if _error_code(exc) == "ValidationError" and "does not exist" in _error_message(exc):
            return CheckResult(
                "fail", "bootstrap", TITLES["bootstrap"],
                f"stack {BOOTSTRAP_STACK_NAME} not found in {ctx.region}: the environment is not bootstrapped",
                fix_bootstrap,
            )
        raise
    stack = stacks[0]
    status = stack.get("StackStatus", "")
    outputs = {
        output.get("OutputKey"): output.get("OutputValue")
        for output in stack.get("Outputs", [])
    }
    version_text: str | None = None
    source = ""
    try:
        parameter = ctx.clients("ssm").get_parameter(Name=BOOTSTRAP_VERSION_PARAMETER)
        version_text = parameter["Parameter"]["Value"]
        source = BOOTSTRAP_VERSION_PARAMETER
    except ClientError as exc:
        if not (_is_access_denied(exc) or _error_code(exc) == "ParameterNotFound"):
            raise
        version_text = outputs.get("BootstrapVersion")
        source = "stack output BootstrapVersion"
    pinned = _pinned_cdk_lib(ctx)
    if not version_text:
        return CheckResult(
            "warn", "bootstrap", TITLES["bootstrap"],
            f"{BOOTSTRAP_STACK_NAME} is {status} but its template version could not be read",
            f"grant ssm:GetParameter on {BOOTSTRAP_VERSION_PARAMETER}, or re-run: npx cdk bootstrap",
        )
    try:
        version = int(str(version_text).strip())
    except ValueError:
        return CheckResult(
            "warn", "bootstrap", TITLES["bootstrap"],
            f"unexpected bootstrap version {version_text!r} from {source}", fix_bootstrap,
        )
    detail = (
        f"{BOOTSTRAP_STACK_NAME} is {status}, template version {version} ({source}); "
        f"aws-cdk-lib {pinned} pinned"
    )
    if "FAILED" in status or "ROLLBACK" in status or status.startswith("DELETE"):
        return CheckResult(
            "fail", "bootstrap", TITLES["bootstrap"],
            f"{detail}\nthe bootstrap stack is not healthy", fix_bootstrap,
        )
    if version < BOOTSTRAP_MIN_VERSION:
        return CheckResult(
            "fail", "bootstrap", TITLES["bootstrap"],
            f"{detail}\nversion {version} is older than the minimum {BOOTSTRAP_MIN_VERSION}",
            fix_bootstrap,
        )
    if version < BOOTSTRAP_CURRENT_VERSION:
        return CheckResult(
            "warn", "bootstrap", TITLES["bootstrap"],
            f"{detail}\nversion {version} is older than the template the pinned CDK ships ({BOOTSTRAP_CURRENT_VERSION})",
            fix_bootstrap,
        )
    return CheckResult("pass", "bootstrap", TITLES["bootstrap"], detail)


def check_bedrock_model_access(ctx: Context) -> CheckResult:
    """Every allowed model (and every workload model) is enabled for the
    account in its Region; inference profiles are resolved to their
    underlying foundation models first."""
    problems: list[str] = []
    warnings: list[str] = []
    denied: list[str] = []
    checked: list[str] = []
    unchecked: list[str] = []
    seen: set[tuple[str, str]] = set()

    def availability(model_id: str, region: str | None) -> None:
        key = (region or ctx.region, model_id)
        if key in seen:
            return
        seen.add(key)
        label = model_id if region in (None, ctx.region) else f"{model_id} ({region})"
        try:
            response = ctx.clients("bedrock", region=region).get_foundation_model_availability(
                modelId=model_id
            )
        except ClientError as exc:
            if _is_access_denied(exc):
                denied.append(label)
                return
            if _error_code(exc) in ("ResourceNotFoundException", "ValidationException"):
                problems.append(f"{label}: {_error_message(exc)}")
                return
            raise
        agreement = (response.get("agreementAvailability") or {}).get("status")
        authorization = response.get("authorizationStatus")
        regional = response.get("regionAvailability")
        if agreement != "AVAILABLE" or authorization != "AUTHORIZED" or regional != "AVAILABLE":
            problems.append(
                f"{label}: agreement={agreement}, authorization={authorization}, region={regional}"
            )
        else:
            checked.append(label)

    def profile(profile_id: str, region: str | None = None) -> None:
        try:
            response = ctx.clients("bedrock", region=region).get_inference_profile(
                inferenceProfileIdentifier=profile_id
            )
        except ClientError as exc:
            if _is_access_denied(exc):
                denied.append(profile_id)
                return
            if _error_code(exc) in ("ResourceNotFoundException", "ValidationException"):
                problems.append(f"inference profile {profile_id}: {_error_message(exc)}")
                return
            raise
        models = response.get("models") or []
        if not models:
            problems.append(f"inference profile {profile_id} lists no underlying models")
        for entry in models:
            parts = _arn_parts(entry.get("modelArn", ""))
            model_id = parts.get("resource", "").split("/", 1)[-1]
            model_region = parts.get("region") or None
            if not model_id:
                unchecked.append(entry.get("modelArn", "?"))
                continue
            availability(model_id, model_region if model_region != ctx.region else None)

    for arn in _as_list(ctx.setting("allowed_model_arns")):
        if arn == "*":
            warnings.append(
                "allowed_model_arns contains '*': vended sessions may call any model "
                "(fine for a demo; production should list exact ARNs)"
            )
            continue
        resource = _arn_parts(arn).get("resource", "")
        kind, _, identifier = resource.partition("/")
        if not identifier or "*" in identifier or "?" in identifier:
            unchecked.append(arn)
        elif kind == "foundation-model":
            availability(identifier, None)
        elif kind == "inference-profile":
            profile(identifier)
        else:
            unchecked.append(arn)

    for workload in ctx.workloads():
        model = getattr(workload, "model", "")
        if not model:
            continue
        if model.split(".", 1)[0] in _CR_PROFILE_PREFIXES:
            profile(model)
        else:
            availability(model, None)

    lines: list[str] = []
    if checked:
        lines.append("available: " + ", ".join(checked))
    if unchecked:
        lines.append("not checked (wildcard or unsupported resource type): " + ", ".join(unchecked))
    if denied:
        lines.append("access denied when checking: " + ", ".join(denied))
    lines.extend(warnings)
    if problems:
        lines = ["not available: " + "; ".join(problems)] + lines
        return CheckResult(
            "fail", "bedrock_model_access", TITLES["bedrock_model_access"], "\n".join(lines),
            "enable model access in the Bedrock console (Model access) for the listed models, "
            "or remove them from allowed_model_arns / workloads",
        )
    if denied:
        return CheckResult(
            "warn", "bedrock_model_access", TITLES["bedrock_model_access"], "\n".join(lines),
            _access_denied_fix("bedrock_model_access"),
        )
    if warnings:
        return CheckResult(
            "warn", "bedrock_model_access", TITLES["bedrock_model_access"], "\n".join(lines),
            "list exact foundation-model and inference-profile ARNs in allowed_model_arns for production",
        )
    if not checked:
        lines.append("no concrete model ARNs to check")
    return CheckResult("pass", "bedrock_model_access", TITLES["bedrock_model_access"], "\n".join(lines))


def check_invocation_logging(ctx: Context) -> CheckResult:
    """The account-wide Bedrock invocation logging setting is compatible
    with what the stack will do with it."""
    bedrock = ctx.clients("bedrock")
    current = bedrock.get_model_invocation_logging_configuration().get("loggingConfig") or {}
    current_group = (current.get("cloudWatchConfig") or {}).get("logGroupName")
    current_bucket = (current.get("s3Config") or {}).get("bucketName")
    if current_group:
        current_text = f"CloudWatch log group {current_group}"
    elif current_bucket:
        current_text = f"S3 bucket {current_bucket}"
    elif current:
        current_text = "an existing configuration"
    else:
        current_text = "nothing"

    manage = _truthy(ctx.setting("manage_invocation_logging"))
    group = str(ctx.setting("invocation_log_group_name") or "").strip()

    if manage:
        if not current:
            return CheckResult(
                "pass", "invocation_logging", TITLES["invocation_logging"],
                f"no invocation logging configured in {ctx.region}; the stack will enable it to {MANAGED_INVOCATION_LOG_GROUP}",
            )
        if current_group == MANAGED_INVOCATION_LOG_GROUP:
            return CheckResult(
                "pass", "invocation_logging", TITLES["invocation_logging"],
                f"invocation logging already delivers to {MANAGED_INVOCATION_LOG_GROUP} (managed by this stack)",
            )
        detail = (
            f"invocation logging in {ctx.region} currently delivers to {current_text}; "
            "manage_invocation_logging=true makes the deploy OVERWRITE this account-wide setting"
        )
        if ctx.options.acknowledge_logging_overwrite:
            return CheckResult(
                "warn", "invocation_logging", TITLES["invocation_logging"],
                f"{detail} (acknowledged with --acknowledge-logging-overwrite)",
                "the previous configuration is not restored on destroy; note it down before deploying",
            )
        return CheckResult(
            "fail", "invocation_logging", TITLES["invocation_logging"], detail,
            "re-run with --acknowledge-logging-overwrite to accept, or deploy with "
            f"manage_invocation_logging=false and invocation_log_group_name={current_group or '<existing group>'}",
        )

    if not group:
        return CheckResult(
            "fail", "invocation_logging", TITLES["invocation_logging"],
            "neither manage_invocation_logging=true nor invocation_log_group_name is set",
            "set manage_invocation_logging=true (demo account) or invocation_log_group_name=<existing group>",
        )

    logs = ctx.clients("logs")
    groups = logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", [])
    match = next((entry for entry in groups if entry.get("logGroupName") == group), None)
    if match is None:
        return CheckResult(
            "fail", "invocation_logging", TITLES["invocation_logging"],
            f"log group {group} does not exist in {ctx.region}",
            "create it and point Bedrock invocation logging at it, or set invocation_log_group_name "
            "to the group that already receives invocation logs",
        )
    notes = [f"log group {group} exists"]
    warnings: list[str] = []
    if current_group != group:
        warnings.append(
            f"Bedrock invocation logging in {ctx.region} delivers to {current_text}, not to {group}: "
            "metering will see no usage"
        )
    stored = int(match.get("storedBytes") or 0)
    if stored > 0:
        notes.append(f"{stored:,} bytes stored")
    else:
        try:
            since = int((time.time() - LOG_ACTIVITY_WINDOW_SECONDS) * 1000)
            events = logs.filter_log_events(logGroupName=group, startTime=since, limit=1).get("events", [])
        except ClientError as exc:
            if not _is_access_denied(exc):
                raise
            events = []
            warnings.append("could not read recent events (logs:FilterLogEvents denied)")
        if events:
            notes.append("events received in the last 24 h")
        else:
            warnings.append("no invocation logs seen in 24 h")
    if warnings:
        return CheckResult(
            "warn", "invocation_logging", TITLES["invocation_logging"],
            "\n".join(notes + warnings),
            "confirm Bedrock model invocation logging is enabled and delivering to this log group",
        )
    return CheckResult("pass", "invocation_logging", TITLES["invocation_logging"], "\n".join(notes))


def check_lambda_concurrency(ctx: Context) -> CheckResult:
    """The account can spare the reserved concurrency the stack pins."""
    limits = ctx.clients("lambda").get_account_settings().get("AccountLimit", {})
    unreserved = int(limits.get("UnreservedConcurrentExecutions") or 0)
    total = int(limits.get("ConcurrentExecutions") or 0)
    reserve = _truthy(ctx.setting("reserve_enforcement_concurrency"))
    if not reserve:
        return CheckResult(
            "pass", "lambda_concurrency", TITLES["lambda_concurrency"],
            f"{unreserved} of {total} concurrent executions unreserved; "
            "reserve_enforcement_concurrency=false so none are reserved",
        )
    remaining = unreserved - ENFORCEMENT_RESERVED_CONCURRENCY
    detail = (
        f"{unreserved} of {total} concurrent executions unreserved; the stack reserves "
        f"{ENFORCEMENT_RESERVED_CONCURRENCY}, leaving {remaining} (Lambda requires at least {LAMBDA_MIN_UNRESERVED})"
    )
    if remaining < LAMBDA_MIN_UNRESERVED:
        return CheckResult(
            "fail", "lambda_concurrency", TITLES["lambda_concurrency"], detail,
            "set reserve_enforcement_concurrency=false or request a quota increase "
            "(Lambda 'Concurrent executions') before deploying",
        )
    return CheckResult("pass", "lambda_concurrency", TITLES["lambda_concurrency"], detail)


def check_oidc_issuer(ctx: Context) -> CheckResult:
    """A bring-your-own issuer publishes discovery and a JWKS the broker can
    use; an optional sample token carries the configured claims."""
    issuer = str(ctx.setting("jwt_issuer") or "").strip()
    if not issuer:
        return CheckResult(
            "skip", "oidc_issuer", TITLES["oidc_issuer"],
            "jwt_issuer is not set: the stack creates a Cognito user pool as the identity provider",
        )
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    try:
        discovery = _fetch_json(
            issuer.rstrip("/") + "/.well-known/openid-configuration", "OIDC discovery document"
        )
    except ValueError as exc:
        return CheckResult(
            "fail", "oidc_issuer", TITLES["oidc_issuer"], str(exc),
            "check jwt_issuer (https URL of the IdP) and that this machine can reach it",
        )
    if not isinstance(discovery, dict):
        discovery = {}
    published = discovery.get("issuer")
    if published != issuer:
        problems.append(
            f"discovery publishes issuer {published!r} but jwt_issuer is {issuer!r}; "
            "the broker compares them exactly"
        )
    else:
        notes.append(f"issuer {issuer}")

    jwks_url = str(ctx.setting("jwt_jwks_url") or "").strip() or discovery.get("jwks_uri")
    if not isinstance(jwks_url, str) or not jwks_url:
        problems.append("discovery document has no jwks_uri (set jwt_jwks_url explicitly)")
    else:
        try:
            jwks = _fetch_json(jwks_url, "JWKS")
            keys = jwks.get("keys") if isinstance(jwks, dict) else None
            if not isinstance(keys, list) or not keys:
                problems.append(f"JWKS at {jwks_url} contains no keys")
            else:
                notes.append(f"JWKS {jwks_url} ({len(keys)} key{'s' if len(keys) != 1 else ''})")
        except ValueError as exc:
            problems.append(str(exc))

    audience = str(ctx.setting("jwt_audience") or "").strip()
    audiences = [item.strip() for item in audience.split(",") if item.strip()]
    if not audiences:
        problems.append("jwt_audience is empty: the broker would accept tokens minted for any client")

    token = ctx.options.sample_jwt
    if token:
        try:
            claims = _decode_jwt_payload(token)
        except ValueError as exc:
            problems.append(f"--sample-jwt could not be decoded: {exc}")
            claims = None
        if claims is not None:
            user_claim = str(ctx.setting("jwt_user_claim") or "sub")
            if user_claim not in claims:
                problems.append(
                    f"sample token has no '{user_claim}' claim (jwt_user_claim); "
                    f"claims present: {', '.join(sorted(claims))}"
                )
            admin_claim = str(ctx.setting("admin_jwt_claim") or "")
            if admin_claim and admin_claim not in claims:
                warnings.append(
                    f"sample token has no '{admin_claim}' claim (admin_jwt_claim); "
                    "administrators' tokens must carry it"
                )
            token_audiences = claims.get("aud")
            if isinstance(token_audiences, str):
                token_audiences = [token_audiences]
            if audiences and not (isinstance(token_audiences, list) and set(token_audiences) & set(audiences)):
                problems.append(
                    f"sample token aud {token_audiences!r} does not include jwt_audience {audiences[0]!r}"
                )
            token_issuer = claims.get("iss")
            if token_issuer and token_issuer != issuer:
                problems.append(f"sample token iss {token_issuer!r} differs from jwt_issuer")
            if not problems:
                notes.append("sample token carries the configured claims")

    detail = "\n".join(notes + warnings + problems)
    if problems:
        return CheckResult(
            "fail", "oidc_issuer", TITLES["oidc_issuer"], detail,
            "align jwt_issuer, jwt_audience, jwt_user_claim and admin_jwt_claim with what the IdP issues",
        )
    if warnings:
        return CheckResult(
            "warn", "oidc_issuer", TITLES["oidc_issuer"], detail,
            "configure the IdP to include the admin claim in administrators' tokens",
        )
    return CheckResult("pass", "oidc_issuer", TITLES["oidc_issuer"], detail)


def _principal(arn: str) -> tuple[str, str, str]:
    """Return ``(account, kind, name)`` for an IAM/STS principal ARN; ``kind``
    is root, role, user, assumed-role, or unknown."""
    parts = _arn_parts(arn)
    account = parts.get("account", "")
    resource = parts.get("resource", "")
    if parts.get("service") == "iam":
        if resource == "root":
            return account, "root", ""
        kind, _, path = resource.partition("/")
        if kind in ("role", "user") and path:
            return account, kind, path.rsplit("/", 1)[-1]
    elif parts.get("service") == "sts":
        kind, _, path = resource.partition("/")
        if kind == "assumed-role" and path:
            return account, "assumed-role", path.split("/", 1)[0]
    return account, "unknown", resource


def check_invoker_principals(ctx: Context) -> CheckResult:
    """Each ``invoker_principal_arns`` entry exists (roles/users via IAM)."""
    arns = _as_list(ctx.setting("invoker_principal_arns"))
    if not arns:
        return CheckResult(
            "skip", "invoker_principals", TITLES["invoker_principals"],
            "invoker_principal_arns is empty: the broker URL is granted to this account's root principal",
        )
    iam = ctx.clients("iam")
    found: list[str] = []
    missing: list[str] = []
    denied: list[str] = []
    unverified: list[str] = []
    for arn in arns:
        account, kind, name = _principal(arn)
        if ctx.account and account and account != ctx.account:
            unverified.append(f"{arn} (account {account}; cross-account principals are not verified)")
            continue
        try:
            if kind == "root":
                found.append(f"{arn} (account root)")
            elif kind in ("role", "assumed-role"):
                iam.get_role(RoleName=name)
                found.append(arn)
            elif kind == "user":
                iam.get_user(UserName=name)
                found.append(arn)
            else:
                unverified.append(f"{arn} (unrecognised principal type)")
        except ClientError as exc:
            if _error_code(exc) == "NoSuchEntity":
                missing.append(arn)
            elif _is_access_denied(exc):
                denied.append(arn)
            else:
                raise
    lines: list[str] = []
    if found:
        lines.append("found: " + ", ".join(found))
    if missing:
        lines.append("missing: " + ", ".join(missing))
    if denied:
        lines.append("access denied: " + ", ".join(denied))
    lines.extend(unverified)
    if missing:
        return CheckResult(
            "fail", "invoker_principals", TITLES["invoker_principals"], "\n".join(lines),
            "create the principal or remove it from invoker_principal_arns",
        )
    if denied:
        return CheckResult(
            "warn", "invoker_principals", TITLES["invoker_principals"], "\n".join(lines),
            _access_denied_fix("invoker_principals"),
        )
    if unverified:
        return CheckResult(
            "warn", "invoker_principals", TITLES["invoker_principals"], "\n".join(lines),
            "verify these principals exist in their own account",
        )
    return CheckResult("pass", "invoker_principals", TITLES["invoker_principals"], "\n".join(lines))


def check_scp_bypass(ctx: Context) -> CheckResult:
    """An application role cannot call ``bedrock:InvokeModel`` directly, so
    the only path to Bedrock is a vended session."""
    role_arn = ctx.options.app_role_arn
    if not role_arn:
        return CheckResult(
            "skip", "scp_bypass", TITLES["scp_bypass"],
            "no application role given",
            "pass --app-role-arn <role ARN> to simulate bedrock:InvokeModel for an application role; "
            "it must be denied (SCP or the stack's deny-direct-bedrock-invocation policy)",
        )
    resource = f"arn:{ctx.partition}:bedrock:{ctx.region}::foundation-model/*"
    response = ctx.clients("iam").simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=["bedrock:InvokeModel"],
        ResourceArns=[resource],
    )
    results = response.get("EvaluationResults") or []
    decision = results[0].get("EvalDecision", "unknown") if results else "unknown"
    detail = f"{role_arn}: bedrock:InvokeModel on {resource} -> {decision}"
    if decision in ("explicitDeny", "implicitDeny"):
        return CheckResult("pass", "scp_bypass", TITLES["scp_bypass"], detail)
    return CheckResult(
        "fail", "scp_bypass", TITLES["scp_bypass"],
        f"{detail}\nthe role can bypass the broker and call Bedrock unmetered",
        "attach the stack's deny-direct-bedrock-invocation managed policy to the role, or apply an SCP "
        "that denies bedrock:InvokeModel when aws:SourceIdentity is absent (see docs/integration.md)",
    )


def _load_estimator(ctx: Context) -> Any:
    path = ctx.root / "tools" / "estimate_cost.py"
    spec = importlib.util.spec_from_file_location("bsc_estimate_cost", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_quotas_cost(ctx: Context) -> CheckResult:
    """Monthly cost estimate from tools/estimate_cost.py for the scenario
    matching this configuration (demo or production)."""
    try:
        estimator = _load_estimator(ctx)
        assumptions_path = ctx.root / "docs" / "cost-estimate-assumptions.json"
        data = json.loads(assumptions_path.read_text(encoding="utf-8"))
        scenarios: dict[str, dict[str, Any]] = data["scenarios"]
        wanted = "production" if str(ctx.setting("jwt_issuer") or "").strip() else "demo"
        name = next(
            (key for key in scenarios if key.lower().startswith(wanted)), next(iter(scenarios))
        )
        assumptions = dict(scenarios[name])
        # Overlay what this configuration decides about the scenario.
        assumptions["admin_ui"] = _truthy(ctx.setting("admin_ui"))
        assumptions["reconciliation_enabled"] = _truthy(ctx.setting("reconciliation_enabled"))
        for source, target in (
            ("permission_lease_seconds", "lease_seconds"),
            ("usage_retention_days", "usage_retention_days"),
            ("revocation_reconcile_minutes", "revocation_reconcile_minutes"),
        ):
            value = ctx.setting(source)
            if value is not None:
                assumptions[target] = int(value)
        if ctx.config is not None:
            assumptions["workloads"] = len(ctx.workloads())
        rows = estimator.scenario(name, assumptions, data["unit_prices"])
        total = sum(cost for _, cost, _ in rows)
    except Exception as exc:  # the estimate is informational; never block
        return CheckResult(
            "warn", "quotas_cost", TITLES["quotas_cost"],
            f"cost estimate unavailable: {type(exc).__name__}: {exc}",
            "run: python tools/estimate_cost.py docs/cost-estimate-assumptions.json",
        )
    return CheckResult(
        "pass", "quotas_cost", TITLES["quotas_cost"],
        f"about {total:,.2f} USD/month for scenario '{name}' "
        f"({assumptions['active_users']:,} users, {assumptions['invocations_per_month']:,} invocations); "
        "assumptions and unit prices in docs/cost-estimate-assumptions.json (an estimate, not a bill)",
    )


# The public layer ARN the stack references when adapter_layer_arn is empty
# (cdk/stacks/spend_controls_stack.py); the version must match the stack.
ADAPTER_LAYER_VERSION = 30


def _adapter_layer_published(ctx: Context) -> bool | None:
    """Probe the public Lambda Web Adapter layer in the target Region.

    Returns True when ``GetLayerVersionByArn`` finds it, False when Lambda
    reports it missing, and None when the question could not be answered
    (no permission, no network, Region not enabled), in which case the
    caller falls back to the static Region list.
    """
    arn = (
        f"arn:{ctx.partition}:lambda:{ctx.region}:753240598075:layer:"
        f"LambdaAdapterLayerX86:{ADAPTER_LAYER_VERSION}"
    )
    try:
        ctx.clients("lambda").get_layer_version_by_arn(Arn=arn)
        return True
    except ClientError as exc:
        if _error_code(exc) == "ResourceNotFoundException":
            return False
        return None
    except Exception:  # noqa: BLE001  # offline, Region disabled, dummy credentials
        return None


def check_region_support(ctx: Context) -> CheckResult:
    """The target Region offers everything the stack deploys: Bedrock, the
    public Lambda Web Adapter layer, Cognito (demo IdP) and CloudFront
    (admin console)."""
    region = ctx.region
    problems: list[str] = []
    notes: list[str] = []
    bedrock_regions = _service_regions(ctx, "bedrock", _BEDROCK_FALLBACK_REGIONS)
    if region not in bedrock_regions:
        problems.append(f"Amazon Bedrock is not available in {region}")
    if str(ctx.setting("adapter_layer_arn") or "").strip():
        notes.append("adapter_layer_arn is set: the Lambda Web Adapter layer is not checked")
    else:
        # Ask Lambda first: the static list is a fallback for opt-in Regions
        # the account cannot reach and for runs without permissions.
        published = _adapter_layer_published(ctx)
        if published is False or (
            published is None
            and (ctx.partition != "aws" or region not in ADAPTER_LAYER_REGIONS)
        ):
            problems.append(
                f"the public Lambda Web Adapter layer (account 753240598075) is not published in {region}"
            )
        elif published is None:
            notes.append(
                "Lambda Web Adapter layer presence taken from the static Region list "
                "(lambda:GetLayerVersion could not confirm it)"
            )
    needs_cognito = not str(ctx.setting("jwt_issuer") or "").strip()
    if needs_cognito:
        cognito_regions = _service_regions(ctx, "cognito-idp", _COGNITO_FALLBACK_REGIONS)
        if region not in cognito_regions:
            problems.append(f"Amazon Cognito user pools are not available in {region}")
    if _truthy(ctx.setting("admin_ui")) and ctx.partition not in CLOUDFRONT_PARTITIONS:
        problems.append(f"Amazon CloudFront is not available in partition {ctx.partition}")
    if problems:
        supported = sorted(
            ADAPTER_LAYER_REGIONS
            & _service_regions(ctx, "bedrock", _BEDROCK_FALLBACK_REGIONS, partition="aws")
        )
        return CheckResult(
            "fail", "region_support", TITLES["region_support"], "\n".join(problems + notes),
            "deploy in a supported Region (" + ", ".join(supported) + "), or publish the Lambda Web "
            "Adapter layer yourself and set adapter_layer_arn; set jwt_issuer to bring your own IdP "
            "where Cognito is missing and admin_ui=false where CloudFront is missing",
        )
    notes.insert(0, f"{region} ({ctx.partition}) supports Bedrock, the Lambda Web Adapter layer"
                 + (", Cognito" if needs_cognito else "")
                 + (" and CloudFront" if _truthy(ctx.setting("admin_ui")) else ""))
    return CheckResult("pass", "region_support", TITLES["region_support"], "\n".join(notes))


def check_admin_ui_build(ctx: Context) -> CheckResult:
    """``admin-ui/dist`` exists when the console is enabled."""
    if not _truthy(ctx.setting("admin_ui")):
        return CheckResult(
            "skip", "admin_ui_build", TITLES["admin_ui_build"], "admin_ui=false: no console to build",
        )
    index = ctx.root / "admin-ui" / "dist" / "index.html"
    if index.is_file():
        built = datetime.fromtimestamp(index.stat().st_mtime, tz=timezone.utc)
        return CheckResult(
            "pass", "admin_ui_build", TITLES["admin_ui_build"],
            f"{index.relative_to(ctx.root)} built {built:%Y-%m-%d %H:%M} UTC",
        )
    status = "fail" if ctx.options.admin_ui_build_required else "warn"
    return CheckResult(
        status, "admin_ui_build", TITLES["admin_ui_build"],
        f"{index.relative_to(ctx.root)} is missing; cdk synth fails with admin_ui=true until the console is built",
        "run npm ci && npm run build in admin-ui/",
    )


# --- registry and runner ------------------------------------------------------

CHECKS: dict[str, Callable[[Context], CheckResult]] = {
    "toolchain": check_toolchain,
    "credentials": check_credentials,
    "bootstrap": check_bootstrap,
    "bedrock_model_access": check_bedrock_model_access,
    "invocation_logging": check_invocation_logging,
    "lambda_concurrency": check_lambda_concurrency,
    "oidc_issuer": check_oidc_issuer,
    "invoker_principals": check_invoker_principals,
    "scp_bypass": check_scp_bypass,
    "quotas_cost": check_quotas_cost,
    "region_support": check_region_support,
    "admin_ui_build": check_admin_ui_build,
}

assert set(CHECKS) == set(TITLES) == set(REQUIRED_ACTIONS)


def execute(name: str, ctx: Context) -> CheckResult:
    """Run one check, converting any error into a result."""
    check = CHECKS[name]
    title = TITLES[name]
    try:
        return check(ctx)
    except ClientError as exc:
        code = _error_code(exc)
        message = _error_message(exc)
        operation = getattr(exc, "operation_name", None) or "the AWS API call"
        if _is_access_denied(exc):
            return CheckResult(
                "warn", name, title, f"{operation} denied ({code}): {message}", _access_denied_fix(name),
            )
        return CheckResult("fail", name, title, f"{operation} failed ({code}): {message}")
    except BotoCoreError as exc:
        return CheckResult(
            "fail", name, title, f"{type(exc).__name__}: {exc}",
            "check the AWS credentials, profile and Region, and network access to AWS endpoints",
        )
    except Exception as exc:  # never let one check abort the run
        return CheckResult("fail", name, title, f"unexpected {type(exc).__name__}: {exc}")


def run(ctx: Context, names: Iterable[str] | None = None) -> Report:
    """Run ``names`` (default: every check, in :data:`CHECKS` order)."""
    if names is None:
        selected = list(CHECKS)
    else:
        selected = list(names)
        unknown = [name for name in selected if name not in CHECKS]
        if unknown:
            raise ValueError(
                f"unknown check(s): {', '.join(unknown)}; available: {', '.join(CHECKS)}"
            )
    results = []
    for name in selected:
        if ctx.region_disabled and name in REGIONAL_CHECKS:
            results.append(CheckResult(
                "skip", name, TITLES[name],
                f"not checked: Region {ctx.region} is not enabled for this account (see credentials)",
            ))
            continue
        results.append(execute(name, ctx))
    return Report(results=results, account=ctx.account, region=ctx.region, profile=ctx.profile)
