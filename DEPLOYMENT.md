# Deployment guide

How to deploy Bedrock Spend Controls in a personal account (demo) and in a
shared or production account. Reference material lives in `docs/`:
[configuration keys](docs/configuration.md), [quotas](docs/quotas.md),
[pricing](docs/pricing.md), [operations](docs/operations.md),
[admin API](docs/admin-api.md), and the
[application integration guide](docs/integration.md).

## Decisions before deployment

| Decision | Demo / personal account (`config/demo.json`) | Production / shared account (`config/production.json`) |
|---|---|---|
| Quota identity | JWT `sub` | Stable user, tenant, team, or project claim (`jwt_user_claim`) |
| IdP | Stack-created Cognito user pool | Your OIDC issuer |
| Enforcement | Lease + revocation + emergency stop, always on | Same layers |
| Permission lease default | 60 s (runtime dial) | 300 s; tune per incident with `PUT /admin/enforcement` |
| STS credential lifetime | 3600 s | 900 s (900 to 3600; the broker role-chains, so sessions above one hour are rejected) |
| Refresh overlap / jitter | 40 s / 5 s | 10 s / 5 s |
| Runtime models | `*` for exploration | Exact model and inference-profile ARNs; exclude models used through unmetered APIs |
| Invocation logging | Stack managed (`manage_invocation_logging: true`) | Reuse the centrally managed log group (`false` + `invocation_log_group_name`) |
| Auto-provisioning | Enabled | Disabled |
| Function URL callers | Account default | Explicit backend and admin role ARNs in `invoker_principal_arns` |
| Usage retention | 35 days | 90 days (policy-defined, at least 31) |
| DynamoDB on delete | `DESTROY` | `RETAIN` |
| Alerts | Personal email | Operations topic or distribution list |
| Admin console | Hosted, Cognito group `quota-admins` | `admin_ui: true` with `admin_ui_client_id` against your IdP, or not hosted |
| Direct Bedrock access | Demo deny policy optional | SCP, permissions boundary, or `DenyDirectBedrockPolicyArn` required |
| Price fallback | Shipped default | Reviewed against the most expensive allowed model |

The architectural decision to accept is the enforcement guarantee: the
sample does not inspect inference requests. Every credential carries an
immutable permission-lease deadline, the revocation layer cuts blocked
identities' sessions after IAM propagation, and already-authorized streams
may finish. Overspend is bounded by `metering lag + min(lease remainder,
deny propagation)`; measure it in your account before relying on a number
([qualification/QUALIFICATION.md](qualification/QUALIFICATION.md)).

## Request flow

1. The application obtains a JWT from Cognito or your IdP.
2. An allowed AWS principal SigV4-signs `POST /v1/credentials` to the
   broker's `AWS_IAM` Function URL with the JWT in `X-Quota-User-Token`.
3. The broker verifies issuer, audience, signature, expiry, and the identity
   claim.
4. DynamoDB provides status, limits, and current usage for every enabled
   period.
5. The broker reserves one logical lease, assumes `BedrockUserRole` with the
   subject as `SourceIdentity`, and embeds the permission-lease deadline
   (60, 300, or 900 s) in the session policy. STS keys last
   `vended_ttl_seconds`; Bedrock permission ends at the deadline.
6. The application's credential provider caches the keys for all Runtime
   calls and renews once per lease, not once per inference.
7. The application calls `bedrock-runtime` directly.
8. Bedrock writes model invocation logs to CloudWatch Logs; a subscription
   invokes the usage processor.
9. The processor deduplicates by `requestId`, prices every dimension,
   updates the daily and per-model ledgers, emits metrics, warns, and blocks.
10. A blocked subject cannot obtain another lease; the revocation layer
    denies its in-flight sessions after IAM propagation.

## Demo / personal account

### 1. Prerequisites

