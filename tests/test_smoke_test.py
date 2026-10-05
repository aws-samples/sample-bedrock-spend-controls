"""tools/smoke_test.py: step logic with fakes, no network.

The AWS clients, the signed HTTP function, and the Bedrock client factory are
replaced by in-memory fakes; the clock and sleep are virtual so the ledger
poll runs instantly.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import smoke_test  # noqa: E402
from tools.smoke_test import (  # noqa: E402
    REQUIRED_OUTPUTS,
    STEP_NAMES,
    SmokeTest,
    StepResult,
    default_model,
    exit_code,
    parse_outputs,
    random_password,
)

REGION = "us-east-1"
STACK = "BedrockSpendControls"
SUBJECT = "11111111-2222-3333-4444-555555555555"
ADMIN_KEY = "admin-key-value-never-printed"


def _jwt(sub: str = SUBJECT) -> str:
    def segment(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{segment({'alg': 'none'})}.{segment({'sub': sub})}.sig"


def _outputs(**overrides) -> dict:
    values = {
        "BrokerApiUrl": "https://abc.lambda-url.us-east-1.on.aws/",
        "AdminKeySecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:admin-AbCdEf",
        "DemoUserPoolId": "us-east-1_demoPool",
        "DemoUserPoolClientId": "demo-client-id",
        "AdminUiUrl": "https://d123.cloudfront.net",
    }
    values.update(overrides)
    return {
        "Stacks": [
            {
                "StackName": STACK,
                "Outputs": [
                    {"OutputKey": key, "OutputValue": value}
                    for key, value in values.items()
                    if value is not None
                ],
            }
        ]
    }


class FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeCloudFormation:
    def __init__(self, description: dict):
        self.description = description

    def describe_stacks(self, StackName: str):  # noqa: N803 (boto3 API)
        assert StackName == STACK
        return self.description


class FakeSecrets:
    def __init__(self, value: str | None = ADMIN_KEY):
        self.value = value
        self.requested: list[str] = []

    def get_secret_value(self, SecretId: str):  # noqa: N803
        self.requested.append(SecretId)
        return {"SecretString": self.value} if self.value is not None else {}


class FakeCognito:
    def __init__(self, *, fail_set_password: bool = False, no_token: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_set_password = fail_set_password
        self.no_token = no_token

    def admin_create_user(self, **kwargs):
        self.calls.append(("admin_create_user", kwargs))
        return {"User": {"Username": kwargs["Username"]}}

    def admin_set_user_password(self, **kwargs):
        self.calls.append(("admin_set_user_password", kwargs))
        if self.fail_set_password:
            raise ClientError(
                {"Error": {"Code": "InvalidPasswordException", "Message": "weak"}},
                "AdminSetUserPassword",
            )
        return {}

    def initiate_auth(self, **kwargs):
        self.calls.append(("initiate_auth", kwargs))
        if self.no_token:
            return {"ChallengeName": "NEW_PASSWORD_REQUIRED"}
        return {"AuthenticationResult": {"IdToken": _jwt()}}

    def admin_delete_user(self, **kwargs):
        self.calls.append(("admin_delete_user", kwargs))
        return {}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeProvider:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.credentials = self

    def get_frozen_credentials(self):
        if self.fail:
            raise smoke_test.BrokerCredentialError(
                "quota_blocked: user is blocked", terminal=True, error_type="quota_blocked"
            )
        return SimpleNamespace(access_key="fake-access-key", secret_key="x", token="session")


class FakeBedrockClient:
    def __init__(self, error_code: str | None = None):
        self.error_code = error_code
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error_code:
            raise ClientError(
                {"Error": {"Code": self.error_code, "Message": "no access"}}, "Converse"
            )
        return {"usage": {"inputTokens": 12, "outputTokens": 5}}


class FakeFactory:
    """Stands in for examples.refreshable_bedrock.BedrockSpendControls."""

    instances: list["FakeFactory"] = []

    def __init__(self, gateway_url: str, region: str, session, *, vend_fail=False, converse_error=None):
        self.gateway_url = gateway_url
        self.region = region
        self.session = session
        self.provider = FakeProvider(fail=vend_fail)
        self.client = FakeBedrockClient(error_code=converse_error)
        self.tokens: list[str] = []
        self.closed = False
        FakeFactory.instances.append(self)

    def provider_for(self, jwt: str):
        self.tokens.append(jwt)
        return self.provider

    def client_for(self, jwt: str):
        self.tokens.append(jwt)
        return self.client

    def close(self):
        self.closed = True


class FakeHttp:
    """Scripted responses per path; records every call for assertions."""

    def __init__(self, usage_requests: list[int] | None = None, users: list[dict] | None = None,
                 healthz_status: int = 200, usage_statuses: list[int] | None = None):
        self.usage_requests = list(usage_requests if usage_requests is not None else [1])
        self.usage_statuses = list(usage_statuses or [])
        self.users = users
        self.healthz_status = healthz_status
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url.split(".on.aws", 1)[1]
        if path == "/healthz":
            return FakeResponse(self.healthz_status, {"status": "ok"} if self.healthz_status == 200 else {"error": "down"})
        if path == "/admin/user/usage":
            assert kwargs["admin_key"] == ADMIN_KEY
            if self.usage_statuses:
                status = self.usage_statuses.pop(0)
                if status != 200:
                    return FakeResponse(status, {"error": {"type": "not_found"}})
            requests = self.usage_requests.pop(0) if len(self.usage_requests) > 1 else self.usage_requests[0]
            return FakeResponse(200, {
                "user_id": kwargs["params"]["user_id"], "period": "daily", "requests": requests,
                "input_tokens": 12 * requests, "output_tokens": 5 * requests, "cost_usd": 0.000001 * requests,
            })
        if path == "/admin/users":
            assert kwargs["admin_key"] == ADMIN_KEY
            users = self.users if self.users is not None else [
                {"user_id": SUBJECT, "name": "install-smoke-abc123"}]
            return FakeResponse(200, {"users": users, "next_cursor": None})
        raise AssertionError(f"unexpected request {method} {url}")


class VirtualClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _smoke(*, description=None, secrets=None, cognito=None, http=None, factory_kwargs=None,
           keep_user=False, timeout=420.0, model=None):
    clock = VirtualClock()
    lines: list[str] = []
    factory_kwargs = factory_kwargs or {}
    smoke = SmokeTest(
        cloudformation=FakeCloudFormation(description or _outputs()),
        secretsmanager=secrets or FakeSecrets(),
        cognito=cognito or FakeCognito(),
        session=object(),
        region=REGION,
        stack_name=STACK,
        model=model,
        keep_user=keep_user,
        timeout=timeout,
        request_fn=http or FakeHttp(),
        bedrock_factory=lambda url, region, session: FakeFactory(url, region, session, **factory_kwargs),
        sleep=clock.sleep,
        clock=clock,
        emit=lines.append,
        username_suffix="abc123",
    )
    return smoke, lines, clock


# --- helpers -------------------------------------------------------------------


def test_parse_outputs_maps_keys_and_rejects_missing_stack():
    assert parse_outputs(_outputs())["BrokerApiUrl"].endswith("/")
    with pytest.raises(smoke_test.StepFailed, match="no stack"):
        parse_outputs({"Stacks": []})


@pytest.mark.parametrize(
    ("region", "model"),
    [
        ("us-east-1", "us.amazon.nova-micro-v1:0"),
        ("us-west-2", "us.amazon.nova-micro-v1:0"),
        ("eu-west-1", "eu.amazon.nova-micro-v1:0"),
        ("ap-southeast-2", "apac.amazon.nova-micro-v1:0"),
        ("ca-central-1", "amazon.nova-micro-v1:0"),
        ("us-gov-west-1", "amazon.nova-micro-v1:0"),
    ],
)
def test_default_model_follows_the_cross_region_profile_prefix(region, model):
    assert default_model(region) == model


def test_random_password_meets_the_default_cognito_policy():
    for _ in range(20):
        password = random_password()
        assert len(password) >= 8
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c in "!@#$%^&*" for c in password)
    assert random_password() != random_password()


def test_exit_code_is_zero_only_when_every_step_passed():
    assert exit_code([StepResult("a", "PASS"), StepResult("b", "PASS")]) == 0
    assert exit_code([StepResult("a", "PASS"), StepResult("b", "FAIL", "x")]) == 1
    assert exit_code([StepResult("a", "PASS"), StepResult("b", "SKIP")]) == 1
    assert exit_code([]) == 1


# --- the happy path ---------------------------------------------------------------


def test_happy_path_runs_every_step_once_and_cleans_up():
    smoke, lines, clock = _smoke()
    results = smoke.run()

    assert [result.name for result in results] == list(STEP_NAMES)
    assert all(result.ok for result in results), lines
    assert exit_code(results) == 0
    # Outputs: trailing slash stripped so paths do not double up.
    assert smoke.base_url == "https://abc.lambda-url.us-east-1.on.aws"
    # Admin key read once and never echoed.
    assert smoke.secretsmanager.requested == [_outputs()["Stacks"][0]["Outputs"][1]["OutputValue"]]
    assert ADMIN_KEY not in "\n".join(lines)
    # Exactly one Cognito user lifecycle: create, password, auth, delete.
    assert smoke.cognito.names() == [
        "admin_create_user", "admin_set_user_password", "initiate_auth", "admin_delete_user",
    ]
    create = smoke.cognito.calls[0][1]
    assert create["Username"] == "install-smoke-abc123"
    assert create["MessageAction"] == "SUPPRESS"
    assert "UserAttributes" not in create  # the broker names the subject after cognito:username
    assert smoke.cognito.calls[1][1]["Permanent"] is True
    assert smoke.cognito.calls[2][1]["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert smoke.cognito.calls[3][1] == {"UserPoolId": "us-east-1_demoPool", "Username": "install-smoke-abc123"}
    # One vend: provider_for and client_for share the token (one identity).
    factory = FakeFactory.instances[-1]
    assert factory.gateway_url == smoke.base_url
    assert len(set(factory.tokens)) == 1 and len(factory.tokens) == 2
    assert factory.closed is True
    converse = factory.client.calls
    assert len(converse) == 1
    assert converse[0]["modelId"] == "us.amazon.nova-micro-v1:0"
    assert converse[0]["inferenceConfig"] == {"maxTokens": 5}
    # Ledger read for the token's sub; list-users searched by prefix.
    usage_calls = [call for call in smoke.request_fn.calls if call[1].endswith("/admin/user/usage")]
    assert usage_calls[0][2]["params"] == {"user_id": SUBJECT, "period": "daily"}
    list_calls = [call for call in smoke.request_fn.calls if call[1].endswith("/admin/users")]
    assert list_calls[0][2]["params"] == {"query": "install-smoke"}
    assert clock.sleeps == []
    assert lines[-1] == "Smoke test: 9/9 steps passed"
    assert "ledger rows for subject" in results[-1].detail


# --- outputs ---------------------------------------------------------------------


def test_missing_outputs_fail_first_and_skip_the_rest_without_touching_cognito():
    smoke, lines, _ = _smoke(description=_outputs(DemoUserPoolId=None, DemoUserPoolClientId=None))
    results = smoke.run()
    by_name = {result.name: result for result in results}
    assert by_name["outputs"].status == "FAIL"
    assert "DemoUserPoolId, DemoUserPoolClientId" in by_name["outputs"].detail
    for name in STEP_NAMES[1:-1]:
        assert by_name[name].status == "SKIP", name
    assert by_name["cleanup"].ok and "no Cognito user to delete" in by_name["cleanup"].detail
    assert smoke.cognito.calls == []
    assert smoke.secretsmanager.requested == []
    assert exit_code(results) == 1
    assert REQUIRED_OUTPUTS == ("BrokerApiUrl", "AdminKeySecretArn", "DemoUserPoolId", "DemoUserPoolClientId")


def test_empty_admin_secret_and_unhealthy_broker_are_reported():
    smoke, _, _ = _smoke(secrets=FakeSecrets(value=""))
    results = {result.name: result for result in smoke.run()}
    assert results["admin-key"].status == "FAIL" and "SecretString" in results["admin-key"].detail
    assert results["healthz"].status == "SKIP"

    smoke, _, _ = _smoke(http=FakeHttp(healthz_status=503))
    results = {result.name: result for result in smoke.run()}
    assert results["healthz"].status == "FAIL"
    assert results["healthz"].detail.startswith("HTTP 503")
    assert results["cognito-user"].status == "SKIP"
    assert smoke.cognito.calls == []


# --- ledger polling ---------------------------------------------------------------


def test_ledger_polls_until_requests_reach_one():
    smoke, _, clock = _smoke(http=FakeHttp(usage_requests=[0, 0, 1]))
    results = {result.name: result for result in smoke.run()}
    assert results["ledger"].ok, results["ledger"].detail
    assert "requests=1" in results["ledger"].detail
    assert "after 30 s (3 polls)" in results["ledger"].detail
    assert clock.sleeps == [15, 15]
    usage_calls = [call for call in smoke.request_fn.calls if call[1].endswith("/admin/user/usage")]
    assert len(usage_calls) == 3


def test_ledger_treats_404_as_not_yet_and_other_errors_as_failure():
    smoke, _, _ = _smoke(http=FakeHttp(usage_requests=[1], usage_statuses=[404, 200]))
    results = {result.name: result for result in smoke.run()}
    assert results["ledger"].ok

    smoke, _, _ = _smoke(http=FakeHttp(usage_requests=[1], usage_statuses=[500]))
    results = {result.name: result for result in smoke.run()}
    assert results["ledger"].status == "FAIL"
    assert results["ledger"].detail.startswith("HTTP 500")
    assert results["list-users"].status == "SKIP"
    assert "admin_delete_user" in smoke.cognito.names()


def test_ledger_times_out_with_a_diagnostic_and_still_cleans_up():
    smoke, lines, clock = _smoke(http=FakeHttp(usage_requests=[0]), timeout=40)
    results = {result.name: result for result in smoke.run()}
    assert results["ledger"].status == "FAIL"
    assert "after 40 s" in results["ledger"].detail
    assert "InvocationLogGroup" in results["ledger"].detail
    # 15 + 15 + 10: the last sleep is clipped to the remaining budget.
    assert clock.sleeps == [15, 15, 10]
    assert results["list-users"].status == "SKIP"
    assert results["cleanup"].ok
    assert smoke.cognito.names()[-1] == "admin_delete_user"
    assert exit_code(list(results.values())) == 1


# --- list-users -----------------------------------------------------------------------


def test_list_users_must_contain_the_subject():
    smoke, _, _ = _smoke(http=FakeHttp(users=[{"user_id": "someone-else", "name": "x"}]))
    results = {result.name: result for result in smoke.run()}
    assert results["list-users"].status == "FAIL"
    assert SUBJECT in results["list-users"].detail


# --- cleanup always runs ---------------------------------------------------------------


def test_cleanup_runs_after_vend_and_converse_failures():
    smoke, _, _ = _smoke(factory_kwargs={"vend_fail": True})
    results = {result.name: result for result in smoke.run()}
    assert results["vend"].status == "FAIL"
    assert "broker refused: quota_blocked" in results["vend"].detail
    assert results["converse"].status == "SKIP"
    assert smoke.cognito.names()[-1] == "admin_delete_user"
    assert FakeFactory.instances[-1].closed is True

    smoke, _, _ = _smoke(factory_kwargs={"converse_error": "AccessDeniedException"})
    results = {result.name: result for result in smoke.run()}
    assert results["converse"].status == "FAIL"
    assert "enable model access" in results["converse"].detail
    assert "--model" in results["converse"].detail
    assert results["ledger"].status == "SKIP"
    assert smoke.cognito.names()[-1] == "admin_delete_user"


def test_cleanup_deletes_the_user_even_when_password_or_token_steps_fail():
    smoke, _, _ = _smoke(cognito=FakeCognito(fail_set_password=True))
    results = {result.name: result for result in smoke.run()}
    assert results["cognito-user"].status == "FAIL"
    assert "InvalidPasswordException" in results["cognito-user"].detail
    assert smoke.cognito.names() == ["admin_create_user", "admin_set_user_password", "admin_delete_user"]

    smoke, _, _ = _smoke(cognito=FakeCognito(no_token=True))
    results = {result.name: result for result in smoke.run()}
    assert results["cognito-user"].status == "FAIL"
    assert "NEW_PASSWORD_REQUIRED" in results["cognito-user"].detail
    assert smoke.cognito.names()[-1] == "admin_delete_user"


def test_keep_user_skips_the_deletion_but_says_so():
    smoke, _, _ = _smoke(keep_user=True)
    results = smoke.run()
    assert all(result.ok for result in results)
    assert "admin_delete_user" not in smoke.cognito.names()
    assert "--keep-user" in results[-1].detail and "install-smoke-abc123" in results[-1].detail


def test_cleanup_failure_makes_the_run_fail():
    class DeleteFails(FakeCognito):
        def admin_delete_user(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "gone"}}, "AdminDeleteUser"
            )

    smoke, _, _ = _smoke(cognito=DeleteFails())
    results = smoke.run()
    assert results[-1].name == "cleanup" and results[-1].status == "FAIL"
    assert "UserNotFoundException" in results[-1].detail
    assert exit_code(results) == 1


# --- output format and packaging ------------------------------------------------------


def test_lines_are_pass_fail_skip_with_step_names():
    smoke, lines, _ = _smoke(http=FakeHttp(healthz_status=500))
    smoke.run()
    statuses = [line.split()[0] for line in lines[:-1]]
    assert set(statuses) <= {"PASS", "FAIL", "SKIP"}
    assert lines[0].startswith("PASS  outputs")
    assert lines[2].startswith("FAIL  healthz")
    assert lines[3].startswith("SKIP  cognito-user")
    assert lines[-1].startswith("Smoke test: ")


def test_module_does_not_import_the_cdk_app():
    source = (ROOT / "tools" / "smoke_test.py").read_text(encoding="utf-8")
    assert "from cdk" not in source and "import cdk" not in source
    assert "aws_cdk" not in source
    assert "examples.refreshable_bedrock" in source and "examples.sigv4_gateway" in source


def test_help_runs_as_a_script():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "smoke_test.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for flag in ("--profile", "--region", "--stack", "--model", "--keep-user", "--timeout"):
        assert flag in completed.stdout, flag
