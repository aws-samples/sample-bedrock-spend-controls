"""End-to-end smoke test for a deployed Bedrock Spend Controls stack.

The non-interactive version of DEPLOYMENT.md steps 6 and 7: read the stack
outputs, call the admin API, create a throwaway Cognito quota user, vend
credentials for it through the broker, make one small Bedrock call, wait for
the metered request to reach the ledger, list the user, and clean up.

    python tools/smoke_test.py --region us-east-1 --stack BedrockSpendControls \
        [--profile P] [--model us.amazon.nova-micro-v1:0] [--keep-user] [--timeout 420]

Every step prints ``PASS`` or ``FAIL`` (``SKIP`` when an earlier step made it
impossible); the exit status is 0 only when every step passed. The admin key
is read from Secrets Manager and never printed. The Cognito user is always
deleted unless ``--keep-user``; its ledger rows stay (there is no admin route
to delete usage) and expire with ``usage_retention_days``.

Dependencies: ``examples/requirements.txt`` (boto3, botocore, httpx) plus the
repository's ``examples/`` client library. Nothing under ``cdk/`` is imported.
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from botocore.exceptions import BotoCoreError, ClientError  # noqa: E402

from examples.refreshable_bedrock import (  # noqa: E402
    BedrockSpendControls,
    BrokerCredentialError,
    jwt_claim,
)
from examples.sigv4_gateway import signed_request  # noqa: E402

USERNAME_PREFIX = "install-smoke-"
DEFAULT_TIMEOUT_SECONDS = 420
POLL_INTERVAL_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 20
REQUIRED_OUTPUTS = (
    "BrokerApiUrl",
    "AdminKeySecretArn",
    "DemoUserPoolId",
    "DemoUserPoolClientId",
)
STEP_NAMES = (
    "outputs",
    "admin-key",
    "healthz",
    "cognito-user",
    "vend",
    "converse",
    "ledger",
    "list-users",
    "cleanup",
)


class StepFailed(Exception):
    """A step did not pass; the message is the FAIL detail."""


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str  # PASS, FAIL or SKIP
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def default_model(region: str) -> str:
    """Cheapest widely available model, through the Region's cross-Region
    inference profile where one exists (us., eu., apac.)."""
    if region.startswith("eu-"):
        return "eu.amazon.nova-micro-v1:0"
    if region.startswith("ap-"):
        return "apac.amazon.nova-micro-v1:0"
    if region.startswith("us-") and not region.startswith("us-gov-"):
        return "us.amazon.nova-micro-v1:0"
    return "amazon.nova-micro-v1:0"


def random_password() -> str:
    """Permanent password for the throwaway user: satisfies Cognito's default
    policy (8+ characters with upper, lower, digit and symbol)."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(20))
    return (
        body
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%^&*")
    )


def exit_code(results: list[StepResult]) -> int:
    """0 only when every step passed (a skipped step is not a pass)."""
    return 0 if results and all(result.ok for result in results) else 1


def parse_outputs(description: dict[str, Any]) -> dict[str, str]:
    """``{OutputKey: OutputValue}`` from a ``describe_stacks`` response."""
    stacks = description.get("Stacks") or []
    if not stacks:
        raise StepFailed("describe_stacks returned no stack")
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in stacks[0].get("Outputs", [])
    }


