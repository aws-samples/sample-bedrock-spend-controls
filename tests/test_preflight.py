"""tools/preflight: every check has a happy path and a failing path, AWS
errors never escape a run, and the CLI's JSON schema and exit codes are
stable for install.sh and the wizard.

All AWS calls go through botocore Stubber on real clients created with
dummy credentials; HTTP is faked by patching urllib.request.urlopen. No
network, no credentials, no subprocesses except one --help smoke test.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import ANY, Stubber

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.preflight import (  # noqa: E402
    CHECKS,
    REQUIRED_ACTIONS,
    TITLES,
    CheckResult,
    Context,
    Options,
    PreflightError,
    Report,
    main,
    run,
    run_for_config,
)
from tools.preflight import checks, cli  # noqa: E402
from tools.preflight.context import (  # noqa: E402
    build_context,
    parse_overrides,
    resolve_region,
)

ACCOUNT = "123456789012"
REGION = "us-east-1"
DEMO_CONFIG = ROOT / "cdk" / "config" / "demo.json"
MODEL_PRICING = ROOT / "cdk" / "config" / "model-pricing.json"
SESSION = boto3.session.Session(
    aws_access_key_id="testing",
    aws_secret_access_key="testing",
    aws_session_token="testing",
    region_name=REGION,
)
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
PYTHON = sys.executable


# --- helpers ---------------------------------------------------------------


def make_context(raw: dict | None = None, *, config=None, options: Options | None = None,
                 account: str | None = ACCOUNT, region: str = REGION, partition: str = "aws",
                 root: Path = ROOT, expected_account: str | None = None) -> Context:
    return Context(
        session=SESSION,
        region=region,
        account=account,
        partition=partition,
        config=config,
        raw=dict(raw or {}),
        options=options or Options(),
        root=root,
        expected_account=expected_account,
    )


def stub(ctx: Context, service: str, region: str | None = None) -> Stubber:
    """Register a Stubber-wrapped real client in the context's cache."""
    client = SESSION.client(service, region_name=region or ctx.region)
    key = service if region in (None, ctx.region) else f"{service}@{region}"
    ctx.client_cache[key] = client
    stubber = Stubber(client)
    stubber.activate()
    return stubber


