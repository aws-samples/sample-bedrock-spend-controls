# One-click installer (`deploy/installer.yaml`)

`installer.yaml` is a static CloudFormation template (no assets to upload)
that installs the Bedrock Spend Controls **demo** configuration from the AWS
console, without a terminal. It is the third way to deploy the sample, next
to `install.sh` (one command) and the manual steps in
[DEPLOYMENT.md](../DEPLOYMENT.md).

## What it does

The stack creates a CodeBuild project and starts one build. The build:

1. clones `https://github.com/aws-samples/sample-bedrock-spend-controls` at
   the `GitRef` branch or tag (`git clone --depth 1 --branch`), and
2. runs, from that checkout,
   `./install.sh --yes --config demo --region $AWS_REGION --alert-email ... --admin-email ... --acknowledge-logging-overwrite`,
   which is the same preflight, build, `cdk bootstrap`, `cdk synth`,
   `cdk deploy` and smoke-test sequence a person runs from CloudShell.

The build uses the `aws/codebuild/amazonlinux-x86_64-standard:5.0` image with
Node.js 20 and Python 3.12. No profile is passed: CodeBuild supplies the
service role's credentials, and the Region is the one the installer stack is
created in.

**The stack completes before the install does.** A CloudFormation custom
resource (a small Lambda function) only *starts* the build and returns its
id, because a Lambda function can run for 15 minutes at most while the
install takes 15–25. The stack therefore reaches `CREATE_COMPLETE` within a
minute; the result of the install is the build's status, not the stack's.
Watch it with the `WatchCommand` output or the `BuildUrl` output:

```bash
aws codebuild batch-get-builds --region <region> --ids <BuildId output> \
  --query 'builds[0].buildStatus' --output text   # IN_PROGRESS, SUCCEEDED, FAILED
```

When it prints `SUCCEEDED`, open the `AdminUiUrl` output of stack
`BedrockSpendControls` in the same Region, sign in as `quota-admin` with the
temporary password Cognito emailed to the administrator address, and confirm
the SNS subscription sent to the alert address. When it prints `FAILED`,
`BuildLogsUrl` has the full `install.sh` output; the failing phase is named
in its summary table.

## Launching it

Download this file and upload it in the CloudFormation console: *Create
stack* (`https://console.aws.amazon.com/cloudformation/home?region=<region>#/stacks/create/template`),
*Upload a template file*, then a stack name such as
`bedrock-spend-controls-installer` and the parameters below. CloudFormation
accepts templates only from Amazon S3 or as an uploaded file, so no
quick-create link can point at the repository itself. Or use the CLI:

```bash
aws cloudformation create-stack --region us-east-1 \
  --stack-name bedrock-spend-controls-installer \
  --template-body file://deploy/installer.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters ParameterKey=AlertEmail,ParameterValue=you@example.com \
               ParameterKey=AcknowledgeLoggingOverwrite,ParameterValue=yes
```

The template must be deployed in the Region where the sample should run
(see the `region_support` preflight check for the supported ones).

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `AlertEmail` | (required) | SNS quota alerts; AWS Notifications sends a subscription confirmation. |
| `AdminEmail` | empty = `AlertEmail` | Console administrator (`quota-admin`); the temporary password is emailed there. |
| `AcknowledgeLoggingOverwrite` | (required, only `yes`) | The demo turns on Bedrock model-invocation logging for the whole Region and overwrites any existing configuration; the preflight refuses to continue without this acknowledgement. |
| `GitRef` | `main` | Branch or tag to install. Pin a release tag for reproducible installs. |
| `RerunToken` | empty | Change it (any text) on a stack update to run the installer again. |
| `ComputeType` | `BUILD_GENERAL1_SMALL` | `MEDIUM` shortens the console and CDK builds by a few minutes. |
| `ExistingBuildRoleArn` | empty = create a role | Use your own CodeBuild service role instead of the one below. |

A stack update that changes `GitRef`, `AlertEmail`, `AdminEmail` or
`RerunToken` starts a new build with the new values; `install.sh` then shows
`cdk diff` and deploys the changes (`--yes`). The project allows one build at
a time (`ConcurrentBuildLimit: 1`): an update while a build is still running
fails with a clear error instead of running two installs at once.

## Resources and IAM footprint