- AWS CLI with an authorized profile and Bedrock model access in the Region.
- Node.js and npm.
- Python 3.12 or later with `pip3` (the project virtualenv's is preferred).
  No container runtime is required; Docker or Finch is used only as a
  fallback if host bundling fails (`CDK_DOCKER=finch` for Finch).

```bash
git clone https://github.com/aws-samples/sample-bedrock-spend-controls.git
cd sample-bedrock-spend-controls

export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export ALERT_EMAIL=you@example.com

aws sso login --profile "$AWS_PROFILE"   # omit for non-SSO credentials
aws sts get-caller-identity
```

### 2. Build dependencies

```bash
cd admin-ui
npm ci
npm run build

cd ../cdk
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
npm ci
```

### 3. Bootstrap, synthesize, and deploy

```bash
npx cdk bootstrap

npx cdk synth \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"

npx cdk deploy \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"
```

`manage_invocation_logging: true` overwrites the Region's model invocation
logging configuration ([ownership](docs/configuration.md#invocation-logging-ownership)).
Confirm the SNS subscription from the email AWS sends; until then warnings
and block notifications are not delivered.

### 4. Read outputs

```bash
export STACK_NAME=BedrockSpendControls

export BROKER_API_URL=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='BrokerApiUrl'].OutputValue | [0]" --output text)
# The Function URL output ends with "/"; strip it so "$BROKER_API_URL/admin/..."
# does not become "//admin/..." (the broker answers 404 to the double slash).
export BROKER_API_URL=${BROKER_API_URL%/}

export ADMIN_KEY=$(aws secretsmanager get-secret-value \
  --secret-id "$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='AdminKeySecretArn'].OutputValue | [0]" --output text)" \
  --query SecretString --output text)
```

Other outputs you will use: `AdminUiUrl`, `DemoUserPoolId`,
`DemoUserPoolClientId`, `AdminIdentityPoolId`, `BedrockUserRoleArn`,
`DenyDirectBedrockPolicyArn`, `EmergencyKeySecretArn`, `AlertTopicArn`,
`UsersTableName`, `UsageTableName`, `BrokerApiRoleArn`.

### 5. Configure the demo administrator

`config/demo.json` hosts the console, creates the Cognito group
`quota-admins`, and authorizes that group (`admin_jwt_claim:
cognito:groups`). Create an administrator and add it to the group:

```bash
export USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='DemoUserPoolId'].OutputValue | [0]" --output text)

aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --username quota-admin \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" --username quota-admin \
  --password 'Demo-only-Change-Me-42!' --permanent
aws cognito-idp admin-add-user-to-group --user-pool-id "$USER_POOL_ID" --username quota-admin \
  --group-name quota-admins
```

Open the `AdminUiUrl` output and sign in as `quota-admin`. The browser runs
an authorization-code + PKCE flow against Cognito managed login and exchanges
the ID token at the Identity Pool for SigV4 credentials. The deployment
writes `config.js` with public identifiers only (broker URL, Region, issuer,
client ID, Identity Pool ID, scopes); it contains no secrets.

### 6. Administrative smoke test

```bash
cd ..   # back to the repository root

cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  create-user demo-user \
  --daily-usd 2 --daily-input-tokens 1000000 --daily-output-tokens 200000 \
  --weekly-usd 10 --weekly-input-tokens 5000000 --weekly-output-tokens 1000000

cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  list-users
```

Rerunning the same create returns `409 user_already_exists` and changes
nothing. In the console, check the Overview, Users, Operations, and Audit
tabs, the per-model usage charts, the live-lease timeline, the Unlimited
confirmation and required reasons in the limits editor, and the detail
drawer's Usage and Changes views. In a dedicated sandbox, also exercise a
lease-dial change and one complete emergency activate/recover cycle with the
break-glass key ([operations.md](docs/operations.md#emergency-stop)).

### 7. Runtime smoke test

The runtime path needs a JWT for a **quota user**, which is a different
Cognito user from `quota-admin` (the administrator authorizes console
access; the quota user is the subject being metered). Create one and obtain
an ID token from the secretless app client:

```bash
aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --username quota-user \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" --username quota-user \
  --password 'Demo-only-Change-Me-43!' --permanent

export CLIENT_ID=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='DemoUserPoolClientId'].OutputValue | [0]" --output text)
export USER_JWT=$(aws cognito-idp initiate-auth --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=quota-user,PASSWORD='Demo-only-Change-Me-43!' \
  --query AuthenticationResult.IdToken --output text)
```

With `auto_provision_users: true` the first vend creates the user's row with
`default_limits`. Run the path an application would use: the credential
provider from `examples/refreshable_bedrock.py` vends credentials from the
broker and hands them to an ordinary boto3 client.

```bash
export GATEWAY_URL="$BROKER_API_URL"

cdk/.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "examples")
from refreshable_bedrock import QuotaBrokerCredentialProvider

provider = QuotaBrokerCredentialProvider(
    os.environ["GATEWAY_URL"], os.environ["USER_JWT"],
    region=os.environ.get("AWS_REGION", "us-east-1"),
)
runtime = provider.bedrock_client()
response = runtime.converse(
    modelId="openai.gpt-oss-20b-1:0",
    messages=[{"role": "user", "content": [{"text": "Reply with exactly: runtime quota smoke"}]}],
)
print(response["output"]["message"]["content"][0]["text"])
print("usage:", response["usage"])
PY
```

The vend happens on the first signed request; the inference goes directly to
`bedrock-runtime`. A blocked or over-budget user fails here with a
`BrokerCredentialError` instead of reaching Bedrock. Allow one to two
minutes for invocation-log delivery, then read the ledger. `USER_ID` is the
quota user's `sub`:

```bash
export USER_ID=$(aws cognito-idp admin-get-user --user-pool-id "$USER_POOL_ID" --username quota-user \
  --query "UserAttributes[?Name=='sub'].Value | [0]" --output text)

cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  get-usage "$USER_ID" --period daily
```

The row should show one request with the token counts the `Converse`
response reported. To exercise enforcement end to end: set the user's daily
token limits to current usage plus one (`update-user`), repeat the call and
confirm the vend is refused with `429 quota_exceeded`, then raise the limits
and confirm vending recovers. Each step appears in the console's audit views.

The pattern for a backend serving many users (`BedrockSpendControls`, one
client per user) is in [docs/integration.md](docs/integration.md).

## Production / shared account

### 1. Create a private deployment file

`config/production.json` is the template. Keep the private copy next to it
so the relative `model_config` path stays valid; `config/*.local.json` is
ignored by Git.

```bash
cd sample-bedrock-spend-controls/cdk
cp config/production.json config/production.local.json
```

The template:

```json
{
  "alert_email": "platform-alerts@example.com",
  "jwt_issuer": "https://idp.example.com",
  "jwt_audience": "bedrock-spend-controls",
  "jwt_user_claim": "custom:tenant_id",
  "admin_ui": false,
  "admin_jwt_claim": "",
  "admin_jwt_value": "",
  "auto_provision_users": false,
  "default_limits": {
    "daily": {
      "usd": 25.0,
      "input_tokens": 10000000,
      "output_tokens": 2000000
    },
    "weekly": null,
    "monthly": null
  },
  "warn_threshold": 0.75,
  "usage_retention_days": 90,
  "retain_tables_on_delete": true,
  "vended_ttl_seconds": 900,
  "permission_lease_seconds": 300,
  "refresh_overlap_seconds": 10,
  "refresh_jitter_seconds": 5,
  "vend_rate_limit_per_minute": 6,
  "revocation_policy_shards": 19,
  "revocation_reconcile_minutes": 5,
  "manage_invocation_logging": false,
  "invocation_log_group_name": "/aws/bedrock/modelinvocations",
  "invoker_principal_arns": [
    "arn:aws:iam::111122223333:role/BedrockSpendControlsInvoker"
  ],
  "allowed_model_arns": [
    "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7"
  ],
  "model_config": "model-pricing.json"
}
```

Edit in `config/production.local.json`:

| Key | Set to |
|---|---|
| `alert_email` | Your operations distribution list |
| `jwt_issuer`, `jwt_audience`, `jwt_user_claim` | Your IdP's issuer URL, the audience of the tokens your backend holds, and the claim that identifies the quota subject |
| `invocation_log_group_name` | The log group that already receives Bedrock invocation logs in this Region |
| `invoker_principal_arns` | The exact backend and admin role ARNs that may call the Function URL |
| `allowed_model_arns` | The exact foundation-model and inference-profile ARNs your users may invoke, in your account and Region |
| `default_limits`, `warn_threshold`, `usage_retention_days` | Your quota policy ([quotas.md](docs/quotas.md)) |
| `admin_ui`, `admin_ui_client_id`, `admin_jwt_claim`, `admin_jwt_value` | Only if you host the console against your IdP (step 4) |
| `reconciliation_enabled`, `workloads` | Optional; see [configuration.md](docs/configuration.md#reconciliation) and [Per-workload quotas](#per-workload-quotas) |

Every key, its default, and its validation rule:
[docs/configuration.md](docs/configuration.md). Review `fallback_price` in
`config/model-pricing.json` against the most expensive model you allow
([pricing.md](docs/pricing.md)).

### 2. Validate logging before deployment

```bash
aws bedrock get-model-invocation-logging-configuration

aws logs describe-subscription-filters \
  --log-group-name /aws/bedrock/modelinvocations
```

Confirm that the log group receives Runtime invocation logs, that it has
subscription-filter capacity left (five per log group, not adjustable; this
stack adds one, or two with workloads), and that the central logging owner
agrees to this stack adding its filters.

### 3. Build, bootstrap, synthesize, and deploy

```bash
cd sample-bedrock-spend-controls/admin-ui
npm ci && npm run build          # only when admin_ui is true

cd ../cdk
export AWS_PROFILE=your-production-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export DEPLOYMENT_CONFIG=config/production.local.json

aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
npm ci

npx cdk bootstrap
npx cdk synth  -c deployment_config="$DEPLOYMENT_CONFIG"
npx cdk deploy -c deployment_config="$DEPLOYMENT_CONFIG"
```

Confirm the SNS subscription and record the CloudFormation outputs in your
deployment system. The revocation layer attaches 20 managed policies to the
vended role (19 shards plus the emergency policy). That equals the IAM
default quota of 20 managed policies per role (adjustable up to 25), so the
role has no free attachment slot: check the applied value of *Managed
policies per role* in Service Quotas beforehand, and do not attach other
managed policies to `BedrockUserRole`.

### 4. Corporate IdP and console

The broker and the console federate with any OIDC-compliant issuer. The
console resolves endpoints from `/.well-known/openid-configuration`, signs
in with authorization-code + PKCE against a public (no-secret) client, and
exchanges the ID token at a Cognito Identity Pool for the SigV4 credentials
it signs Function URL requests with.

Set `admin_ui: true` together with your issuer, and the stack hosts the
console and creates the IAM OIDC provider and Identity Pool that trust it:

```json
{
  "jwt_issuer": "https://idp.example.com",
  "jwt_audience": "bedrock-spend-controls",
  "admin_ui": true,
  "admin_ui_client_id": "spa-public-client-id",
  "admin_jwt_claim": "groups",
  "admin_jwt_value": "bedrock-quota-admins"
}
```

- `admin_ui_client_id` is the SPA's public OAuth client in your IdP; omit it
  when the console shares the `jwt_audience` client. The broker accepts both
  audiences.
- After the first deploy, register the `AdminUiCallbackUrl` output as the
  redirect URI on that client. The CloudFront URL exists only after
  deployment, so this is a second step.
- The client must allow browser (CORS) calls to the token endpoint. In
  Microsoft Entra ID register the redirect URI under the *Single-page
  application* platform; in Okta or Auth0 use a public SPA client. If the
  IdP needs a scope for refresh tokens (Entra: `offline_access`) and it is
  not granted, the console re-authenticates silently on expiry.
- If the token endpoint is on a different origin than the issuer, list that
  origin in `admin_ui_connect_origins` so the console's
  Content-Security-Policy allows it.
- Sign-out uses the discovered `end_session_endpoint`.

To host the console elsewhere, build `admin-ui/`, serve it with a
`config.js` like the one the stack writes, and allow that exact HTTPS origin
in the Function URL CORS policy (the stack configures CORS only for its own
CloudFront distribution; never use a wildcard origin):

```javascript
window.QUOTA_ADMIN_CONFIG = {
  gatewayUrl: "BROKER_API_URL",
  region: "us-east-1",
  issuer: "https://idp.example.com",
  clientId: "spa-public-client-id",
  identityPoolId: "IDENTITY_POOL_ID",
  scopes: "openid email profile"
};
```

`config.js` holds public identifiers only. The shared admin key is for
trusted CLI or backend use and must never reach a browser; browser
administrators are authorized by their JWT claim.

### 5. Prevent bypass

The quotas have no effect if application users retain another principal
that can call Bedrock directly. Choose one:

- Attach the stack's `DenyDirectBedrockPolicyArn` to every non-vended role.
- Apply a permissions boundary.
- Apply an organizational SCP.

Example SCP with the deployed `BedrockUserRoleArn` as the only inference
exception:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RequireQuotaBrokerForBedrockRuntime",
      "Effect": "Deny",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:CallWithBearerToken",
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:CallWithBearerToken"
      ],
      "Resource": "*",
      "Condition": {
        "ArnNotEquals": {
          "aws:PrincipalArn": "BEDROCK_USER_ROLE_ARN"
        }
      }
    }
  ]
}
```

The two `bedrock-mantle:*` actions are included because the Bedrock Mantle
endpoint is not captured by model-invocation logging: any principal that can
call it spends outside the ledger. The vended role never receives those
actions, so the exception does not reopen the gap. If you use workloads,
add their role ARNs to the exception as well. Validate any SCP in a
non-production OU first.

### 6. Production acceptance tests

Verify each of the following before go-live:

1. An unsigned broker request returns `401` or `403`.
2. An allowed invoker role with a valid JWT receives STS credentials.
3. An invalid issuer, audience, signature, expiry, or missing claim is
   rejected with `401`.
4. Vended credentials can invoke only the resources in `allowed_model_arns`.
5. A normal application role cannot invoke Bedrock directly.
6. An invocation appears in the ledger through the log subscription.
7. Re-delivering the same `requestId` does not increment usage twice.
8. Warning and block SNS notifications arrive.
9. A blocked identity cannot renew its lease (`403 quota_blocked`).
10. Bedrock authorization fails after the permission-lease deadline even
    though `sts_expiration` is later.
11. `DetectionLagMilliseconds` is reported on the Operations tab.
12. A blocked identity's in-flight session is denied after IAM propagation;
    `RevocationSyncSuccess` is emitted and `revoked_identities_desired`
    reflects it.
13. Emergency activation closes vending (`503 emergency_stop`) before the
    role-wide deny applies; recovery removes the deny before vending reopens.
14. An unknown model is priced at the fallback and raises `pricing_fallback`.
15. Deleting a test stack with `retain_tables_on_delete: true` retains the
    tables.
16. Duplicate create returns `409 user_already_exists`; a stale `If-Match`
    returns `409 version_conflict`; a reused `Idempotency-Key` with a
    different body returns `409 idempotency_conflict`; an exact replay
    returns the original result.
17. Successful routine writes return the complete user and a new `ETag`, and
    appear in the per-user and global audit with the supplied reason.

## Per-workload quotas

A workload is an application that calls `bedrock-runtime` with its own IAM
role: no JWT, no vend, no client library. It is attributed by the
application inference profile it invokes and enforced by an inline deny on
its role. The developer's view is in
[docs/integration.md](docs/integration.md#per-workload-integration); these
are the operator steps.

1. **Describe the roster.** Copy `cdk/config/workloads.example.json` to
   `cdk/config/workloads.json` (ignored by Git):

   ```json
   {
     "workloads": [
       {
         "name": "payments-batch",
         "model": "us.anthropic.claude-opus-4-7",
         "role_arn": "arn:aws:iam::111122223333:role/payments-batch-app"
       },
       { "name": "reports-generator", "model": "anthropic.claude-haiku-4-5-20251001-v1:0" }
     ]
   }
   ```

   `name` must match `[a-z0-9][a-z0-9-]{0,47}`; `model` is a foundation-model
   ID or cross-Region inference-profile ID (never an ARN); `role_arn` is a
   same-account IAM role and is strongly recommended.

2. **Deploy** with `-c workloads=config/workloads.json` (or the `workloads`
   key in the deployment file). Per workload the stack creates an
   application inference profile `spend-controls-<name>` tagged
   `bedrock-spend-controls-workload=<name>`, and, with `role_arn`, attaches
   an invoke policy to the role that allows the profile plus the routed
   models conditioned on `bedrock:InferenceProfileArn`, so the role cannot
   invoke anything except through its profile. The enforcer's IAM
   permissions are scoped to exactly the enrolled role ARNs. The roster is
   written to the `WorkloadRosterParameter` SSM parameter, which the broker
   reads with a five-minute cache.

3. **Hand over the output.** Give the application team the
   `WorkloadProfileArn<Name>` output; they use it as `modelId`.

4. **Without `role_arn`** the invoke policy is emitted as the
   `WorkloadPolicySnippet<Name>` output for the role's owner to attach. The
   workload is metered and alerted but cannot be blocked: it reports
   `enforcement_ready: false` and shows as *Metering only* in the console,
   and a block on it raises `WorkloadEnforcementSkipped` plus an SNS message.
   Add `role_arn` and redeploy to promote it.

5. **Operate** with the standard routes and `user_id=workload:<name>`. The
   row appears after the workload's first metered invocation
   (auto-provisioned with `default_limits`, priced by the profile's
   underlying model); until then the console shows *Awaiting traffic*.
   `GET /admin/workloads` returns the roster joined with the metered rows.
   The workload enforcer attaches the deny within metering lag plus IAM
   propagation of a block, and removes it once every enabled period is under
   quota again ([runbook](docs/runbooks/components/workload-enforcer.md)).

Scale envelope: 1,000 application inference profiles and 1,000 IAM roles per
account (adjustable quotas). Reconciliation per workload needs the
cost-allocation tag activated in the payer account
([configuration.md](docs/configuration.md#reconciliation)).

## Clean up

```bash
cd sample-bedrock-spend-controls/cdk
npx cdk destroy -c deployment_config=config/demo.json
```

Production tables use `RETAIN` and survive stack deletion. Stack-managed
invocation logging resources are also retained because the stack cannot
restore a prior account-wide logging configuration. Review and remove
retained resources only through an explicit data-retention and
logging-owner decision.