def jwt_for(claims: dict) -> str:
    def segment(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256', 'kid': 'k1'})}.{segment(claims)}.c2lnbmF0dXJl"


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, responses: dict, seen: list | None = None):
    """Patch urllib.request.urlopen to serve ``responses`` (url -> JSON body
    or exception)."""

    def _open(request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if seen is not None:
            seen.append(url)
        if url not in responses:
            raise urllib.error.URLError(f"unexpected URL {url}")
        body = responses[url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _open)


ISSUER = "https://idp.example.com"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
JWKS_URL = ISSUER + "/keys"
DISCOVERY = {"issuer": ISSUER, "jwks_uri": JWKS_URL}
JWKS = {"keys": [{"kty": "RSA", "kid": "k1", "n": "AQAB", "e": "AQAB"}]}
OIDC_RAW = {
    "jwt_issuer": ISSUER,
    "jwt_audience": "bedrock-spend-controls",
    "jwt_user_claim": "custom:tenant_id",
    "admin_jwt_claim": "groups",
}

AVAILABLE = {
    "agreementAvailability": {"status": "AVAILABLE"},
    "authorizationStatus": "AUTHORIZED",
    "entitlementAvailability": "AVAILABLE",
    "regionAvailability": "AVAILABLE",
}


def availability(model_id: str, **overrides) -> dict:
    return {"modelId": model_id, **AVAILABLE, **overrides}


def toolkit_stack(version: str = "28", status: str = "CREATE_COMPLETE") -> dict:
    return {
        "Stacks": [
            {
                "StackName": "CDKToolkit",
                "CreationTime": NOW,
                "StackStatus": status,
                "Outputs": [{"OutputKey": "BootstrapVersion", "OutputValue": version}],
            }
        ]
    }


def ssm_version(version: str) -> dict:
    return {"Parameter": {"Name": checks.BOOTSTRAP_VERSION_PARAMETER, "Type": "String", "Value": version}}


# --- report ------------------------------------------------------------------


def test_check_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        CheckResult("maybe", "x", "X")


def test_report_flags_and_text_rendering():
    report = Report(
        results=[
            CheckResult("pass", "toolchain", "Local toolchain", "node: v20.1.0"),
            CheckResult("warn", "credentials", "AWS credentials", "read-only role", "use a deploy role"),
            CheckResult("skip", "scp_bypass", "SCP bypass", "no role given"),
        ],
        account=ACCOUNT,
        region=REGION,
        profile="demo",
    )
    assert report.ok and report.has_warnings
    text = report.to_text()
    assert f"account {ACCOUNT}, region {REGION}, profile demo" in text
    pass_line = next(line for line in text.splitlines() if "PASS" in line)
    warn_line = next(line for line in text.splitlines() if "WARN" in line)
    # Aligned: the title column starts at the same offset on every row.
    assert pass_line.index("Local toolchain") == warn_line.index("AWS credentials")
    assert "fix: use a deploy role" in text
    assert "Summary: 1 passed, 1 warnings, 0 failed, 1 skipped -> OK" in text

    report.results.append(CheckResult("fail", "bootstrap", "CDK bootstrap", "missing", "cdk bootstrap"))
    assert not report.ok
    assert report.summary().endswith("-> FAIL")


def test_report_json_schema_is_stable():
    report = Report([CheckResult("fail", "bootstrap", "CDK bootstrap", "missing", "run cdk bootstrap")],
                    account=ACCOUNT, region=REGION)
    payload = json.loads(report.to_json())
    assert list(payload) == ["account", "region", "ok", "results"]
    assert payload["account"] == ACCOUNT and payload["region"] == REGION and payload["ok"] is False
    assert payload["results"] == [
        {"name": "bootstrap", "status": "fail", "title": "CDK bootstrap",
         "detail": "missing", "fix": "run cdk bootstrap"}
    ]


def test_registry_is_consistent():
    assert list(CHECKS) == [
        "toolchain", "credentials", "bootstrap", "bedrock_model_access", "invocation_logging",
        "lambda_concurrency", "oidc_issuer", "invoker_principals", "scp_bypass", "quotas_cost",
        "region_support", "admin_ui_build",
    ]
    assert set(REQUIRED_ACTIONS) == set(CHECKS) == set(TITLES)
    for actions in REQUIRED_ACTIONS.values():
        assert all(":" in action and action.split(":")[1][:1].isupper() for action in actions)


# --- toolchain ---------------------------------------------------------------


def _patch_toolchain(monkeypatch, versions: dict[str, str | None], python=(3, 12, 4), present=None):
    present = set(versions) if present is None else present
    monkeypatch.setattr(checks, "_which", lambda tool: f"/usr/bin/{tool}" if tool in present else None)

    def run_version(argv):
        if argv[0] == sys.executable:
            return "pip 24.0 from /site-packages/pip (python 3.12)"
        return versions.get(argv[0])

    monkeypatch.setattr(checks, "_run_version", run_version)
    monkeypatch.setattr(checks, "_python_version", lambda: python)


def test_toolchain_passes_with_supported_versions(monkeypatch):
    _patch_toolchain(monkeypatch, {
        "node": "v20.11.1", "npm": "10.2.4", "aws": "aws-cli/2.15.0 Python/3.11.6", "git": "git version 2.43.0",
    })
    result = checks.check_toolchain(make_context())
    assert result.status == "pass"
    assert "node: v20.11.1" in result.detail and "python: 3.12.4 (pip 24.0)" in result.detail


def test_toolchain_fails_on_old_node_and_missing_aws_cli(monkeypatch):
    _patch_toolchain(monkeypatch, {
        "node": "v18.19.0", "npm": "9.0.0", "git": "git version 2.43.0",
    })
    result = checks.check_toolchain(make_context())
    assert result.status == "fail"
    assert "Node.js v18.19.0 is older than the required 20" in result.detail
    assert "AWS CLI not found on PATH" in result.detail
    assert "nvm install 20" in result.fix and "AWS CLI v2" in result.fix


def test_toolchain_fails_on_old_python(monkeypatch):
    _patch_toolchain(monkeypatch, {
        "node": "v22.0.0", "npm": "10.0.0", "aws": "aws-cli/2.15.0", "git": "git version 2.43.0",
    }, python=(3, 11, 9))
    result = checks.check_toolchain(make_context())
    assert result.status == "fail"
    assert "Python 3.11.9 is older than the required 3.12" in result.detail


# --- credentials -------------------------------------------------------------


def test_credentials_pass_and_fill_account(monkeypatch):
    ctx = make_context(account=None, partition="aws")
    sts = stub(ctx, "sts")
    sts.add_response("get_caller_identity", {
        "UserId": "AROA123:deployer", "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Deployer/deployer",
    })
    result = checks.check_credentials(ctx)
    assert result.status == "pass"
    assert ctx.account == ACCOUNT and ctx.partition == "aws"
    assert f"account {ACCOUNT}, region {REGION}" in result.detail
    sts.assert_no_pending_responses()


def test_credentials_warn_on_read_only_role_and_fail_on_account_mismatch():
    ctx = make_context()
    stub(ctx, "sts").add_response("get_caller_identity", {
        "UserId": "AROA", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:role/ReadOnlyAccess",
    })
    result = checks.check_credentials(ctx)
    assert result.status == "warn" and "read-only" in result.detail and result.fix

    ctx = make_context(expected_account="999999999999")
    stub(ctx, "sts").add_response("get_caller_identity", {
        "UserId": "AROA", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:role/Deployer",
    })
    result = checks.check_credentials(ctx)
    assert result.status == "fail" and "--account-id 999999999999" in result.detail


def test_access_denied_becomes_warning_with_grant_fix():
    ctx = make_context()
    stub(ctx, "sts").add_client_error(
        "get_caller_identity", service_error_code="AccessDenied",
        service_message="User is not authorized", http_status_code=403,
    )
    report = run(ctx, ["credentials"])
    (result,) = report.results
    assert result.status == "warn"
    assert result.fix == "grant sts:GetCallerIdentity to run this check"
    assert report.ok


# --- bootstrap ---------------------------------------------------------------


def test_bootstrap_passes_on_current_template():
    ctx = make_context()
    stub(ctx, "cloudformation").add_response(
        "describe_stacks", toolkit_stack("28"), {"StackName": "CDKToolkit"})
    stub(ctx, "ssm").add_response(
        "get_parameter", ssm_version("28"), {"Name": checks.BOOTSTRAP_VERSION_PARAMETER})
    result = checks.check_bootstrap(ctx)
    assert result.status == "pass"
    assert "template version 28" in result.detail and "aws-cdk-lib 2.261.0" in result.detail


def test_bootstrap_fails_when_stack_is_missing():
    ctx = make_context()
    stub(ctx, "cloudformation").add_client_error(
        "describe_stacks", service_error_code="ValidationError",
        service_message="Stack with id CDKToolkit does not exist", http_status_code=400,
    )
    result = checks.check_bootstrap(ctx)
    assert result.status == "fail"
    assert "not bootstrapped" in result.detail
    assert f"npx cdk bootstrap aws://{ACCOUNT}/{REGION}" in result.fix


def test_bootstrap_old_versions_fail_or_warn():
    ctx = make_context()
    stub(ctx, "cloudformation").add_response("describe_stacks", toolkit_stack("10"))
    stub(ctx, "ssm").add_response("get_parameter", ssm_version("10"))
    result = checks.check_bootstrap(ctx)
    assert result.status == "fail" and "older than the minimum 21" in result.detail

    ctx = make_context()
    stub(ctx, "cloudformation").add_response("describe_stacks", toolkit_stack("25"))
    # SSM denied: the check falls back to the stack output.
    stub(ctx, "ssm").add_client_error(
        "get_parameter", service_error_code="AccessDeniedException", http_status_code=400)
    result = checks.check_bootstrap(ctx)
    assert result.status == "warn"
    assert "stack output BootstrapVersion" in result.detail and "cdk bootstrap" in result.fix


# --- bedrock_model_access --------------------------------------------------------


def test_bedrock_model_access_resolves_profiles_across_regions():
    raw = {"allowed_model_arns": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5",
        f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/us.anthropic.claude-sonnet-5",
    ]}
    config = SimpleNamespace(workloads=(SimpleNamespace(name="batch", model="amazon.nova-lite-v1:0"),))
    ctx = make_context(raw, config=config)
    bedrock = stub(ctx, "bedrock")
    bedrock.add_response("get_foundation_model_availability",
                         availability("anthropic.claude-sonnet-5"), {"modelId": "anthropic.claude-sonnet-5"})
    bedrock.add_response("get_inference_profile", {
        "inferenceProfileName": "US Claude", "inferenceProfileId": "us.anthropic.claude-sonnet-5",
        "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/us.anthropic.claude-sonnet-5",
        "status": "ACTIVE", "type": "SYSTEM_DEFINED",
        "models": [
            {"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-5"},
            {"modelArn": "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-sonnet-5"},
        ],
    }, {"inferenceProfileIdentifier": "us.anthropic.claude-sonnet-5"})
    bedrock.add_response("get_foundation_model_availability",
                         availability("amazon.nova-lite-v1:0"), {"modelId": "amazon.nova-lite-v1:0"})
    west = stub(ctx, "bedrock", region="us-west-2")
    west.add_response("get_foundation_model_availability",
                      availability("anthropic.claude-sonnet-5"), {"modelId": "anthropic.claude-sonnet-5"})

    result = checks.check_bedrock_model_access(ctx)
    assert result.status == "pass", result.detail
    assert "anthropic.claude-sonnet-5 (us-west-2)" in result.detail
    assert "amazon.nova-lite-v1:0" in result.detail
    bedrock.assert_no_pending_responses()
    west.assert_no_pending_responses()


