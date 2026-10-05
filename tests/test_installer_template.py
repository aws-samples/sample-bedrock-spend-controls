"""deploy/installer.yaml: the one-click (CodeBuild) installer template.

The template is parsed with a PyYAML loader that turns the CloudFormation
short-form tags (``!Ref``, ``!Sub``, ``!GetAtt`` ...) into plain mappings.
The tests pin the contract between the inline buildspec and ``install.sh``
(every flag exists, ``--profile`` is never passed), the IAM scoping of the
build role (CDK bootstrap qualifier ``hnb659fds``, read-only preflight
actions), the custom resource's Lambda handler (compiled and exercised with a
fake CodeBuild client and a captured response PUT), and run ``cfn-lint`` when
it is installed. Nothing here contacts AWS.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

yaml = pytest.importorskip("yaml")  # PyYAML, pinned in requirements-dev.txt

from tools.preflight.checks import REQUIRED_ACTIONS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy" / "installer.yaml"
README = ROOT / "deploy" / "README.md"
INSTALL_SH = ROOT / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"

EXPECTED_PARAMETERS = {
    "AlertEmail",
    "AdminEmail",
    "AcknowledgeLoggingOverwrite",
    "GitRef",
    "RerunToken",
    "ComputeType",
    "ExistingBuildRoleArn",
}
EXPECTED_INSTALL_FLAGS = {
    "--yes",
    "--config",
    "--region",
    "--alert-email",
    "--admin-email",
    "--acknowledge-logging-overwrite",
}
FORBIDDEN_INSTALL_FLAGS = {"--profile", "--skip-smoke", "--skip-preflight", "--destroy", "--dry-run"}
EXPECTED_ENVIRONMENT = {
    "ALERT_EMAIL",
    "ADMIN_EMAIL",
    "GIT_REF",
    "ACKNOWLEDGE_LOGGING_OVERWRITE",
    "AWS_REGION",
    "INSTALL_DIR",
    "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION",
    "CI",
}
EXPECTED_OUTPUTS = {"BuildProjectUrl", "BuildId", "BuildUrl", "BuildLogsUrl", "WatchCommand", "NextSteps"}
BOOTSTRAP_QUALIFIER = "hnb659fds"
STACK_NAME = "BedrockSpendControls"
EXAMPLE_ACCOUNT = "111122223333"
BUILD_ID = "bedrock-spend-controls-installer:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


# --- loading ---------------------------------------------------------------


class CfnLoader(yaml.SafeLoader):
    """SafeLoader that keeps CloudFormation's short-form intrinsic tags."""