class SmokeTest:
    """Run the steps against injected AWS clients and HTTP function.

    ``request_fn`` has the signature of ``examples.sigv4_gateway.signed_request``;
    ``bedrock_factory(gateway_url, region, session)`` returns an object with
    ``provider_for(jwt)`` and ``client_for(jwt)`` (``BedrockSpendControls`` by
    default). ``sleep`` and ``clock`` are injectable so the ledger poll can be
    tested without waiting.
    """

    def __init__(
        self,
        *,
        cloudformation: Any,
        secretsmanager: Any,
        cognito: Any,
        session: Any,
        region: str,
        stack_name: str,
        model: str | None = None,
        keep_user: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request_fn: Callable[..., Any] = signed_request,
        bedrock_factory: Callable[[str, str, Any], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] = print,
        username_suffix: str | None = None,
    ) -> None:
        self.cloudformation = cloudformation
        self.secretsmanager = secretsmanager
        self.cognito = cognito
        self.session = session
        self.region = region
        self.stack_name = stack_name
        self.model = model or default_model(region)
        self.keep_user = keep_user
        self.timeout = timeout
        self.request_fn = request_fn
        self.bedrock_factory = bedrock_factory or self._default_factory
        self.sleep = sleep
        self.clock = clock
        self.emit = emit
        self.username = USERNAME_PREFIX + (username_suffix or secrets.token_hex(3))

        self.outputs: dict[str, str] = {}
        self.base_url = ""
        self.admin_key = ""
        self.user_created = False
        self.id_token = ""
        self.subject = ""
        self.factory: Any = None
        self.results: list[StepResult] = []

    @staticmethod
    def _default_factory(gateway_url: str, region: str, session: Any) -> Any:
        return BedrockSpendControls(gateway_url, region=region, aws_session=session)

    # -- driver ---------------------------------------------------------------

    def run(self) -> list[StepResult]:
        """Run every step, cleaning up whatever an earlier failure left."""
        steps = [
            ("outputs", self.step_outputs),
            ("admin-key", self.step_admin_key),
            ("healthz", self.step_healthz),
            ("cognito-user", self.step_cognito_user),
            ("vend", self.step_vend),
            ("converse", self.step_converse),
            ("ledger", self.step_ledger),
            ("list-users", self.step_list_users),
        ]
        failed = False
        try:
            for name, step in steps:
                if failed:
                    self._record(name, "SKIP", "not attempted after an earlier failure")
                    continue
                failed = not self._attempt(name, step)
        finally:
            self._attempt("cleanup", self.step_cleanup)
        passed = sum(1 for result in self.results if result.ok)
        self.emit(f"Smoke test: {passed}/{len(self.results)} steps passed")
        return self.results

    def _attempt(self, name: str, step: Callable[[], str]) -> bool:
        try:
            detail = step()
        except StepFailed as exc:
            self._record(name, "FAIL", str(exc))
            return False
        except BrokerCredentialError as exc:
            self._record(name, "FAIL", f"broker refused: {exc.message}")
            return False
        except ClientError as exc:
            error = exc.response.get("Error", {})
            self._record(
                name,
                "FAIL",
                f"{exc.operation_name} failed ({error.get('Code', 'ClientError')}): "
                f"{error.get('Message', '')}".strip(),
            )
            return False
        except (BotoCoreError, OSError, ValueError) as exc:
            self._record(name, "FAIL", f"{type(exc).__name__}: {exc}")
            return False
        self._record(name, "PASS", detail)
        return True

    def _record(self, name: str, status: str, detail: str) -> None:
        result = StepResult(name, status, detail)
        self.results.append(result)
        self.emit(f"{status:<4}  {name:<13} {detail}".rstrip())

    # -- HTTP helpers -------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request_fn(
            method,
            self.base_url + path,
            region=self.region,
            aws_session=self.session,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )

    def _admin_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._request("GET", path, admin_key=self.admin_key, params=params or {})

    @staticmethod
    def _body(response: Any) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _http_error(response: Any) -> str:
        text = getattr(response, "text", "") or ""
        return f"HTTP {response.status_code}: {text[:200]}"

    # -- steps ------------------------------------------------------------------

    def step_outputs(self) -> str:
        description = self.cloudformation.describe_stacks(StackName=self.stack_name)
        self.outputs = parse_outputs(description)
        missing = [key for key in REQUIRED_OUTPUTS if not self.outputs.get(key)]
        if missing:
            raise StepFailed(
                f"stack {self.stack_name} lacks outputs {', '.join(missing)}; the smoke "
                "test needs the stack-managed demo Cognito user pool (empty jwt_issuer)"
            )
        self.base_url = self.outputs["BrokerApiUrl"].rstrip("/")
        return f"{len(self.outputs)} outputs read from stack {self.stack_name} ({self.region})"

    def step_admin_key(self) -> str:
        secret = self.secretsmanager.get_secret_value(
            SecretId=self.outputs["AdminKeySecretArn"]
        )
        value = secret.get("SecretString") or ""
        if not value:
            raise StepFailed("AdminKeySecretArn has no SecretString")
        self.admin_key = value
        return "admin key read from Secrets Manager (not shown)"

    def step_healthz(self) -> str:
        response = self._request("GET", "/healthz")
        if response.status_code != 200:
            raise StepFailed(self._http_error(response))
        status = self._body(response).get("status")
        if status != "ok":
            raise StepFailed(f"GET /healthz returned status {status!r}")
        return "GET /healthz -> 200 ok"

    def step_cognito_user(self) -> str:
        pool_id = self.outputs["DemoUserPoolId"]
        password = random_password()
        # No email attribute: the broker then names the auto-provisioned
        # subject after cognito:username, which list-users searches for.
        self.cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=self.username,
            MessageAction="SUPPRESS",
        )
        self.user_created = True
        self.cognito.admin_set_user_password(
            UserPoolId=pool_id,
            Username=self.username,
            Password=password,
            Permanent=True,
        )
        auth = self.cognito.initiate_auth(
            ClientId=self.outputs["DemoUserPoolClientId"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": self.username, "PASSWORD": password},
        )
        token = (auth.get("AuthenticationResult") or {}).get("IdToken") or ""
        if not token:
            raise StepFailed(
                "USER_PASSWORD_AUTH returned no ID token "
                f"(challenge {auth.get('ChallengeName', 'none')})"
            )
        self.id_token = token
        self.subject = jwt_claim(token, "sub")
        return f"{self.username} created; ID token obtained (sub {self.subject})"

    def step_vend(self) -> str:
        self.factory = self.bedrock_factory(self.base_url, self.region, self.session)
        provider = self.factory.provider_for(self.id_token)
        # One vend: converse below reuses these cached credentials, so the
        # broker's per-identity vend rate limit is barely touched.
        credentials = provider.credentials.get_frozen_credentials()
        if not credentials.access_key or not credentials.token:
            raise StepFailed("broker returned incomplete credentials")
        return f"POST /v1/credentials vended credentials for subject {self.subject}"

    def step_converse(self) -> str:
        client = self.factory.client_for(self.id_token)
        try:
            response = client.converse(
                modelId=self.model,
                messages=[
                    {"role": "user", "content": [{"text": "Reply with exactly: smoke"}]}
                ],
                inferenceConfig={"maxTokens": 5},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"AccessDeniedException", "ResourceNotFoundException", "ValidationException"}:
                raise StepFailed(
                    f"converse on {self.model} failed ({code}): enable model access in "
                    f"{self.region} or pass --model with a model this account can use"
                ) from exc
            raise
        usage = response.get("usage", {})
        return (
            f"{self.model}: {usage.get('inputTokens', '?')} input / "
            f"{usage.get('outputTokens', '?')} output tokens"
        )

    def step_ledger(self) -> str:
        started = self.clock()
        deadline = started + self.timeout
        polls = 0
        last = ""
        while True:
            polls += 1
            response = self._admin_get(
                "/admin/user/usage", {"user_id": self.subject, "period": "daily"}
            )
            if response.status_code == 200:
                body = self._body(response)
                requests = int(body.get("requests", 0) or 0)
                last = f"requests={requests}"
                if requests >= 1:
                    elapsed = int(self.clock() - started)
                    return (
                        f"GET /admin/user/usage shows requests={requests}, "
                        f"input_tokens={body.get('input_tokens', '?')}, "
                        f"output_tokens={body.get('output_tokens', '?')}, "
                        f"cost_usd={body.get('cost_usd', '?')} after {elapsed} s "
                        f"({polls} poll{'s' if polls != 1 else ''})"
                    )
            elif response.status_code != 404:
                raise StepFailed(self._http_error(response))
            else:
                last = "HTTP 404"
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise StepFailed(
                    f"no metered request in the ledger after {int(self.timeout)} s "
                    f"({polls} polls, last {last}); invocation-log delivery normally "
                    "takes 1 to 2 minutes: check the InvocationLogGroup subscription "
                    "filter and the usage processor's log group"
                )
            self.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    def step_list_users(self) -> str:
        response = self._admin_get("/admin/users", {"query": USERNAME_PREFIX.rstrip("-")})
        if response.status_code != 200:
            raise StepFailed(self._http_error(response))
        users = self._body(response).get("users") or []
        ids = {user.get("user_id") for user in users if isinstance(user, dict)}
        if self.subject not in ids:
            raise StepFailed(
                f"GET /admin/users?query={USERNAME_PREFIX.rstrip('-')} returned "
                f"{len(users)} user(s) but not subject {self.subject}"
            )
        return f"GET /admin/users?query={USERNAME_PREFIX.rstrip('-')} lists the subject"

    def step_cleanup(self) -> str:
        if self.factory is not None:
            try:
                self.factory.close()
            except Exception:  # best effort; nothing to clean up remotely
                pass
        ledger_note = (
            f"; ledger rows for subject {self.subject} stay until usage_retention_days "
            "expires them (no admin route deletes usage)"
            if self.subject
            else ""
        )
        if not self.user_created:
            return "no Cognito user to delete" + ledger_note
        if self.keep_user:
            return f"--keep-user: Cognito user {self.username} kept" + ledger_note
        self.cognito.admin_delete_user(
            UserPoolId=self.outputs["DemoUserPoolId"], Username=self.username
        )
        return f"Cognito user {self.username} deleted" + ledger_note


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python tools/smoke_test.py",
        description=(
            "Non-interactive smoke test of a deployed Bedrock Spend Controls stack: "
            "admin API, credential vending, one Bedrock call, ledger, cleanup."
        ),
        epilog=(
            "Exit status 0 only when every step passed. The admin key is never printed; "
            "the throwaway Cognito user is deleted unless --keep-user."
        ),
    )
    parser.add_argument("--profile", metavar="NAME", help="AWS CLI profile")
    parser.add_argument("--region", metavar="REGION", required=True, help="stack Region")
    parser.add_argument(
        "--stack", metavar="NAME", default="BedrockSpendControls", help="stack name"
    )
    parser.add_argument(
        "--model",
        metavar="ID",
        help=(
            "model or inference-profile ID for the single converse call "
            "(default: Nova Micro through the Region's cross-Region profile)"
        ),
    )
    parser.add_argument(
        "--keep-user", action="store_true", help="do not delete the install-smoke user"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"how long to wait for the ledger (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import boto3  # deferred: the step logic above is testable without a session

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    smoke = SmokeTest(
        cloudformation=session.client("cloudformation"),
        secretsmanager=session.client("secretsmanager"),
        cognito=session.client("cognito-idp"),
        session=session,
        region=args.region,
        stack_name=args.stack,
        model=args.model,
        keep_user=args.keep_user,
        timeout=args.timeout,
    )
    print(
        f"Smoke test for stack {args.stack} in {args.region}"
        f"{' (profile ' + args.profile + ')' if args.profile else ''}, model {smoke.model}"
    )
    return exit_code(smoke.run())


if __name__ == "__main__":
    sys.exit(main())