def test_bedrock_model_access_fails_when_model_not_enabled():
    ctx = make_context({"allowed_model_arns": ["arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-7"]})
    stub(ctx, "bedrock").add_response("get_foundation_model_availability", availability(
        "anthropic.claude-opus-4-7", authorizationStatus="NOT_AUTHORIZED",
        agreementAvailability={"status": "NOT_AVAILABLE", "errorMessage": "no agreement"},
    ))
    result = checks.check_bedrock_model_access(ctx)
    assert result.status == "fail"
    assert "authorization=NOT_AUTHORIZED" in result.detail
    assert "enable model access in the Bedrock console" in result.fix


def test_bedrock_model_access_warns_on_wildcard_and_denied():
    ctx = make_context({"allowed_model_arns": ["*"]})
    result = checks.check_bedrock_model_access(ctx)
    assert result.status == "warn" and "'*'" in result.detail

    ctx = make_context({"allowed_model_arns": ["arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0"]})
    stub(ctx, "bedrock").add_client_error(
        "get_foundation_model_availability", service_error_code="AccessDeniedException", http_status_code=403)
    result = checks.check_bedrock_model_access(ctx)
    assert result.status == "warn"
    assert "bedrock:GetFoundationModelAvailability" in result.fix


# --- invocation_logging ------------------------------------------------------------