| Resource | Purpose |
|---|---|
| `InstallerLogGroup` | `/aws/codebuild/<stack name>`, 30-day retention, the build output. |
| `BuildRole` (unless `ExistingBuildRoleArn`) | CodeBuild service role, described below. |
| `InstallerProject` | The CodeBuild project (inline buildspec, `NO_SOURCE`, 60-minute timeout). |
| `StartBuildLogGroup`, `StartBuildRole`, `StartBuildFunction` | The custom resource's Lambda function (Python 3.12, inline code); its role may only write its own logs and call `codebuild:StartBuild` / `BatchGetBuilds` on the project. |
| `RunInstaller` | `Custom::RunInstaller`: starts the build on create and update, does nothing on delete. |

`BuildRole` is **broad by nature**: the build may run `cdk bootstrap`
(creating the `CDKToolkit` stack, its `cdk-hnb659fds-*` IAM roles, assets
bucket, ECR repository and SSM parameter) and it assumes the CDK bootstrap
roles that `cdk deploy` uses to create a stack full of IAM roles, Lambda
functions, Cognito, CloudFront, DynamoDB, SQS, SNS and Bedrock logging
resources. Its policy is scoped to those names where AWS allows it:

- `sts:AssumeRole` on `role/cdk-hnb659fds-*` (the CDK deploy, file-publishing,
  image-publishing and lookup roles; the CloudFormation execution role does
  the actual deploy);
- for `cdk bootstrap`: `cloudformation:*` on stack `CDKToolkit`, `iam:*` on
  `role/cdk-hnb659fds-*` and `policy/cdk-hnb659fds-*`, `s3:*` on bucket
  `cdk-hnb659fds-assets-<account>-<region>`, `ecr:*` on repository
  `cdk-hnb659fds-container-assets-*`, and the parameter actions on
  `/cdk-bootstrap/hnb659fds/*`;
- read-only: `cloudformation:DescribeStacks/DescribeStackEvents/GetTemplate/ListStacks`,
  `sts:GetCallerIdentity` and the actions `tools/preflight` needs
  (`REQUIRED_ACTIONS` in `tools/preflight/checks.py`);
- for the smoke test that `install.sh` runs after the deploy:
  `secretsmanager:GetSecretValue` on the admin key secret (`AdminApiKey*`),
  `cognito-idp:AdminCreateUser/AdminSetUserPassword/AdminDeleteUser` on the
  account's user pools, and `lambda:InvokeFunctionUrl` (IAM auth) plus
  `lambda:InvokeFunction` (only when invoked via the Function URL) on
  functions named `BedrockSpendControls-*`;
- `logs:CreateLogStream` / `PutLogEvents` on the installer log group;
- to retry after a failed first create: `cloudformation:DeleteStack` on
  stack `BedrockSpendControls` only (a stack in `ROLLBACK_COMPLETE` cannot
  be updated) and `logs:DeleteLogGroup` on the retained
  `/bedrock/spend-controls/model-invocations` group only.

The role trusts `codebuild.amazonaws.com` only from this account's CodeBuild
projects (`aws:SourceAccount` / `aws:SourceArn`). Accounts whose existing CDK
bootstrap uses a customer managed KMS key or a non-default qualifier need a
role with the matching permissions: pass it as `ExistingBuildRoleArn` (it
must trust `codebuild.amazonaws.com` and hold the permissions above,
including the log group write).

## Deleting the installer stack

Once the build has succeeded the installer stack has no further role. Delete
it from the console or with:

```bash
aws cloudformation delete-stack --region <region> --stack-name bedrock-spend-controls-installer
```

This removes the CodeBuild project, the build role, the Lambda function and
both log groups (the build output is lost with them, so read or export the
logs first if you need them). The deployed `BedrockSpendControls` stack is
**not** touched. To remove the deployment itself, run
`./install.sh --destroy --region <region>` from a checkout (or
`npx cdk destroy` as in [DEPLOYMENT.md, "Clean up"](../DEPLOYMENT.md#clean-up));
both list the resources that are retained on purpose.

## Tests

`tests/test_installer_template.py` parses the template, checks the
parameters, the buildspec flags against `install.sh`, the role's bootstrap
scoping and the custom resource contract, exercises the Lambda handler with a
fake CodeBuild client, and runs `cfn-lint` when it is installed
(`requirements-dev.txt`).
