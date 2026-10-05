# Installer reference

Three entry points install Bedrock Spend Controls. They share one engine:
`tools/preflight/` runs the account and Region checks, and
`cdk/stacks/configuration.py` validates the deployment file exactly as
`cdk synth` does, so the checks, the wizard, and the deploy never disagree.

| Entry point | For | How it runs |
|---|---|---|
| [`install.sh`](#installsh) | The demo in a personal account, or any deployment file, from a terminal or CloudShell | One command: preflight, build, `cdk bootstrap`, `cdk synth`, `cdk diff`, `cdk deploy`, outputs, smoke test |
| [`deploy/installer.yaml`](#deployinstalleryaml-one-click) | The demo from the CloudFormation console, no terminal | A CodeBuild project clones the repository and runs `install.sh --yes --config demo` |
| [`setup.py`](#setuppy-configuration-wizard) | A production deployment file for a shared account and your IdP | Interactive questions validated live; writes `cdk/config/<name>.local.json`; `--deploy` hands over to `install.sh` |

[`tools/preflight`](#toolspreflight) and [`tools/smoke_test.py`](#smoke-test)
can also be run on their own. The manual steps each tool automates are in
[DEPLOYMENT.md](../DEPLOYMENT.md).

## `install.sh`

```text
install.sh [--profile P] [--region R] [--config demo|path.json]
           [--alert-email E] [--admin-email E] [--acknowledge-logging-overwrite]
           [--yes] [--skip-smoke] [--skip-preflight] [--destroy] [--dry-run] [-h]
```

The script sequences the documented commands and reports; every decision
lives in Python (`tools/preflight`, `cdk/stacks/configuration.py`,
`tools/smoke_test.py`). It runs on Amazon Linux 2023 (CloudShell) and on
macOS with the stock bash 3.2. It installs nothing on the host: Node.js 20
or later with npm, Python 3.12 or later, the AWS CLI, and `git` (only to
clone) must already be present, and the script stops with installation
hints when they are not (see [Troubleshooting](#troubleshooting)). Exit
status is 0 when every phase succeeded or was skipped, 1 otherwise; the
failing phase is named on stderr and in the summary table printed at the
end.

### Running it without a checkout

```bash
curl -fsSL https://raw.githubusercontent.com/aws-samples/sample-bedrock-spend-controls/main/install.sh \
  | bash -s -- --alert-email you@example.com --acknowledge-logging-overwrite
```

When the script is not running from inside a checkout (as under `curl |
bash`) it clones the repository at `GIT_REF` (default `main`, `--depth 1`)
into `INSTALL_DIR` (default `~/sample-bedrock-spend-controls`) and
re-executes itself from there with the same arguments; an existing
`INSTALL_DIR/install.sh` is reused and `GIT_REF` is then ignored. Questions
are asked on the terminal (`/dev/tty`), never on stdin, so a piped script
can still ask for the alert address or a confirmation; without a terminal
the script stops at the first question and asks you to re-run with `--yes`.
Pin a release tag with `GIT_REF=<tag>` for a reproducible install, or
clone first and read the script (see
[threat-model.md](threat-model.md), T-33).

### Phases

| # | Phase | What happens | Skipped when |
|---|---|---|---|
| 1 | `preflight` | Creates `cdk/.venv` (pinned `cdk/requirements.txt` plus `examples/requirements.txt`) and runs `python -m tools.preflight --config <file> --region <region> --json`, passing `--profile`, `--set alert_email=`, `--set admin_email=`, and `--acknowledge-logging-overwrite` through. Prints the check table followed by the `Installer verdict` line and stops on `FAIL`; with warnings it asks before continuing | `--skip-preflight` |
| 2 | `build` | `npm ci && npm run build` in `admin-ui/` when `admin_ui` is `true` in the deployment file; `npm ci` in `cdk/` | never (the console build is skipped when `admin_ui` is `false`) |
| 3 | `bootstrap` | `npx cdk bootstrap aws://<account>/<region>` with the deployment context | the preflight `bootstrap` check passed |
| 4 | `synth` | `npx cdk synth --quiet`; the template lands in `cdk/cdk.out` | never |
| 5 | `diff` | When the stack already exists: `npx cdk diff`, then "Deploy these changes?" | the stack does not exist yet |
| 6 | `deploy` | First repairs the two states a failed first deploy leaves behind, each after a confirmation: a stack in `ROLLBACK_COMPLETE` (or `ROLLBACK_FAILED`, `CREATE_FAILED`, `DELETE_FAILED`) is deleted, and a `/bedrock/spend-controls/model-invocations` log group that exists without the stack is deleted when the file sets `manage_invocation_logging: true`. Then `npx cdk deploy --require-approval never` | never |
| 7 | `outputs` | Reads the stack outputs and writes [`.install-outputs.env`](#install-outputsenv) | never |
| 8 | `smoke` | Creates `.venv-examples` (`examples/requirements.txt`) and runs [`tools/smoke_test.py`](#smoke-test) against the stack | `--skip-smoke` |
| 9 | `done` | Prints the console URL, the broker URL, where the administrator password went, the SNS subscription reminder, the outputs path, and the `--destroy` command | never |

The confirmations in phases 1, 5, and 6 are answered by `--yes`, which the
one-click installer passes because CodeBuild has no terminal.

### Flags

| Flag | Meaning |
|---|---|
| `--profile P` | AWS CLI profile (default `$AWS_PROFILE`; otherwise the default credential chain) |
| `--region R` | Target Region (default `$AWS_REGION`, then `$AWS_DEFAULT_REGION`, then the profile's configured region) |
| `--config demo\|path.json` | Deployment file. `demo` is `cdk/config/demo.json` (default); any other value is a path, for example the file `setup.py` wrote |
| `--alert-email E` | SNS alert address, passed as the `alert_email` context. Required with the demo file (asked for on the terminal when omitted); with another file it defaults to the file's value |
| `--admin-email E` | Address of the console administrator (`admin_email` context). With the demo file it defaults to the alert address; without it no administrator is created |
| `--acknowledge-logging-overwrite` | Accept that `manage_invocation_logging: true` overwrites the Region's existing Bedrock invocation logging configuration; without it the preflight fails when logging is already configured by someone else |
| `--yes` | No questions: continue on preflight warnings, skip the diff confirmation, confirm the repairs in the deploy phase and `--destroy` |
| `--skip-smoke` | Do not run the smoke test |
| `--skip-preflight` | Do not run the preflight; `cdk bootstrap` is then always attempted (it is idempotent) |
| `--destroy` | Destroy the stack instead of installing ([below](#--destroy)) |
| `--dry-run` | Print the commands (prefixed `+`) without running them; nothing contacts AWS. Only inside a checkout |

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GIT_REF` | `main` | Branch or tag cloned when the script does not run from a checkout |
| `INSTALL_DIR` | `~/sample-bedrock-spend-controls` | Where that clone goes (an existing checkout there is reused) |
| `ALERT_EMAIL`, `ADMIN_EMAIL` | unset | Defaults for `--alert-email` and `--admin-email` |
| `SMOKE_MODEL` | unset | Model or inference-profile ID for the smoke test's single `Converse` call (default: Nova Micro through the Region's cross-Region profile) |
| `AWS_PROFILE`, `AWS_REGION` | unset | Defaults for `--profile` and `--region`; the script exports `AWS_REGION`, `AWS_DEFAULT_REGION`, `CDK_DEFAULT_REGION`, and `CDK_DEFAULT_ACCOUNT` for the CDK commands it runs |

### `.install-outputs.env`

Written in the repository root after the deploy, mode `600`, ignored by
Git, removed by `--destroy`. It holds stack outputs only, no secrets:

```bash
BROKER_API_URL=...   # BrokerApiUrl without the trailing slash
ADMIN_UI_URL=...     # AdminUiUrl (empty when admin_ui is false)
USER_POOL_ID=...     # DemoUserPoolId (demo Cognito only)
CLIENT_ID=...        # DemoUserPoolClientId (demo Cognito only)
STACK_NAME=BedrockSpendControls
AWS_REGION=...
```

Source it (`. .install-outputs.env`) before the manual smoke tests in
[DEPLOYMENT.md](../DEPLOYMENT.md#6-administrative-smoke-test), which use
the same variable names.

### `--destroy`

`./install.sh --destroy --region <region> [--profile P] [--config path.json]`
is the supported teardown. It asks for confirmation (`--yes` answers it),
runs `npx cdk destroy --force` with the same deployment context, deletes
`.install-outputs.env`, and then lists what is retained on purpose, with the
commands that remove each item once nothing else depends on it:

- the Bedrock model-invocation logging configuration of the Region,
- the log group `/bedrock/spend-controls/model-invocations`,
- the Bedrock logging role (name starts with
  `BedrockSpendControls-BedrockLoggingRole`),
- with `retain_tables_on_delete: true`, the DynamoDB tables (deletion
  protection on).

The stack retains the logging resources because it cannot restore the
account-wide setting that existed before it. A later install in the same
Region deletes the orphaned log group after asking (phase 6), or reuses it
with `manage_invocation_logging: false` and `invocation_log_group_name`.

## `tools/preflight`

```text
python -m tools.preflight --config cdk/config/demo.json [--profile P] [--region R]
    [--account-id A] [--set key=value ...] [--acknowledge-logging-overwrite]
    [--sample-jwt TOKEN|@file] [--app-role-arn ARN] [--require-admin-ui-build]
    [--checks a,b] [--json]
```

Read-only checks that a deployment will succeed in this account and Region.
Run it from the repository root with the CDK Python environment
(`cdk/.venv`, or a virtualenv from `requirements-dev.txt`): the deployment
file is validated with the CDK app's own validator first, and a file that
`cdk synth` would reject exits with status 2 and the validator's message.
`--set key=value` overrides a key before validation (lists are
comma-separated; JSON is accepted for objects and arrays).

Every check returns `PASS`, `WARN`, `FAIL`, or `SKIP` with a detail and,
for warnings and failures, a `fix` line. An AWS `AccessDenied` on any call
is reported as `WARN` naming the missing action, never as a failure. The
checks run in this order:

| Check | Verifies | Fails when |
|---|---|---|
| `toolchain` | Node.js 20+, npm, Python 3.12+ with pip, AWS CLI v2, and `git` on `PATH` | a tool is missing or too old |
| `credentials` | `sts:GetCallerIdentity`: account, Region, partition; `--account-id` matches | the credentials are unusable, the account differs, or the Region is an opt-in Region the account has not enabled (named in the detail; the Region-bound checks below are then skipped). A principal name containing `readonly` or `viewonly` is a warning |
| `bootstrap` | The `CDKToolkit` stack exists, is healthy, and its template version (SSM `/cdk-bootstrap/hnb659fds/version`, or the stack output) is at least 21; older than 28 warns | the environment is not bootstrapped, the bootstrap stack is unhealthy, or the version is below 21 |
| `bedrock_model_access` | Every `allowed_model_arns` entry and every workload model is enabled for the account in its Region (`GetFoundationModelAvailability`); inference profiles are expanded to their foundation models first (`GetInferenceProfile`). `*` is an advisory warning | a model is not enabled or does not exist |
| `invocation_logging` | With `manage_invocation_logging: true`: nothing is configured in the Region, or logging already delivers to the stack-managed group. With `invocation_log_group_name`: the group exists; a warning when logging does not deliver to it or it has received nothing in 24 hours | another destination is configured and `--acknowledge-logging-overwrite` was not given (it turns the failure into a warning); the named group does not exist; neither key is set |
| `lambda_concurrency` | `GetAccountSettings`: after the five reserved executions the stack pins, at least 10 unreserved remain | the account cannot spare them (fix: `reserve_enforcement_concurrency: false` or a quota increase) |
| `oidc_issuer` | Your issuer's `/.well-known/openid-configuration` and JWKS are reachable over HTTPS and consistent; `--sample-jwt` inspects a token's claims locally (never sent or verified) for `jwt_user_claim` and the admin claim | discovery or the JWKS fail, or the issuer does not match. Skipped with the demo Cognito pool |
| `invoker_principals` | Each `invoker_principal_arns` entry exists (`iam:GetRole` / `iam:GetUser`); cross-account principals are listed as unverified | a principal does not exist. Skipped when the list is empty |
| `scp_bypass` | `iam:SimulatePrincipalPolicy` for the role given with `--app-role-arn`: `bedrock:InvokeModel` on foundation models must be denied | the role is allowed to call Bedrock directly. Skipped without `--app-role-arn` |
| `quotas_cost` | Offline: the monthly cost estimate from `tools/estimate_cost.py` for the scenario matching the file | never (informational) |
| `region_support` | Amazon Bedrock, the public Lambda Web Adapter layer (`lambda:GetLayerVersion`, with a static Region list as fallback), Cognito user pools when the stack creates the IdP, and CloudFront when `admin_ui` is `true` are available in the Region and partition | a required service is missing; the fix lists the supported Regions |
| `admin_ui_build` | `admin-ui/dist/index.html` exists when `admin_ui` is `true` | only with `--require-admin-ui-build`; otherwise a warning. Skipped when `admin_ui` is `false` |

Exit status: 0 when no check failed (warnings allowed), 1 when at least one
failed, 2 for a usage or configuration error. `--json` prints
`{"account", "region", "ok", "results": [{"name", "status", "title",
"detail", "fix"}, ...]}`, the form `install.sh` and the wizard read.

**The installer's reading.** `install.sh` renders the same table and adds
an `Installer verdict: OK|FAIL` line (`python -m tools.preflight.verdict`).
Two findings do not fail the verdict because the next phases resolve them:
a missing or outdated `bootstrap` (phase 3 runs `cdk bootstrap`) and a
missing `admin_ui_build` (phase 2 builds the console); the line then says
`(bootstrap, admin_ui_build: handled by the next phases)`. The
`allowed_model_arns` `*` advisory is ignored for the same reason: the demo
file sets it on purpose. Any other `WARN` adds `; warnings need a look` and
a confirmation.

**IAM actions.** Everything is read-only. The checks call
`sts:GetCallerIdentity`, `cloudformation:DescribeStacks`,
`ssm:GetParameter`, `bedrock:GetFoundationModelAvailability`,
`bedrock:GetInferenceProfile`,
`bedrock:GetModelInvocationLoggingConfiguration`, `logs:DescribeLogGroups`,
`logs:FilterLogEvents`, `lambda:GetAccountSettings`,
`lambda:GetLayerVersion`, `iam:GetRole`, `iam:GetUser`, and
`iam:SimulatePrincipalPolicy` (`REQUIRED_ACTIONS` in
`tools/preflight/checks.py`), so a read-only role can run the preflight;
the `credentials` check then warns that `cdk deploy` will need more.

## Smoke test

```text
python tools/smoke_test.py --region us-east-1 --stack BedrockSpendControls
    [--profile P] [--model us.amazon.nova-micro-v1:0] [--keep-user] [--timeout 420]
```

The non-interactive form of DEPLOYMENT.md steps 6 and 7, run by
`install.sh` after the deploy and usable on its own against a stack whose
identity provider is the stack-created Cognito pool (it needs the
`DemoUserPoolId` and `DemoUserPoolClientId` outputs). Dependencies:
`examples/requirements.txt` in a virtualenv (`install.sh` creates
`.venv-examples`). Every step prints `PASS`, `FAIL`, or `SKIP` (not
attempted after an earlier failure); the exit status is 0 only when every
step passed.

| Step | What it does |
|---|---|
| `outputs` | `describe-stacks`; the required outputs are present |
| `admin-key` | Reads the routine admin key from Secrets Manager (`AdminKeySecretArn`); it is never printed |
| `healthz` | `GET /healthz` on the broker returns `200` and `status: ok` |
| `cognito-user` | Creates the quota user `install-smoke-<6 hex>` with a random permanent password (`MessageAction: SUPPRESS`, no email) and obtains an ID token with `USER_PASSWORD_AUTH` |
| `vend` | `POST /v1/credentials` through `examples/refreshable_bedrock.py` returns a complete credential set |
| `converse` | One `Converse` call with `maxTokens: 5` on `--model` (default `us.`, `eu.`, or `apac.` `amazon.nova-micro-v1:0` by Region, else the base model) using the vended credentials |
| `ledger` | Polls `GET /admin/user/usage` every 15 s until the request appears (`--timeout`, default 420 s; delivery normally takes 1 to 2 minutes) |
| `list-users` | `GET /admin/users?query=install-smoke` lists the subject |
| `cleanup` | Deletes the Cognito user unless `--keep-user` (always attempted, even after a failure) |

What it leaves behind: the subject's ledger rows (usage, per-model, and
request markers) stay until `usage_retention_days` expires them, because
there is no admin route that deletes usage; the console lists the subject
until then. The invocation itself is a few tokens of Nova Micro. A
`converse` failure with `AccessDeniedException`, `ResourceNotFoundException`,
or `ValidationException` means the account has no access to that model in
the Region: enable it, or pass `--model` (`SMOKE_MODEL` for `install.sh`).

## `deploy/installer.yaml` (one-click)

A static CloudFormation template, no assets to upload, that installs the
demo configuration from the console. The full description of its resources
and IAM policy is in [deploy/README.md](../deploy/README.md); this is the
operator's view.

Open it with a Launch Stack URL (replace `<region>`; `<ref>` is a branch or
tag):

```text
https://console.aws.amazon.com/cloudformation/home?region=<region>#/stacks/create/review?templateURL=https://raw.githubusercontent.com/aws-samples/sample-bedrock-spend-controls/<ref>/deploy/installer.yaml&stackName=bedrock-spend-controls-installer
```

or create it with the CLI (`--capabilities CAPABILITY_IAM`). The sample is
deployed in the Region the installer stack is created in.

### Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `AlertEmail` | required | SNS quota alerts; confirm the subscription AWS Notifications sends |
| `AdminEmail` | empty = `AlertEmail` | Console administrator `quota-admin`; Cognito emails the temporary password there |
| `AcknowledgeLoggingOverwrite` | required, only `yes` | The demo enables Bedrock model-invocation logging for the whole Region and overwrites any existing configuration; the preflight refuses to continue without this acknowledgement |
| `GitRef` | `main` | Branch or tag to clone (`git clone --depth 1 --branch`). Pin a release tag for a reproducible install |
| `RerunToken` | empty | Change it (any text) on a stack update to run the installer again |
| `ComputeType` | `BUILD_GENERAL1_SMALL` | `MEDIUM` shortens the console and CDK builds by a few minutes |
| `ExistingBuildRoleArn` | empty = create `BuildRole` | Use your own CodeBuild service role instead |

### Start and return

The stack creates a CodeBuild project (image
`aws/codebuild/amazonlinux-x86_64-standard:5.0`, Node.js 20, Python 3.12,
60-minute timeout, one build at a time) and a custom resource whose Lambda
function only **starts** one build and returns its id. A Lambda function may
run for 15 minutes while the install takes 15 to 25, so the stack reaches
`CREATE_COMPLETE` within about a minute and the install's result is the
build's status, not the stack's. The build runs, from the clone,
`./install.sh --yes --config demo --region $AWS_REGION --alert-email ...
--admin-email ... --acknowledge-logging-overwrite`.

Watch the build with the `WatchCommand` output:

```bash
aws codebuild batch-get-builds --region <region> --ids <BuildId output> \
  --query 'builds[0].buildStatus' --output text   # IN_PROGRESS, SUCCEEDED, FAILED
```

`BuildUrl` opens the build in the console and `BuildLogsUrl` the CloudWatch
Logs stream with the full `install.sh` output (30-day retention). On
`SUCCEEDED`, open the `AdminUiUrl` output of stack `BedrockSpendControls`
in the same Region and sign in as `quota-admin` with the emailed temporary
password. On `FAILED`, the failing phase is named in the summary table at
the end of the log.

### IAM footprint

`BuildRole` is broad by nature, because the build may run `cdk bootstrap`
and must assume the CDK bootstrap roles that `cdk deploy` uses. Its
statements are scoped where AWS allows it: `sts:AssumeRole` on
`role/cdk-hnb659fds-*`; `cloudformation:*` on the `CDKToolkit` stack,
`iam:*`, `s3:*`, `ecr:*`, and the SSM parameter actions on the
`cdk-hnb659fds-*` bootstrap names; read-only stack and preflight actions;
and, for the smoke test, `secretsmanager:GetSecretValue` on
`BedrockSpendControls*` secrets, the three Cognito admin-user actions on the
account's user pools, and `lambda:InvokeFunctionUrl` on
`BedrockSpendControls-*` functions. The CloudFormation execution role from
the bootstrap does the actual deploy. The role trusts
`codebuild.amazonaws.com` only for projects in this account. The statement
list is in [deploy/README.md](../deploy/README.md#resources-and-iam-footprint);
the threat model records the role as an accepted, single-use risk
([threat-model.md](threat-model.md), T-32).

Accounts whose bootstrap uses a customer managed KMS key or a non-default
qualifier, or whose policy forbids a template-created role, pass their own
role as `ExistingBuildRoleArn`. It must trust `codebuild.amazonaws.com`,
hold the permissions above, and be allowed to write the installer's log
group.

### Re-running and deleting

A stack update that changes `GitRef`, `AlertEmail`, `AdminEmail`, or
`RerunToken` starts a new build; `install.sh` then shows `cdk diff` and
deploys the change. The project allows one build at a time, so an update
while a build is still running fails with a clear error instead of running
two installs at once. Once the build has succeeded, delete the installer
stack:

```bash
aws cloudformation delete-stack --region <region> --stack-name bedrock-spend-controls-installer
```

This removes the project, the build role, the Lambda function, and both log
groups (export the build log first if you want to keep it). The deployed
`BedrockSpendControls` stack is not touched; remove it with
[`install.sh --destroy`](#--destroy) or `npx cdk destroy`.

## `setup.py` (configuration wizard)

```text
python setup.py [--profile-template demo|production]
    [--output cdk/config/<name>.local.json] [--profile AWS_PROFILE] [--region REGION]
    [--answers FILE] [--save-answers FILE] [--yes] [--deploy] [--no-live-checks]
```

Run it from the repository root with the CDK Python environment active
(`cdk/.venv`, or `python3 -m venv .venv && .venv/bin/pip install -r
requirements-dev.txt`): the wizard imports `cdk/stacks/configuration.py`,
and the questions, their help texts, and their validation come from the
same `KEY_DOCS` that `cdk synth` and [configuration.md](configuration.md)
use. Every answer is checked by validating the complete file-to-be, so the
wizard rejects exactly what `cdk synth` would, with the same message, and
asks again.

Exit status: 0 when the deployment file was written (and, with `--deploy`,
the deploy succeeded or was declined); 1 when the values were rejected, an
answer was missing in a `--yes` run, the run was aborted, or the deploy
failed; 2 for a usage error (bad arguments, an unreadable or unknown-key
`--answers` file, a missing CDK Python environment).

### Conventions

- **Enter** keeps the value shown in brackets; a lone **`-`** clears a text
  or list value; **Ctrl-C** (or end of input) aborts without writing.
- **Placeholders must be replaced.** The shipped templates carry example
  values (`example.com` addresses, the `111122223333` documentation
  account). The wizard never keeps them silently: such a key is shown with
  `(example: ...)` and requires a real answer, and a file that still holds
  one is refused at the end ("these keys still hold the template's example
  values").
- **Sections.** Eleven, in this order: identity provider, admin console,
  Bedrock invocation logging, allowed models, broker access, default
  limits, enforcement, retention, alerts, workloads, operations. Each
  section asks its basic keys and then offers "Configure advanced settings
  for <section>?" for the rest. Keys that only make sense with another
  answer (`jwt_audience`, `jwt_jwks_url`, `admin_ui_client_id`,
  `admin_ui_connect_origins`, `admin_email`, `invocation_log_group_name`)
  are asked only then and reset to their default otherwise.
- **Default limits** are edited as `usd`, `input_tokens`, `output_tokens`
  per period (`0` means unlimited) and thresholds as
  `50:warn,80:warn,100:block` pairs; **workloads** are added one by one
  (name, model from the menu or by ID, optional `role_arn`) and written to
  `workloads.json` next to the deployment file, which references it by
  relative path.

### Output file and edit mode

The default output is `cdk/config/<template>.local.json`, a name Git
ignores. When the output file already exists, the wizard runs in **edit
mode**: the file's values become the defaults shown in brackets, and the
summary marks what changed. The written file contains every key the
template sets plus every value that differs from the code defaults, in
sorted order; a `model_config` or `workloads` path that is not next to the
output file is stored as an absolute path so the wizard and `cdk synth`
resolve the same file.

### Answers file

`--answers FILE` is a JSON object of `key: value` pairs using the
deployment keys. Without `--yes` they are the defaults shown; with `--yes`
they are the answers and nothing is asked. Strings are accepted for typed
keys (`"true"`, `"35"`, `"a,b"` for lists); objects (`default_limits`,
`workloads` as `{"workloads": [...]}`) are given as JSON. Unknown keys exit
with status 2. A `--yes` run that reaches a placeholder with no answer exits
with status 1 and names the key. `--save-answers FILE` writes the answers
given in this run for a later `--answers FILE --yes` replay, for example in
a second account.

### Live checks

With usable credentials (`--profile`, or the `AWS_*` variables) and a
Region, the wizard checks answers against the account as it goes, and says
`Live checks: off` otherwise (or with `--no-live-checks`). It runs
`region_support` at the start; `oidc_issuer` after `jwt_issuer`;
`invocation_logging` after the logging keys, having first read the Region's
current configuration and proposed `manage_invocation_logging: false` with
the existing log group when another destination is configured (choosing
`true` there asks you to accept the overwrite, which becomes
`--acknowledge-logging-overwrite` for `--deploy`); `bedrock_model_access`
after `allowed_model_arns`; `invoker_principals` after
`invoker_principal_arns`; and `lambda_concurrency` before
`reserve_enforcement_concurrency`, suggesting `false` when the account
cannot spare the reserved executions. For `allowed_model_arns` and workload
models it shows a numbered menu of the foundation models and cross-Region
inference profiles visible in the Region; picking a profile adds the ARNs
of the foundation models it routes to.

The live checks need the preflight actions listed
[above](#toolspreflight) plus `bedrock:ListFoundationModels`,
`bedrock:ListInferenceProfiles`, `bedrock:GetInferenceProfile`,
`bedrock:GetModelInvocationLoggingConfiguration`, and
`sts:GetCallerIdentity`. All are read-only. A denied call is reported and
the question falls back to typing values by hand.

### Summary and `--deploy`

After the last question the wizard writes the file and prints a summary:
the values (marking changes against the template), the monthly cost
estimate from `tools/estimate_cost.py`, and the tasks for other teams
derived from the answers: the console redirect URI to register at the IdP,
the claims tokens must carry, the bypass-prevention SCP from DEPLOYMENT.md
with the workload roles already listed, models to enable, and the invoking
roles to grant `lambda:InvokeFunctionUrl`.

With `--deploy` it then runs the full preflight on the written file, prints
the `install.sh --config <file> [--profile] [--region]
[--acknowledge-logging-overwrite] [--yes]` command it is about to run, and
runs it after a confirmation (`--yes` confirms). A failed preflight stops
with status 1 before anything is deployed. When `install.sh` is not
available, the equivalent manual commands are printed instead.

## Troubleshooting

Failures seen while testing the installers, and what to do about each.

1. **Opt-in Region not enabled.** The preflight `credentials` check fails
   with `account <id> has not enabled Region <region> (an opt-in Region):
   its endpoints reject every request`, and the Region-bound checks are
   skipped in that run. Enable it (`aws account enable-region --region-name
   <region>`, then wait a few minutes) or deploy in a Region the account
   already has.
2. **Missing or old CDK bootstrap.** `bootstrap` fails with `stack CDKToolkit
   not found` (or a version below 21). `install.sh` and the one-click
   installer handle this: the verdict stays `OK (bootstrap: handled by the
   next phases)` and phase 3 runs `cdk bootstrap`. By hand: `npx cdk
   bootstrap aws://<account>/<region>` from `cdk/`.
3. **A catalog model the Region does not sell.** The deploy no longer fails
   at `BedrockModelPriceSnapshot`: the model is skipped, the
   `ModelPriceSnapshot` output ends with `unresolved=<n>`, the resolver logs
   one warning per model, and the price parameter lists them under
   `unresolved`. Invocations of such a model are priced at the conservative
   fallback and counted in `FallbackPricedRequests`, so the
   `pricing_fallback` alarm fires if anyone calls it
   ([pricing.md](pricing.md#models-the-region-does-not-price)). Pin the
   model in `price_overrides` or keep it out of `allowed_model_arns`.
4. **`ROLLBACK_COMPLETE` after a failed first create.** CloudFormation
   cannot update a stack whose first create failed, and the stack retains
   its `/bedrock/spend-controls/model-invocations` log group on rollback, so
   the next create fails with "already exists". `install.sh` detects both
   in the deploy phase and, after asking, deletes the failed stack and the
   orphaned log group. By hand: `aws cloudformation delete-stack
   --stack-name BedrockSpendControls`, wait for the deletion, then `aws logs
   delete-log-group --log-group-name /bedrock/spend-controls/model-invocations`
   (or keep the group and pass it back with `manage_invocation_logging:
   false` and `invocation_log_group_name`).
5. **Two installs at once.** A second `cdk` process on the same checkout
   fails with `Other CLIs (PID ...) are currently reading from cdk.out`. Let
   the first finish. The one-click project allows one build at a time, so a
   stack update during a running build fails instead of starting a second.
6. **Node.js missing or too old.** `install.sh` stops before the preflight:
   `Node.js 20 or newer is required`. It installs nothing itself. In
   CloudShell or Amazon Linux 2023: `curl -o-
   https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash`,
   then `. ~/.nvm/nvm.sh && nvm install 22 && nvm use 22`; on macOS `brew
   install node@22`. Python 3.12 is needed too: `sudo dnf install -y
   python3.12` or `brew install python@3.12`.
7. **Lambda concurrency quota of 10.** Some new or sandbox accounts cannot
   spare the five reserved executions; `lambda_concurrency` fails and the
   deploy would stop with an unreserved-concurrency error. Set
   `reserve_enforcement_concurrency: false` (the wizard suggests it when it
   sees the quota) or request a quota increase first.
8. **Invocation logging already configured by someone else.**
   `invocation_logging` fails: `manage_invocation_logging=true makes the
   deploy OVERWRITE this account-wide setting`. `--acknowledge-logging-overwrite`
   (or `AcknowledgeLoggingOverwrite=yes`) turns it into a warning and the
   deploy overwrites the configuration, which is not restored on destroy.
   In a shared account prefer `manage_invocation_logging: false` with
   `invocation_log_group_name` set to the group that already receives
   invocation logs, which the wizard proposes when it finds one
   ([configuration.md](configuration.md#invocation-logging-ownership)).