def test_invocation_logging_managed_passes_when_nothing_configured():
    ctx = make_context({"manage_invocation_logging": True})
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", {})
    result = checks.check_invocation_logging(ctx)
    assert result.status == "pass" and checks.MANAGED_INVOCATION_LOG_GROUP in result.detail


def test_invocation_logging_managed_fails_on_existing_config_unless_acknowledged():
    existing = {"loggingConfig": {
        "cloudWatchConfig": {"logGroupName": "/central/bedrock", "roleArn": f"arn:aws:iam::{ACCOUNT}:role/x"},
        "textDataDeliveryEnabled": True,
    }}
    ctx = make_context({"manage_invocation_logging": True})
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", existing)
    result = checks.check_invocation_logging(ctx)
    assert result.status == "fail"
    assert "/central/bedrock" in result.detail and "--acknowledge-logging-overwrite" in result.fix

    ctx = make_context({"manage_invocation_logging": True}, options=Options(acknowledge_logging_overwrite=True))
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", existing)
    result = checks.check_invocation_logging(ctx)
    assert result.status == "warn" and "acknowledged" in result.detail


def test_invocation_logging_existing_group_passes_with_stored_bytes():
    group = "/aws/bedrock/modelinvocations"
    ctx = make_context({"manage_invocation_logging": False, "invocation_log_group_name": group})
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", {"loggingConfig": {
        "cloudWatchConfig": {"logGroupName": group, "roleArn": f"arn:aws:iam::{ACCOUNT}:role/x"}}})
    stub(ctx, "logs").add_response("describe_log_groups", {"logGroups": [
        {"logGroupName": group + "-other", "storedBytes": 0},
        {"logGroupName": group, "storedBytes": 2048},
    ]}, {"logGroupNamePrefix": group})
    result = checks.check_invocation_logging(ctx)
    assert result.status == "pass" and "2,048 bytes stored" in result.detail


def test_invocation_logging_existing_group_missing_or_idle():
    group = "/aws/bedrock/modelinvocations"
    ctx = make_context({"invocation_log_group_name": group})
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", {})
    stub(ctx, "logs").add_response("describe_log_groups", {"logGroups": []})
    result = checks.check_invocation_logging(ctx)
    assert result.status == "fail" and "does not exist" in result.detail and result.fix

    ctx = make_context({"invocation_log_group_name": group})
    stub(ctx, "bedrock").add_response("get_model_invocation_logging_configuration", {"loggingConfig": {
        "cloudWatchConfig": {"logGroupName": group, "roleArn": f"arn:aws:iam::{ACCOUNT}:role/x"}}})
    logs = stub(ctx, "logs")
    logs.add_response("describe_log_groups", {"logGroups": [{"logGroupName": group, "storedBytes": 0}]})
    logs.add_response("filter_log_events", {"events": []},
                      {"logGroupName": group, "startTime": ANY, "limit": 1})
    result = checks.check_invocation_logging(ctx)
    assert result.status == "warn" and "no invocation logs seen in 24 h" in result.detail


# --- lambda_concurrency --------------------------------------------------------------


def test_lambda_concurrency_pass_and_fail():
    ctx = make_context({"reserve_enforcement_concurrency": True})
    stub(ctx, "lambda").add_response("get_account_settings", {
        "AccountLimit": {"ConcurrentExecutions": 1000, "UnreservedConcurrentExecutions": 985}})
    result = checks.check_lambda_concurrency(ctx)
    assert result.status == "pass" and "985 of 1000" in result.detail

    ctx = make_context({"reserve_enforcement_concurrency": True})
    stub(ctx, "lambda").add_response("get_account_settings", {
        "AccountLimit": {"ConcurrentExecutions": 10, "UnreservedConcurrentExecutions": 10}})
    result = checks.check_lambda_concurrency(ctx)
    assert result.status == "fail"
    assert "set reserve_enforcement_concurrency=false or request a quota increase" in result.fix

    ctx = make_context({"reserve_enforcement_concurrency": False})
    stub(ctx, "lambda").add_response("get_account_settings", {
        "AccountLimit": {"ConcurrentExecutions": 10, "UnreservedConcurrentExecutions": 10}})
    assert checks.check_lambda_concurrency(ctx).status == "pass"


# --- oidc_issuer -----------------------------------------------------------------


def test_oidc_issuer_skips_for_cognito_managed():
    result = checks.check_oidc_issuer(make_context({"jwt_issuer": ""}))
    assert result.status == "skip" and "Cognito" in result.detail


def test_oidc_issuer_passes_with_discovery_jwks_and_sample_token(monkeypatch):
    seen: list[str] = []
    fake_urlopen(monkeypatch, {DISCOVERY_URL: DISCOVERY, JWKS_URL: JWKS}, seen)
    token = jwt_for({"iss": ISSUER, "aud": ["bedrock-spend-controls", "other"],
                     "custom:tenant_id": "acme", "groups": ["quota-admins"], "exp": 1})
    ctx = make_context(OIDC_RAW, options=Options(sample_jwt=token))
    result = checks.check_oidc_issuer(ctx)
    assert result.status == "pass", result.detail
    assert seen == [DISCOVERY_URL, JWKS_URL]
    assert "1 key" in result.detail and "sample token carries the configured claims" in result.detail