def _construct_intrinsic(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> dict:
    key = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: object = loader.construct_scalar(node)
        if tag_suffix == "GetAtt":
            value = str(value).split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {key: value}


CfnLoader.add_multi_constructor("!", _construct_intrinsic)


@pytest.fixture(scope="module")
def text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def template(text: str) -> dict:
    return yaml.load(text, Loader=CfnLoader)


@pytest.fixture(scope="module")
def project(template: dict) -> dict:
    return template["Resources"]["InstallerProject"]["Properties"]


@pytest.fixture(scope="module")
def buildspec(project: dict) -> dict:
    return yaml.safe_load(project["Source"]["BuildSpec"])


@pytest.fixture(scope="module")
def install_command(buildspec: dict) -> str:
    commands = buildspec["phases"]["build"]["commands"]
    matches = [command for command in commands if "install.sh" in command]
    assert len(matches) == 1, commands
    return matches[0]


@pytest.fixture(scope="module")
def lambda_code(template: dict) -> str:
    return template["Resources"]["StartBuildFunction"]["Properties"]["Code"]["ZipFile"]


@pytest.fixture(scope="module")
def shims(tmp_path_factory) -> Path:
    """A PATH prefix whose aws/npx/npm print their argv and exit 0."""
    directory = tmp_path_factory.mktemp("installer-shims")
    for tool in ("aws", "npx", "npm"):
        shim = directory / tool
        shim.write_text(f'#!/bin/sh\necho "SHIM {tool} $*"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


# --- helpers ---------------------------------------------------------------


def _strings(node: object):
    """Every string inside a nested template fragment."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _refs(node: object) -> set[str]:
    """Names referenced with ``Ref`` anywhere inside ``node``."""
    found: set[str] = set()
    if isinstance(node, dict):
        if set(node) == {"Ref"} and isinstance(node["Ref"], str):
            found.add(node["Ref"])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _statements(role: dict) -> list[dict]:
    statements: list[dict] = []
    for policy in role["Properties"]["Policies"]:
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _sub_text(value: object) -> str:
    """The template string of a ``Fn::Sub`` (or a plain string)."""
    if isinstance(value, dict) and "Fn::Sub" in value:
        inner = value["Fn::Sub"]
        return inner if isinstance(inner, str) else inner[0]
    assert isinstance(value, str), value
    return value


def _resource_text(value: object) -> str:
    """A readable form of a policy Resource: Sub text, ``!GetAtt A.B``,
    ``!Ref X`` or the plain string."""
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return "!GetAtt " + ".".join(value["Fn::GetAtt"])
    if isinstance(value, dict) and "Ref" in value:
        return f"!Ref {value['Ref']}"
    return _sub_text(value)


def _install_sh_flags() -> set[str]:
    """The long options install.sh accepts (its argument ``case`` block)."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    start = script.index('while [ "$#" -gt 0 ]; do')
    end = script.index("esac", start)
    return set(re.findall(r"--[a-z][a-z-]*", script[start:end]))


def _node_major() -> int | None:
    node = shutil.which("node")
    if node is None:
        return None
    output = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout
    match = re.match(r"v(\d+)", output.strip())
    return int(match.group(1)) if match else None


# --- template shape --------------------------------------------------------


def test_template_is_small_valid_yaml_with_the_expected_sections(text: str, template: dict):
    assert len(text.encode("utf-8")) < 50 * 1024
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert "install.sh" in template["Description"]
    assert set(template) >= {"Parameters", "Conditions", "Mappings", "Resources", "Outputs", "Metadata"}
    assert set(template["Resources"]) == {
        "InstallerLogGroup",
        "BuildRole",
        "InstallerProject",
        "StartBuildLogGroup",
        "StartBuildRole",
        "StartBuildFunction",
        "RunInstaller",
    }


def test_parameters_contract(template: dict):
    parameters = template["Parameters"]
    assert set(parameters) == EXPECTED_PARAMETERS
    assert len(parameters) <= 10

    acknowledge = parameters["AcknowledgeLoggingOverwrite"]
    assert acknowledge["AllowedValues"] == ["yes"]
    assert "Default" not in acknowledge
    assert "overwrite" in acknowledge["Description"].lower()
    assert "logging" in acknowledge["Description"].lower()

    alert = parameters["AlertEmail"]
    assert "Default" not in alert
    pattern = re.compile(alert["AllowedPattern"])
    assert pattern.fullmatch("alerts@example.com")
    assert not pattern.fullmatch("")
    assert not pattern.fullmatch("not an address")
    assert not pattern.fullmatch("two@example.com three@example.com")

    admin = parameters["AdminEmail"]
    assert admin["Default"] == ""
    admin_pattern = re.compile(admin["AllowedPattern"])
    assert admin_pattern.fullmatch("")
    assert admin_pattern.fullmatch("admin@example.com")
    assert not admin_pattern.fullmatch("admin")

    git_ref = parameters["GitRef"]
    assert git_ref["Default"] == "main"
    ref_pattern = re.compile(git_ref["AllowedPattern"])
    for accepted in ("main", "v1.2.0", "release/2026-10", "feature_x"):
        assert ref_pattern.fullmatch(accepted), accepted
    for rejected in ("-rf", "a b", "ref;rm", "$(x)", ""):
        assert not ref_pattern.fullmatch(rejected), rejected

    assert parameters["ExistingBuildRoleArn"]["Default"] == ""
    role_pattern = re.compile(parameters["ExistingBuildRoleArn"]["AllowedPattern"])
    assert role_pattern.fullmatch("")
    assert role_pattern.fullmatch(f"arn:aws:iam::{EXAMPLE_ACCOUNT}:role/installer")
    assert not role_pattern.fullmatch("installer")

    compute = parameters["ComputeType"]
    assert compute["Default"] == "BUILD_GENERAL1_SMALL"
    assert compute["Default"] in compute["AllowedValues"]

    assert parameters["RerunToken"]["Default"] == ""

    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    grouped = [name for group in interface["ParameterGroups"] for name in group["Parameters"]]
    assert sorted(grouped) == sorted(parameters)
    assert set(interface["ParameterLabels"]) == set(parameters)


def test_conditions_pick_the_role_and_the_admin_email(template: dict, project: dict):
    conditions = template["Conditions"]
    assert conditions["CreateBuildRole"] == {"Fn::Equals": [{"Ref": "ExistingBuildRoleArn"}, ""]}
    assert conditions["UseAlertEmailAsAdmin"] == {"Fn::Equals": [{"Ref": "AdminEmail"}, ""]}

    assert template["Resources"]["BuildRole"]["Condition"] == "CreateBuildRole"
    assert project["ServiceRole"] == {
        "Fn::If": ["CreateBuildRole", {"Fn::GetAtt": ["BuildRole", "Arn"]}, {"Ref": "ExistingBuildRoleArn"}]
    }
    # install.sh rejects an empty --admin-email, so the fallback to the
    # alert address happens in the template, not in the buildspec.
    admin_email = {"Fn::If": ["UseAlertEmailAsAdmin", {"Ref": "AlertEmail"}, {"Ref": "AdminEmail"}]}
    environment = {item["Name"]: item["Value"] for item in project["Environment"]["EnvironmentVariables"]}
    assert environment["ADMIN_EMAIL"] == admin_email
    trigger = template["Resources"]["RunInstaller"]["Properties"]["Trigger"]
    assert trigger["AdminEmail"] == admin_email


# --- CodeBuild project ----------------------------------------------------


def test_codebuild_project_environment(template: dict, project: dict):
    assert project["Source"]["Type"] == "NO_SOURCE"
    assert project["Artifacts"] == {"Type": "NO_ARTIFACTS"}
    assert project["TimeoutInMinutes"] == 60
    assert project["ConcurrentBuildLimit"] == 1
    environment = project["Environment"]
    assert environment["Type"] == "LINUX_CONTAINER"
    assert environment["Image"] == "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    assert environment["ComputeType"] == {"Ref": "ComputeType"}
    assert environment["PrivilegedMode"] is False

    variables = {item["Name"]: item for item in environment["EnvironmentVariables"]}
    assert set(variables) == EXPECTED_ENVIRONMENT
    for item in variables.values():
        # Plain values or parameter references only: no Secrets Manager or
        # Parameter Store lookups, nothing that looks like a credential.
        assert item.get("Type", "PLAINTEXT") == "PLAINTEXT", item
        assert not re.search(r"secret|password|token|key", item["Name"], re.IGNORECASE), item
    assert variables["AWS_REGION"]["Value"] == {"Ref": "AWS::Region"}
    assert variables["ALERT_EMAIL"]["Value"] == {"Ref": "AlertEmail"}
    assert variables["GIT_REF"]["Value"] == {"Ref": "GitRef"}
    assert variables["ACKNOWLEDGE_LOGGING_OVERWRITE"]["Value"] == {"Ref": "AcknowledgeLoggingOverwrite"}
    assert variables["INSTALL_DIR"]["Value"] == "/tmp/sample-bedrock-spend-controls"
    assert variables["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"]["Value"] == "1"
    assert variables["CI"]["Value"] == "1"

    logs = project["LogsConfig"]["CloudWatchLogs"]
    assert logs == {"Status": "ENABLED", "GroupName": {"Ref": "InstallerLogGroup"}}
    assert project["LogsConfig"]["S3Logs"] == {"Status": "DISABLED"}
    assert template["Resources"]["InstallerLogGroup"]["Properties"]["RetentionInDays"] == 30


def test_buildspec_clones_the_repository_and_runs_install_sh(buildspec: dict, install_command: str, project: dict):
    assert buildspec["version"] == 0.2
    runtimes = buildspec["phases"]["install"]["runtime-versions"]
    assert runtimes == {"nodejs": 20, "python": 3.12}

    commands = buildspec["phases"]["build"]["commands"]
    assert len(commands) == 2
    clone = commands[0]
    assert clone.startswith("git clone --depth 1 --branch \"$GIT_REF\" ")
    assert "https://github.com/aws-samples/sample-bedrock-spend-controls.git" in clone
    assert clone.endswith('"$INSTALL_DIR"')
    assert install_command.startswith('cd "$INSTALL_DIR" && ./install.sh ')

    spec_text = project["Source"]["BuildSpec"]
    assert "curl" not in spec_text
    assert "| bash" not in spec_text
    assert "sudo" not in spec_text
    # Every shell variable the buildspec reads is a project environment
    # variable or one CodeBuild sets itself.
    used = set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", spec_text))
    assert used <= EXPECTED_ENVIRONMENT | {"CODEBUILD_BUILD_SUCCEEDING"}, used


def test_install_command_uses_only_flags_install_sh_accepts(install_command: str):
    flags = set(re.findall(r"--[a-z][a-z-]*", install_command))
    assert flags == EXPECTED_INSTALL_FLAGS
    assert not flags & FORBIDDEN_INSTALL_FLAGS
    assert flags <= _install_sh_flags()
    # Values travel through quoted environment variables, never literals.
    assert '--region "$AWS_REGION"' in install_command
    assert '--alert-email "$ALERT_EMAIL"' in install_command
    assert '--admin-email "$ADMIN_EMAIL"' in install_command
    assert "--config demo" in install_command
    # The acknowledgement flag is derived from the consent parameter.
    assert "${ACKNOWLEDGE_LOGGING_OVERWRITE:+--acknowledge-logging-overwrite}" in install_command
    assert "--profile" not in install_command


def _run_install_command(install_command: str, shims: Path, acknowledge: bool) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"AWS_PROFILE", "ALERT_EMAIL", "ADMIN_EMAIL", "SMOKE_MODEL", "ACKNOWLEDGE_LOGGING_OVERWRITE"}
    }
    env.update(
        {
            "PATH": f"{shims}{os.pathsep}{env['PATH']}",
            "HOME": env.get("HOME", str(ROOT)),
            "INSTALL_DIR": str(ROOT),
            "AWS_REGION": "us-east-1",
            "ALERT_EMAIL": "alerts@example.com",
            "ADMIN_EMAIL": "admin@example.com",
            "GIT_REF": "main",
            "CI": "1",
        }
    )
    if acknowledge:
        env["ACKNOWLEDGE_LOGGING_OVERWRITE"] = "yes"
    return subprocess.run(
        [BASH, "-c", f"{install_command} --dry-run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_install_command_dry_runs_through_install_sh(install_command: str, shims: Path):
    """The exact buildspec command, executed by install.sh in dry-run mode
    with the project's environment variables: it must parse and plan every
    phase without a profile and without touching AWS (aws/npm/npx are shims)."""
    node = _node_major()
    if node is None or node < 20:
        pytest.skip("install.sh needs Node.js 20+ on PATH")

    completed = _run_install_command(install_command, shims, acknowledge=True)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    out = completed.stdout
    assert "SHIM" not in out
    assert "dry run" in out
    assert "profile:     (default credentials)" in out
    assert "region:      us-east-1" in out
    preflight = out.split("==> [1/9] preflight", 1)[1].split("==> [2/9]", 1)[0]
    assert re.search(
        r"-m tools\.preflight --config \S*demo\.json --region us-east-1 --json "
        r"--set alert_email=alerts@example\.com --set admin_email=admin@example\.com "
        r"--acknowledge-logging-overwrite",
        preflight,
    )
    assert "--profile" not in out
    assert "-c alert_email=alerts@example.com -c admin_email=admin@example.com" in out

    # Without the acknowledgement variable the flag is not passed, so the
    # preflight would stop the install when logging is already configured.
    completed = _run_install_command(install_command, shims, acknowledge=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    preflight = completed.stdout.split("==> [1/9] preflight", 1)[1].split("==> [2/9]", 1)[0]
    assert "--acknowledge-logging-overwrite" not in preflight


# --- IAM -------------------------------------------------------------------


def test_build_role_trusts_codebuild_from_this_account_only(template: dict):
    role = template["Resources"]["BuildRole"]
    assert role["Type"] == "AWS::IAM::Role"
    assert "RoleName" not in role["Properties"], "a fixed name would collide across Regions"
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    assert trust[0]["Principal"] == {"Service": "codebuild.amazonaws.com"}
    assert trust[0]["Action"] == "sts:AssumeRole"
    assert trust[0]["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert ":codebuild:" in _sub_text(trust[0]["Condition"]["ArnLike"]["aws:SourceArn"])


def test_build_role_is_scoped_to_the_cdk_bootstrap_and_read_only_elsewhere(template: dict):
    statements = _statements(template["Resources"]["BuildRole"])
    assert all(statement["Effect"] == "Allow" for statement in statements)
    by_sid = {statement["Sid"]: statement for statement in statements}

    # cdk deploy works by assuming the bootstrap roles of the default qualifier.
    assume = by_sid["AssumeCdkBootstrapRoles"]
    assert _as_list(assume["Action"]) == ["sts:AssumeRole"]
    assert _sub_text(assume["Resource"]).endswith(f":role/cdk-{BOOTSTRAP_QUALIFIER}-*")

    granted: set[str] = set()
    for statement in statements:
        actions = _as_list(statement["Action"])
        resources = [_resource_text(resource) for resource in _as_list(statement["Resource"])]
        granted |= set(actions)
        for action in actions:
            assert action not in ("*", "*:*"), statement
            if action.endswith(":*"):
                # Service-wide wildcards exist only for cdk bootstrap and
                # only on resources named after the bootstrap qualifier.
                assert statement["Sid"].startswith("Bootstrap"), statement
                for resource in resources:
                    assert BOOTSTRAP_QUALIFIER in resource or "stack/CDKToolkit/" in resource, resource
        if resources == ["*"]:
            # Anything granted on every resource must be read-only.
            for action in actions:
                verb = action.split(":", 1)[1]
                assert re.match(r"(Get|List|Describe|Simulate|Filter)", verb), action

    # The preflight runs inside the build: each action it needs is granted.
    for check, actions in REQUIRED_ACTIONS.items():
        for action in actions:
            assert action in granted, f"{check} needs {action}"

    # cdk bootstrap on an account that is not bootstrapped yet.
    assert "cloudformation:*" in _as_list(by_sid["BootstrapStack"]["Action"])
    assert "stack/CDKToolkit/*" in _sub_text(by_sid["BootstrapStack"]["Resource"])
    assert set(_as_list(by_sid["BootstrapRoles"]["Action"])) == {"iam:*"}
    assert {f"cdk-{BOOTSTRAP_QUALIFIER}-*" in _sub_text(r) for r in by_sid["BootstrapRoles"]["Resource"]} == {True}
    bucket = [_sub_text(r) for r in by_sid["BootstrapBucket"]["Resource"]]
    assert any(r.endswith(f"cdk-{BOOTSTRAP_QUALIFIER}-assets-${{AWS::AccountId}}-${{AWS::Region}}") for r in bucket)
    assert any(r.endswith("/*") for r in bucket)
    assert f"repository/cdk-{BOOTSTRAP_QUALIFIER}-container-assets-*" in _sub_text(by_sid["BootstrapRepository"]["Resource"])
    assert f"parameter/cdk-bootstrap/{BOOTSTRAP_QUALIFIER}/*" in _sub_text(by_sid["BootstrapVersionParameter"]["Resource"])
    assert "kms:*" not in granted, "the default bootstrap uses the AWS managed key"

    # Recovering from a failed first create is scoped to the deployed stack
    # and its retained invocation log group (install.sh asks, --yes answers).
    recover = by_sid["RecoverFailedCreate"]
    assert _as_list(recover["Action"]) == ["cloudformation:DeleteStack"]
    assert _sub_text(recover["Resource"]).endswith(f":stack/{STACK_NAME}/*")
    orphan = by_sid["RecoverRetainedLogGroup"]
    assert _as_list(orphan["Action"]) == ["logs:DeleteLogGroup"]
    assert _sub_text(orphan["Resource"]).endswith(":log-group:/bedrock/spend-controls/model-invocations:*")

    # The build writes only to its own log group.
    logs = by_sid["WriteBuildLogs"]
    assert logs["Resource"] == {"Fn::GetAtt": ["InstallerLogGroup", "Arn"]}

    # The smoke test touches the deployed stack's resources only.
    # Secrets are named after their logical ID (AdminApiKey<hash>-<random>),
    # not after the stack: the first live smoke test under CodeBuild failed
    # on a BedrockSpendControls* pattern.
    assert _sub_text(by_sid["SmokeTestAdminKey"]["Resource"]).endswith(":secret:AdminApiKey*")
    assert set(_as_list(by_sid["SmokeTestCognitoUser"]["Action"])) == {
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminDeleteUser",
    }
    broker = by_sid["SmokeTestBrokerUrl"]
    assert _as_list(broker["Action"]) == ["lambda:InvokeFunctionUrl"]
    assert _sub_text(broker["Resource"]).endswith(f":function:{STACK_NAME}-*")
    assert broker["Condition"] == {"StringEquals": {"lambda:FunctionUrlAuthType": "AWS_IAM"}}
    # IAM-authenticated Function URLs also evaluate lambda:InvokeFunction for
    # the call (the first live smoke test under CodeBuild got 403 without it).
    via_url = by_sid["SmokeTestBrokerFunction"]
    assert _as_list(via_url["Action"]) == ["lambda:InvokeFunction"]
    assert _sub_text(via_url["Resource"]).endswith(f":function:{STACK_NAME}-*")
    assert via_url["Condition"] == {"Bool": {"lambda:InvokedViaFunctionUrl": "true"}}


def test_start_build_role_can_only_start_and_read_the_project(template: dict):
    role = template["Resources"]["StartBuildRole"]
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "lambda.amazonaws.com"}
    statements = _statements(role)
    actions = {action for statement in statements for action in _as_list(statement["Action"])}
    assert actions == {"logs:CreateLogStream", "logs:PutLogEvents", "codebuild:StartBuild", "codebuild:BatchGetBuilds"}
    for statement in statements:
        if "codebuild:StartBuild" in _as_list(statement["Action"]):
            assert statement["Resource"] == {"Fn::GetAtt": ["InstallerProject", "Arn"]}
        else:
            assert statement["Resource"] == {"Fn::GetAtt": ["StartBuildLogGroup", "Arn"]}


# --- custom resource -----------------------------------------------------


def test_lambda_function_is_small_inline_python(template: dict, lambda_code: str):
    function = template["Resources"]["StartBuildFunction"]["Properties"]
    assert function["Runtime"] == "python3.12"
    assert function["Handler"] == "index.handler"
    assert function["Timeout"] <= 900
    assert function["Role"] == {"Fn::GetAtt": ["StartBuildRole", "Arn"]}
    assert function["LoggingConfig"] == {"LogGroup": {"Ref": "StartBuildLogGroup"}}
    assert len(lambda_code.encode("utf-8")) <= 4096
    compile(lambda_code, "index.py", "exec")
    assert "def handler(event, context)" in lambda_code
    for needle in ('"SUCCESS"', '"FAILED"', '"Delete"', "start_build", "urllib.request", "ResponseURL"):
        assert needle in lambda_code, needle
    assert "cfnresponse" not in lambda_code
    assert "${" not in lambda_code, "the code reads its inputs from ResourceProperties, not Fn::Sub"


def test_custom_resource_contract(template: dict):
    resource = template["Resources"]["RunInstaller"]
    assert resource["Type"] == "Custom::RunInstaller"
    properties = resource["Properties"]
    assert properties["ServiceToken"] == {"Fn::GetAtt": ["StartBuildFunction", "Arn"]}
    assert properties["ProjectName"] == {"Ref": "InstallerProject"}
    assert properties["Region"] == {"Ref": "AWS::Region"}
    assert properties["LogGroupName"] == {"Ref": "InstallerLogGroup"}
    assert properties["ConsoleDomain"] == {"Fn::FindInMap": ["ConsoleDomain", {"Ref": "AWS::Partition"}, "Host"]}
    assert set(template["Mappings"]["ConsoleDomain"]) == {"aws", "aws-cn", "aws-us-gov"}
    # Changing any of these on a stack update re-runs the build.
    trigger = properties["Trigger"]
    assert _refs(trigger) >= {"GitRef", "AlertEmail", "AdminEmail", "RerunToken"}


def test_outputs(template: dict):
    outputs = template["Outputs"]
    assert set(outputs) == EXPECTED_OUTPUTS
    assert outputs["BuildId"]["Value"] == {"Fn::GetAtt": ["RunInstaller", "BuildId"]}
    assert outputs["BuildUrl"]["Value"] == {"Fn::GetAtt": ["RunInstaller", "BuildUrl"]}
    assert outputs["BuildLogsUrl"]["Value"] == {"Fn::GetAtt": ["RunInstaller", "LogsUrl"]}
    project_url = _sub_text(outputs["BuildProjectUrl"]["Value"])
    assert "/codesuite/codebuild/${AWS::AccountId}/projects/${InstallerProject}/" in project_url
    watch = _sub_text(outputs["WatchCommand"]["Value"])
    assert "aws codebuild batch-get-builds" in watch
    assert "--ids ${RunInstaller.BuildId}" in watch
    assert "--query 'builds[0].buildStatus'" in watch
    next_steps = _sub_text(outputs["NextSteps"]["Value"])
    assert "AdminUiUrl" in next_steps
    assert STACK_NAME in next_steps
    assert "email" in next_steps.lower()
    assert "does not remove" in next_steps


# --- the handler, executed ------------------------------------------------


class _FakeCodeBuild:
    def __init__(self, calls: list, error: Exception | None) -> None:
        self.calls = calls
        self.error = error

    def start_build(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "build": {
                "id": BUILD_ID,
                "arn": f"arn:aws:codebuild:us-east-1:{EXAMPLE_ACCOUNT}:build/{BUILD_ID}",
                "buildStatus": "IN_PROGRESS",
            }
        }


class _FakeHttpResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return b""


def _event(request_type: str, **overrides) -> dict:
    event = {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example.com/respond",
        "StackId": f"arn:aws:cloudformation:us-east-1:{EXAMPLE_ACCOUNT}:stack/installer/11111111-2222-3333-4444-555555555555",
        "RequestId": "request-1",
        "LogicalResourceId": "RunInstaller",
        "ResourceType": "Custom::RunInstaller",
        "ResourceProperties": {
            "ServiceToken": f"arn:aws:lambda:us-east-1:{EXAMPLE_ACCOUNT}:function:installer-start-build",
            "ProjectName": "bedrock-spend-controls-installer",
            "Region": "us-east-1",
            "ConsoleDomain": "console.aws.amazon.com",
            "LogGroupName": "/aws/codebuild/bedrock-spend-controls-installer",
            "Trigger": {"GitRef": "main", "AlertEmail": "alerts@example.com", "AdminEmail": "alerts@example.com", "RerunToken": ""},
        },
    }
    event.update(overrides)
    return event


@pytest.fixture
def run_handler(lambda_code: str, monkeypatch):
    """Execute the inline code with a fake boto3 client and a captured
    response PUT; returns (start_build calls, PUT requests)."""
    import boto3

    def runner(event: dict, *, error: Exception | None = None, send_error: Exception | None = None):
        starts: list[dict] = []
        puts: list[dict] = []
        monkeypatch.setattr(boto3, "client", lambda service, **kwargs: _FakeCodeBuild(starts, error) if service == "codebuild" else None)

        def fake_urlopen(request, timeout=None):
            if send_error is not None:
                raise send_error
            puts.append({"url": request.full_url, "method": request.get_method(), "body": json.loads(request.data), "timeout": timeout})
            return _FakeHttpResponse()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        namespace: dict = {}
        exec(compile(lambda_code, "index.py", "exec"), namespace)  # noqa: S102 - the template's own code
        context = SimpleNamespace(log_stream_name="2026/10/05/[$LATEST]0123456789abcdef", function_name="start-build")
        result = namespace["handler"](event, context)
        assert result is None
        return starts, puts

    return runner


def test_handler_create_starts_the_build_and_reports_success(run_handler):
    starts, puts = run_handler(_event("Create"))
    assert starts == [{"projectName": "bedrock-spend-controls-installer"}]
    assert len(puts) == 1
    put = puts[0]
    assert put["method"] == "PUT"
    assert put["url"] == _event("Create")["ResponseURL"]
    body = put["body"]
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == BUILD_ID
    assert body["StackId"] == _event("Create")["StackId"]
    assert body["RequestId"] == "request-1"
    assert body["LogicalResourceId"] == "RunInstaller"
    data = body["Data"]
    assert data["BuildId"] == BUILD_ID
    assert data["BuildArn"].endswith(f":build/{BUILD_ID}")
    assert data["BuildUrl"].startswith(
        f"https://us-east-1.console.aws.amazon.com/codesuite/codebuild/{EXAMPLE_ACCOUNT}"
        "/projects/bedrock-spend-controls-installer/build/bedrock-spend-controls-installer%3A"
    )
    assert data["BuildUrl"].endswith("/?region=us-east-1")
    assert data["LogsUrl"] == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logEvent:"
        "group=/aws/codebuild/bedrock-spend-controls-installer;stream=0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    )
    assert BUILD_ID in body["Reason"]


def test_handler_update_starts_a_new_build(run_handler):
    starts, puts = run_handler(_event("Update", PhysicalResourceId="previous-build-id"))
    assert len(starts) == 1
    assert puts[0]["body"]["Status"] == "SUCCESS"
    assert puts[0]["body"]["PhysicalResourceId"] == BUILD_ID


def test_handler_delete_succeeds_without_starting_anything(run_handler):
    starts, puts = run_handler(_event("Delete", PhysicalResourceId=BUILD_ID))
    assert starts == []
    assert len(puts) == 1
    body = puts[0]["body"]
    assert body["Status"] == "SUCCESS"
    assert body["PhysicalResourceId"] == BUILD_ID
    assert body["Data"] == {}


def test_handler_reports_a_start_failure_to_cloudformation(run_handler):
    error = RuntimeError("AccountLimitExceededException: concurrent build limit reached")
    starts, puts = run_handler(_event("Create"), error=error)
    assert len(starts) == 1
    assert len(puts) == 1
    body = puts[0]["body"]
    assert body["Status"] == "FAILED"
    assert "could not start the installer build" in body["Reason"]
    assert "concurrent build limit reached" in body["Reason"]
    assert body["PhysicalResourceId"] == "2026/10/05/[$LATEST]0123456789abcdef"
    assert body["Data"] == {}


def test_handler_never_raises_when_the_response_cannot_be_sent(run_handler):
    # Raising would make Lambda retry the asynchronous invocation and start a
    # second build; the handler logs and returns instead.
    starts, puts = run_handler(_event("Create"), send_error=OSError("network unreachable"))
    assert len(starts) == 1
    assert puts == []


# --- lint and hygiene ------------------------------------------------------


def test_cfn_lint_passes():
    candidates = [shutil.which("cfn-lint"), str(Path(sys.executable).with_name("cfn-lint"))]
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if executable is None:
        pytest.skip("cfn-lint is not installed (pip install -r requirements-dev.txt)")
    completed = subprocess.run(
        [executable, "--", str(TEMPLATE)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    # 0 = clean; 2 = errors, 4 = warnings, 6 = both. Warnings fail too.
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_template_and_readme_hygiene(text: str):
    readme = README.read_text(encoding="utf-8")
    for name, content in (("installer.yaml", text), ("README.md", readme)):
        account_ids = set(re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", content))
        assert account_ids <= {EXAMPLE_ACCOUNT}, (name, account_ids)
        assert not re.search(r"\b(whitelist|blacklist|master|slave)\b", content, re.IGNORECASE), name
        assert "AKIA" not in content, name
    # The README documents the contract the template relies on.
    for needle in (
        "#/stacks/create/template",
        "Upload a template file",
        "bedrock-spend-controls-installer",
        "15 minutes",
        "ExistingBuildRoleArn",
        "aws cloudformation delete-stack",
        "aws codebuild batch-get-builds",
        f"cdk-{BOOTSTRAP_QUALIFIER}-*",
        "AdminUiUrl",
    ):
        assert needle in readme, needle