def test_oidc_issuer_fails_on_issuer_mismatch_and_empty_jwks(monkeypatch):
    fake_urlopen(monkeypatch, {DISCOVERY_URL: {"issuer": ISSUER + "/", "jwks_uri": JWKS_URL},
                               JWKS_URL: {"keys": []}})
    result = checks.check_oidc_issuer(make_context(OIDC_RAW))
    assert result.status == "fail"
    assert "compares them exactly" in result.detail and "contains no keys" in result.detail
    assert "jwt_issuer" in result.fix


def test_oidc_issuer_fails_on_token_claims(monkeypatch):
    fake_urlopen(monkeypatch, {DISCOVERY_URL: DISCOVERY, JWKS_URL: JWKS})
    token = jwt_for({"iss": ISSUER, "aud": "someone-else", "sub": "u1"})
    result = checks.check_oidc_issuer(make_context(OIDC_RAW, options=Options(sample_jwt=token)))
    assert result.status == "fail"
    assert "no 'custom:tenant_id' claim" in result.detail
    assert "does not include jwt_audience" in result.detail


def test_oidc_issuer_warns_when_admin_claim_missing_only(monkeypatch):
    fake_urlopen(monkeypatch, {DISCOVERY_URL: DISCOVERY, JWKS_URL: JWKS})
    token = jwt_for({"iss": ISSUER, "aud": "bedrock-spend-controls", "custom:tenant_id": "acme"})
    result = checks.check_oidc_issuer(make_context(OIDC_RAW, options=Options(sample_jwt=token)))
    assert result.status == "warn" and "'groups'" in result.detail


def test_oidc_issuer_fails_when_discovery_unreachable_or_not_https(monkeypatch):
    fake_urlopen(monkeypatch, {DISCOVERY_URL: urllib.error.URLError("connection refused")})
    result = checks.check_oidc_issuer(make_context(OIDC_RAW))
    assert result.status == "fail" and "connection refused" in result.detail

    calls: list[str] = []
    fake_urlopen(monkeypatch, {}, calls)
    result = checks.check_oidc_issuer(make_context({**OIDC_RAW, "jwt_issuer": "http://idp.example.com"}))
    assert result.status == "fail" and "https://" in result.detail
    assert calls == []  # never fetched over plain http


# --- invoker_principals -----------------------------------------------------------


def test_invoker_principals_skip_pass_fail_and_cross_account():
    assert checks.check_invoker_principals(make_context({"invoker_principal_arns": []})).status == "skip"

    role = f"arn:aws:iam::{ACCOUNT}:role/app/BedrockSpendControlsInvoker"
    user = f"arn:aws:iam::{ACCOUNT}:user/alice"
    ctx = make_context({"invoker_principal_arns": [role, user, f"arn:aws:iam::{ACCOUNT}:root"]})
    iam = stub(ctx, "iam")
    iam.add_response("get_role", {"Role": {
        "Path": "/app/", "RoleName": "BedrockSpendControlsInvoker", "RoleId": "EXAMPLEROLEID0000000",
        "Arn": role, "CreateDate": NOW}}, {"RoleName": "BedrockSpendControlsInvoker"})
    iam.add_response("get_user", {"User": {
        "Path": "/", "UserName": "alice", "UserId": "EXAMPLEUSERID0000000", "Arn": user, "CreateDate": NOW}},
        {"UserName": "alice"})
    result = checks.check_invoker_principals(ctx)
    assert result.status == "pass" and "account root" in result.detail
    iam.assert_no_pending_responses()

    ctx = make_context({"invoker_principal_arns": [role]})
    stub(ctx, "iam").add_client_error("get_role", service_error_code="NoSuchEntity", http_status_code=404)
    result = checks.check_invoker_principals(ctx)
    assert result.status == "fail" and role in result.detail
    assert "remove it from invoker_principal_arns" in result.fix

    other = "arn:aws:iam::999999999999:role/Partner"
    result = checks.check_invoker_principals(make_context({"invoker_principal_arns": [other]}))
    assert result.status == "warn" and "cross-account" in result.detail


# --- scp_bypass ---------------------------------------------------------------------


def test_scp_bypass_skip_pass_and_fail():
    result = checks.check_scp_bypass(make_context())
    assert result.status == "skip" and "--app-role-arn" in result.fix

    role = f"arn:aws:iam::{ACCOUNT}:role/OrderService"
    for decision, status in (("implicitDeny", "pass"), ("explicitDeny", "pass"), ("allowed", "fail")):
        ctx = make_context(options=Options(app_role_arn=role))
        stub(ctx, "iam").add_response("simulate_principal_policy", {"EvaluationResults": [
            {"EvalActionName": "bedrock:InvokeModel", "EvalDecision": decision}]}, {
            "PolicySourceArn": role, "ActionNames": ["bedrock:InvokeModel"],
            "ResourceArns": [f"arn:aws:bedrock:{REGION}::foundation-model/*"]})
        result = checks.check_scp_bypass(ctx)
        assert result.status == status, decision
        assert decision in result.detail
    assert "deny-direct-bedrock-invocation" in result.fix


# --- quotas_cost --------------------------------------------------------------------


def test_quotas_cost_reports_estimate_for_matching_scenario():
    demo = checks.check_quotas_cost(make_context({"admin_ui": True}))
    assert demo.status == "pass" and "USD/month for scenario 'Demo" in demo.detail

    production = checks.check_quotas_cost(make_context({"jwt_issuer": ISSUER, "admin_ui": False}))
    assert production.status == "pass" and "scenario 'Production" in production.detail


def test_quotas_cost_warns_when_estimator_unavailable(tmp_path):
    result = checks.check_quotas_cost(make_context(root=tmp_path))
    assert result.status == "warn" and "cost estimate unavailable" in result.detail
    assert "estimate_cost.py" in result.fix


# --- region_support -----------------------------------------------------------------


def _layer_arn(region: str, partition: str = "aws") -> str:
    return (f"arn:{partition}:lambda:{region}:753240598075:layer:"
            f"LambdaAdapterLayerX86:{checks.ADAPTER_LAYER_VERSION}")


def test_region_support_pass_and_fail():
    ctx = make_context({"admin_ui": True})
    stubber = stub(ctx, "lambda")
    stubber.add_response("get_layer_version_by_arn", {"LayerVersionArn": _layer_arn(REGION)},
                         {"Arn": _layer_arn(REGION)})
    result = checks.check_region_support(ctx)
    assert result.status == "pass" and "Cognito and CloudFront" in result.detail
    assert "static Region list" not in result.detail
    stubber.assert_no_pending_responses()

    ctx = make_context({"admin_ui": True}, region="us-gov-west-1", partition="aws-us-gov")
    stub(ctx, "lambda").add_client_error("get_layer_version_by_arn", "ResourceNotFoundException")
    result = checks.check_region_support(ctx)
    assert result.status == "fail"
    assert "Lambda Web Adapter layer" in result.detail and "CloudFront" in result.detail
    assert "adapter_layer_arn" in result.fix and "us-east-1" in result.fix

    ctx = make_context({"admin_ui": False, "adapter_layer_arn": "arn:aws-cn:lambda:cn-north-1:1:layer:x:1"},
                       region="cn-north-1", partition="aws-cn")
    result = checks.check_region_support(ctx)
    assert result.status == "fail" and "Cognito" in result.detail and "Bedrock" in result.detail


def test_region_support_live_probe_overrides_static_list():
    # Lambda finds the layer in a Region missing from the static list (an
    # opt-in Region the list predates): the live answer wins.
    ctx = make_context({"admin_ui": False, "jwt_issuer": "https://idp.example.com"},
                       region="ap-southeast-7")
    stub(ctx, "lambda").add_response(
        "get_layer_version_by_arn", {"LayerVersionArn": _layer_arn("ap-southeast-7")},
        {"Arn": _layer_arn("ap-southeast-7")})
    result = checks.check_region_support(ctx)
    assert "Lambda Web Adapter layer (account" not in result.detail

    # Lambda cannot answer (AccessDenied): fall back to the static list, and
    # say so.
    ctx = make_context({"admin_ui": True})
    stub(ctx, "lambda").add_client_error("get_layer_version_by_arn", "AccessDeniedException")
    result = checks.check_region_support(ctx)
    assert result.status == "pass" and "static Region list" in result.detail


# --- admin_ui_build -----------------------------------------------------------------


def test_admin_ui_build_skip_pass_warn_fail(tmp_path):
    assert checks.check_admin_ui_build(make_context({"admin_ui": False})).status == "skip"

    result = checks.check_admin_ui_build(make_context({"admin_ui": True}, root=tmp_path))
    assert result.status == "warn" and result.fix == "run npm ci && npm run build in admin-ui/"

    required = make_context({"admin_ui": True}, root=tmp_path, options=Options(admin_ui_build_required=True))
    assert checks.check_admin_ui_build(required).status == "fail"

    index = tmp_path / "admin-ui" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text("<html></html>")
    result = checks.check_admin_ui_build(make_context({"admin_ui": True}, root=tmp_path))
    assert result.status == "pass" and "admin-ui/dist/index.html" in result.detail


# --- runner -----------------------------------------------------------------------


class _BrokenClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    def __getattr__(self, name):
        def call(**kwargs):
            raise self._exc

        return call


def test_run_never_raises_and_keeps_order():
    ctx = make_context({"reserve_enforcement_concurrency": True, "admin_ui": False})
    stub(ctx, "lambda").add_client_error(
        "get_account_settings", service_error_code="ServiceException", service_message="boom")
    ctx.client_cache["sts"] = _BrokenClient(EndpointConnectionError(endpoint_url="https://sts.example"))
    ctx.client_cache["bedrock"] = _BrokenClient(RuntimeError("kaboom"))

    report = run(ctx, ["admin_ui_build", "lambda_concurrency", "credentials", "invocation_logging"])
    assert [result.name for result in report.results] == [
        "admin_ui_build", "lambda_concurrency", "credentials", "invocation_logging"]
    by_name = {result.name: result for result in report.results}
    assert by_name["admin_ui_build"].status == "skip"
    assert by_name["lambda_concurrency"].status == "fail" and "ServiceException" in by_name["lambda_concurrency"].detail
    assert by_name["credentials"].status == "fail" and "EndpointConnectionError" in by_name["credentials"].detail
    assert by_name["invocation_logging"].status == "fail" and "kaboom" in by_name["invocation_logging"].detail
    assert not report.ok

    with pytest.raises(ValueError, match="unknown check"):
        run(ctx, ["nope"])


def test_clients_are_memoised_per_service_and_region():
    ctx = make_context()
    assert ctx.clients("iam") is ctx.clients("iam")
    assert ctx.clients("bedrock", region="us-west-2") is ctx.clients("bedrock", region="us-west-2")
    assert ctx.clients("bedrock", region="us-west-2") is not ctx.clients("bedrock")
    assert ctx.clients("bedrock", region=REGION) is ctx.clients("bedrock")


# --- context: --set parsing and Region resolution ---------------------------------------


def test_parse_overrides_types():
    overrides = parse_overrides([
        "admin_ui=false",
        "usage_retention_days=40",
        "warn_threshold=0.5",
        "alert_email=ops@example.com",
        "allowed_model_arns=arn:aws:bedrock:*::foundation-model/a, arn:aws:bedrock:*::foundation-model/b",
        "manage_invocation_logging=true",
        'default_limits={"daily": {"usd": 2, "input_tokens": 1, "output_tokens": 1}}',
        "invocation_log_group_name=null",
    ])
    assert overrides["admin_ui"] is False
    assert overrides["usage_retention_days"] == 40
    assert overrides["warn_threshold"] == 0.5
    assert overrides["alert_email"] == "ops@example.com"
    assert overrides["allowed_model_arns"] == [
        "arn:aws:bedrock:*::foundation-model/a", "arn:aws:bedrock:*::foundation-model/b"]
    assert overrides["manage_invocation_logging"] is True
    assert overrides["default_limits"]["daily"]["usd"] == 2
    assert overrides["invocation_log_group_name"] is None

    with pytest.raises(PreflightError, match="unknown deployment key"):
        parse_overrides(["bogus=1"])
    with pytest.raises(PreflightError, match="key=value"):
        parse_overrides(["admin_ui"])
    with pytest.raises(PreflightError, match="expected an integer"):
        parse_overrides(["usage_retention_days=many"])


def test_resolve_region_precedence():
    no_region = boto3.session.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name=None)
    assert resolve_region("eu-west-1", SESSION, {"AWS_REGION": "us-west-2"}) == "eu-west-1"
    assert resolve_region(None, SESSION, {"AWS_REGION": "us-west-2"}) == "us-west-2"
    assert resolve_region(None, SESSION, {"AWS_DEFAULT_REGION": "eu-central-1"}) == "eu-central-1"
    assert resolve_region(None, SESSION, {}) == REGION
    if no_region.region_name is None:
        with pytest.raises(PreflightError, match="no AWS Region"):
            resolve_region(None, no_region, {})


def test_build_context_validates_with_the_cdk_configuration(tmp_path):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({
        "manage_invocation_logging": True, "warn_threshold": 5, "model_config": str(MODEL_PRICING)}))
    with pytest.raises(PreflightError) as excinfo:
        build_context(config, region=REGION, account_id=ACCOUNT)
    assert "warn_threshold" in str(excinfo.value)

    ctx = build_context(DEMO_CONFIG, region=REGION, account_id=ACCOUNT,
                        overrides={"usage_retention_days": 60})
    assert ctx.account == ACCOUNT and ctx.partition == "aws" and ctx.region == REGION
    assert ctx.config is not None and ctx.config.usage_retention_days == 60
    assert ctx.setting("usage_retention_days") == 60
    assert ctx.workloads() == ()


# --- CLI --------------------------------------------------------------------------


def test_cli_json_output_and_exit_codes(capsys):
    argv = ["--config", str(DEMO_CONFIG), "--region", REGION,
            "--account-id", ACCOUNT, "--checks", "region_support,quotas_cost,admin_ui_build,scp_bypass"]
    assert main(argv + ["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["account", "region", "ok", "results"]
    assert payload["account"] == ACCOUNT and payload["region"] == REGION and payload["ok"] is True
    assert [result["name"] for result in payload["results"]] == [
        "region_support", "quotas_cost", "admin_ui_build", "scp_bypass"]
    assert set(payload["results"][0]) == {"name", "status", "title", "detail", "fix"}

    # A failing check (GovCloud has no public adapter layer or CloudFront) -> 1.
    assert main(["--config", str(DEMO_CONFIG), "--region", "us-gov-west-1",
                 "--account-id", ACCOUNT, "--checks", "region_support", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["results"][0]["status"] == "fail"

    # Text mode renders the table and the summary.
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "PASS  region_support" in out and "Summary:" in out


def test_cli_usage_and_config_errors_exit_2(capsys, tmp_path):
    assert main(["--checks", "nope", "--region", REGION, "--account-id", ACCOUNT]) == 2
    assert "unknown check(s): nope" in capsys.readouterr().err

    assert main(["--set", "bogus=1", "--region", REGION, "--account-id", ACCOUNT]) == 2
    assert "unknown deployment key" in capsys.readouterr().err

    config = tmp_path / "bad.json"
    config.write_text(json.dumps({
        "manage_invocation_logging": True, "vended_ttl_seconds": 10, "model_config": str(MODEL_PRICING)}))
    assert main(["--config", str(config), "--region", REGION, "--account-id", ACCOUNT]) == 2
    err = capsys.readouterr().err
    assert "configuration rejected" in err and "vended_ttl_seconds" in err

    assert main(["--config", str(tmp_path / "missing.json"), "--region", REGION, "--account-id", ACCOUNT]) == 2
    assert "config file not found" in capsys.readouterr().err

    missing_token = tmp_path / "no-token.jwt"
    assert main(["--sample-jwt", f"@{missing_token}", "--region", REGION, "--account-id", ACCOUNT]) == 2
    assert "--sample-jwt" in capsys.readouterr().err


def test_cli_set_overlay_reaches_the_checks(capsys):
    argv = ["--config", str(DEMO_CONFIG), "--region", REGION,
            "--account-id", ACCOUNT, "--checks", "admin_ui_build", "--json",
            "--set", "admin_ui=false", "--set", "admin_jwt_claim=", "--set", "admin_jwt_value="]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["status"] == "skip"


def test_cli_sample_jwt_from_file(tmp_path):
    token_file = tmp_path / "token.jwt"
    token_file.write_text(jwt_for({"sub": "x"}) + "\n")
    assert cli.read_sample_jwt(f"@{token_file}") == jwt_for({"sub": "x"})
    assert cli.read_sample_jwt("abc.def.ghi") == "abc.def.ghi"
    assert cli.read_sample_jwt(None) is None


def test_run_for_config_returns_report():
    report = run_for_config(DEMO_CONFIG, region=REGION,
                            account_id=ACCOUNT, checks=["admin_ui_build", "scp_bypass"])
    assert isinstance(report, Report)
    assert [result.name for result in report.results] == ["admin_ui_build", "scp_bypass"]
    assert report.account == ACCOUNT and report.region == REGION


def test_module_runs_as_directory_and_as_module():
    for target in (["tools/preflight"], ["-m", "tools.preflight"]):
        completed = subprocess.run(
            [PYTHON, *target, "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "--acknowledge-logging-overwrite" in completed.stdout


# --- install verdict ----------------------------------------------------------------


def test_install_verdict_ignores_what_the_installer_resolves(tmp_path, capsys):
    from tools.preflight.verdict import install_verdict, main as verdict_main

    def report(**statuses):
        return {"ok": all(s != "fail" for s in statuses.values()),
                "results": [{"name": n, "status": s} for n, s in statuses.items()]}

    # A fresh account: no bootstrap, no console build -> the installer proceeds.
    fresh = report(toolchain="pass", bootstrap="fail", admin_ui_build="warn", bedrock_model_access="warn")
    assert install_verdict(fresh) == ("ok", "warn", "fail")
    # Only installer-resolvable findings -> no warning prompt either.
    assert install_verdict(report(bootstrap="fail", admin_ui_build="warn")) == ("ok", "clean", "fail")
    # Anything else failing still blocks.
    assert install_verdict(report(bootstrap="pass", invocation_logging="fail")) == ("fail", "clean", "pass")
    assert install_verdict(report(toolchain="pass")) == ("ok", "clean", "missing")

    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(fresh), encoding="utf-8")
    assert verdict_main([str(path)]) == 0
    assert capsys.readouterr().out.strip() == "ok warn fail"
    assert verdict_main([]) == 2


# --- opt-in Region not enabled -----------------------------------------------------------


def test_disabled_region_is_named_and_regional_checks_are_skipped():
    ctx = make_context(account=None, region="eu-south-2")
    stub(ctx, "sts").add_client_error(
        "get_caller_identity", "InvalidClientTokenId",
        "The security token included in the request is invalid")
    stub(ctx, "sts", region="us-east-1").add_response("get_caller_identity", {
        "UserId": "deployer", "Account": ACCOUNT,
        "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/Deployer/deployer",
    })
    result = checks.check_credentials(ctx)
    assert result.status == "fail"
    assert "has not enabled Region eu-south-2" in result.detail
    assert "enable-region --region-name eu-south-2" in result.fix
    assert ctx.region_disabled is True and ctx.account == ACCOUNT

    report = checks.run(ctx, ["bootstrap", "quotas_cost", "region_support"])
    statuses = {r.name: r.status for r in report.results}
    assert statuses["bootstrap"] == "skip" and statuses["region_support"] == "skip"
    assert "not enabled" in report.results[0].detail
    assert statuses["quotas_cost"] == "pass"  # local checks still run


def test_truly_invalid_credentials_still_fail_as_credentials():
    ctx = make_context(account=None, region="eu-south-2")
    stub(ctx, "sts").add_client_error("get_caller_identity", "InvalidClientTokenId", "bad token")
    stub(ctx, "sts", region="us-east-1").add_client_error(
        "get_caller_identity", "InvalidClientTokenId", "bad token")
    result = checks.execute("credentials", ctx)
    assert result.status == "fail" and "InvalidClientTokenId" in result.detail
    assert ctx.region_disabled is False
