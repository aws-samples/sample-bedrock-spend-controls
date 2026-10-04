# Deployment configuration

The stack is configured by one JSON file passed as CDK context:

```bash
npx cdk synth -c deployment_config=config/demo.json
```

Direct `-c key=value` values override the file for scalar and list keys;
`default_limits` is an object and can only be set in the file (a `-c`
string fails validation with `default_limits must be an object`). Relative
paths (`deployment_config` itself, and `model_config` or a `workloads` file
path inside it) resolve from the deployment file's directory first, then
from the current working directory; a `model_config` or `workloads` path
passed as `-c` context resolves from `cdk/`. Unknown keys in the file fail
synthesis. Two reference files ship in `cdk/config/`: `demo.json` (personal
account, stack-created Cognito) and `production.json` (shared account, your
IdP). Private copies named `config/*.local.json` and `config/workloads.json`
are ignored by Git.

## Keys

| Key | Default | Validation and meaning |
|---|---:|---|
| `auto_provision_users` | `true` | Boolean; create a quota row with `default_limits` on the first valid JWT |
| `default_limits` | daily `$1`, 1,000,000 input and 200,000 output tokens; weekly/monthly `null` | Object with `daily`, `weekly`, `monthly`, optional `rate`. Enabled periods contain non-negative `usd`, `input_tokens`, `output_tokens` and an optional `thresholds` list (1 to 20 entries); `null` disables `weekly` or `monthly`, never `daily`. File only, not `-c`. See [Default limits](#default-limits) |
| `warn_threshold` | `0.8` | Greater than 0 and less than 1; the `warn` level of the default thresholds list applied to periods without their own list |
| `usage_retention_days` | `35` | At least 31; daily ledger retention. Monthly totals are derived from these rows ([quotas.md](quotas.md#how-weekly-and-monthly-totals-are-computed)) |
| `log_retention_days` | `90` | One of the CloudWatch Logs retention values (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, ...); retention of every Lambda's explicit log group |
| `retain_tables_on_delete` | `false` | `true` maps the DynamoDB tables to `RETAIN` and enables deletion protection; point-in-time recovery is always on |
| `reserve_enforcement_concurrency` | `true` | Pins the five enforcement workers at one reserved concurrent execution each. Set `false` only in accounts whose Lambda concurrency quota (10 in some new or sandbox accounts) cannot spare five slots ([DEPLOYMENT.md](../DEPLOYMENT.md#3-build-bootstrap-synthesize-and-deploy)) |
| `vended_ttl_seconds` | `900` | 900 to 3600; the broker Lambda uses role chaining, so STS rejects longer sessions |
| `permission_lease_seconds` | `300` | `60`, `300`, or `900`; the deployment **default** for the runtime lease dial ([Runtime lease dial](#runtime-lease-dial)) |
| `refresh_overlap_seconds` | `10` | Positive and less than `permission_lease_seconds`; how long before the lease deadline clients refresh |
| `refresh_jitter_seconds` | `5` | Non-negative and less than `refresh_overlap_seconds` |
| `vend_rate_limit_per_minute` | `6` | Positive; broker calls per identity per minute, retries included, summed across every backend process serving that identity. Set it to at least the number of replicas that can serve one identity concurrently ([integration.md](integration.md#multiple-instances-threads-async)) |
| `revocation_policy_shards` | `19` | Must be `19`; the shard layout is fixed because rehashing live identities would open an authorization gap |
| `revocation_reconcile_minutes` | `5` | Positive; period of the revocation processor's repair schedule |
| `allowed_model_arns` | `["*"]` | Non-empty list of Bedrock IAM resource ARNs (`arn:...:bedrock:...`, foundation models or inference profiles), or `*`; model IDs are rejected. `*` prints a synthesis warning. See [Model and inference-profile IAM](#model-and-inference-profile-iam) |
| `invoker_principal_arns` | `[]` | IAM principal ARNs allowed to call the Function URL: `arn:<partition>:iam::<account>:root`, `:role/...`, `:user/...`, or an STS assumed-role ARN, no wildcards. Empty means the account's default principals; production lists exact role ARNs |
| `manage_invocation_logging` | none | Explicit `true` or `false` required unless `invocation_log_group_name` is set. See [Invocation logging ownership](#invocation-logging-ownership) |
| `invocation_log_group_name` | empty | An existing log group that already receives invocation logs. Setting it implies `manage_invocation_logging: false`; combining it with `true` fails synthesis |
| `model_config` | `config/model-pricing.json` | Price catalog, overrides, and fallback ([pricing.md](pricing.md)) |
| `jwt_issuer` | empty | OIDC issuer URL; must start with `https://` and requires `jwt_audience`. Empty creates the demo Cognito user pool |
| `jwt_audience` | empty | Comma-separated audiences. The first entry is the data-plane audience and the only one accepted on `POST /v1/credentials`; later entries (the console client the stack appends) are accepted on `/admin/*` only. Required with `jwt_issuer` |
| `jwt_jwks_url` | discovery | Explicit `https://` JWKS URL when OIDC discovery is unavailable; requires `jwt_issuer` |
| `jwt_user_claim` | `sub` | Non-empty; the claim whose value is the quota subject |
| `admin_jwt_claim` | empty | Claim that authorizes browser administrators; set together with `admin_jwt_value` |
| `admin_jwt_value` | empty | Required value (or group) of `admin_jwt_claim` |
| `admin_ui` | `false` | Hosts the console on CloudFront; requires both admin JWT keys; works with demo Cognito or your issuer |
| `admin_ui_client_id` | empty | Your issuer only (`admin_ui: true` with `jwt_issuer`): the SPA's public OAuth client ID (defaults to `jwt_audience`) |
| `admin_ui_connect_origins` | `[]` | Your issuer only: extra `https://` origins without a trailing slash for the console's Content-Security-Policy (token endpoint on another origin) |
| `alert_email` | empty | Creates an SNS email subscription on the alerts topic |
| `snapstart` | `false` | Enable Lambda SnapStart for the broker |
| `adapter_layer_arn` | regional default (`LambdaAdapterLayerX86:30`) | Override the Lambda Web Adapter layer |
| `workloads` | empty | Workload roster: inline JSON or a file path. `name` matches `[a-z0-9][a-z0-9-]{0,47}` and is unique; `model` is an ID, not an ARN; `role_arn` (optional) is one concrete `arn:aws:iam::<account>:role/...` without wildcards, in the deploying account, and unique across workloads. Operator steps in [DEPLOYMENT.md](../DEPLOYMENT.md#per-workload-quotas) |
| `reconciliation_enabled` | `false` | Deploys the daily ledger-vs-Cost-Explorer comparison ([Reconciliation](#reconciliation)) |
| `reconcile_lag_days` | `2` | 1 to 14; which settled day (`today - lag`, UTC) each run compares |
| `reconciliation_alarm_percent` | `10` | Greater than 0 and at most 100; absolute delta that, over two consecutive daily runs, raises `reconciliation_delta` |
| `reconciliation_service_names` | `["Amazon Bedrock", "Amazon Bedrock Service"]` | Non-empty list of Cost Explorer `SERVICE` values summed as Bedrock spend |

## Default limits

`default_limits` applies to auto-provisioned subjects, to workload rows on
their first metered invocation, and to `POST /admin/users` bodies that omit
`limits`. Synthesis validates thresholds with the same rules the runtime
applies (see [quotas.md](quotas.md#thresholds-and-alert-only-budgets)).

```json
{
  "default_limits": {
    "daily": {
      "usd": 25,
      "input_tokens": 10000000,
      "output_tokens": 2000000,
      "thresholds": [
        {"at": 0.5, "action": "warn"},
        {"at": 0.8, "action": "warn"},
        {"at": 1.0, "action": "block"}
      ]
    },
    "weekly": {
      "usd": 100,
      "input_tokens": 0,
      "output_tokens": 0,
      "thresholds": [{"at": 1.0, "action": "warn"}]
    },
    "monthly": null,
    "rate": {"rpm": 60, "tpm": 100000}
  }
}
```

Omitting `thresholds` on a period yields `[{warn_threshold: warn}, {1.0:
block}]`; omitting `rate` (or setting a dimension to `0`) leaves rate
limiting off. `daily` must be an object (it cannot be `null`); `weekly` and
`monthly` may be `null`. A `thresholds` list has 1 to 20 entries whose `at`
values are finite, greater than 0, at most `10` (1000 %), and strictly
increasing, with at most one `block` entry, which must be last.

## Invocation logging ownership

Bedrock model invocation logging is an account- and Region-level setting.
There is one configuration per Region, so decide who owns it.

`manage_invocation_logging: true`:

- Creates a log group and a Bedrock writer role.
- **Overwrites** the Region's existing invocation logging configuration.
- Disables prompt, response, and image payload delivery (metadata only).
- Retains the configuration, role, and log group on stack deletion, because
  the previous configuration cannot be reconstructed.

Use this only in a demo account or where the stack owns the setting.

The custom resource that applies the setting runs on create **and** on
update, so a redeploy repairs the configuration if it was changed by hand.
The log group is named `/bedrock/spend-controls/model-invocations`; because
it is retained, a destroy followed by a new deploy in the same Region needs
the old group deleted or renamed, or passed back via
`invocation_log_group_name`.

`manage_invocation_logging: false` (implied by `invocation_log_group_name`):

- Requires `invocation_log_group_name`.
- Does not change the account-wide setting.
- Adds subscription filters (vended role, and workload profiles when
  configured) to the existing log group.

In a shared account, confirm that the log group receives Runtime invocation
logs and has subscription-filter capacity left: CloudWatch Logs allows five
subscription filters per log group (not adjustable), and this stack adds one,
or two when workloads are configured. This stack must not displace a
security or central-logging subscription.

## Model and inference-profile IAM

`allowed_model_arns` is the allowlist for everything a vended session may
invoke. Production lists exact resources:

```json
{
  "allowed_model_arns": [
    "arn:aws:bedrock:*:111122223333:inference-profile/PROFILE-ID",
    "arn:aws:bedrock:*::foundation-model/PROVIDER.MODEL-ID"
  ]
}
```

A cross-Region inference profile requires permission on its underlying
foundation-model resource in every Region it routes to, hence the `*`
Region in the `foundation-model` ARN; validate the complete policy for every
profile you allow. Independently of the allowlist, the role policy and the
permissions boundary carry an explicit `Deny` on
`application-inference-profile/*` and `provisioned-model/*` resources and
on `bedrock:StartAsyncInvoke` and `bedrock:InvokeModelWithBidirectionalStream`,
so a vended session can never reach a workload's profile, provisioned
throughput, or the two unmetered invoke paths, even with `["*"]`. Models
used through those APIs are still worth excluding from the allowlist so
that other principals cannot be confused about what is metered.

The vended role never receives `bedrock:CallWithBearerToken`, so Bedrock API
keys are outside this sample; applications use the temporary STS credentials
with SigV4.

## JWT and identity provider

`jwt_issuer`, `jwt_audience`, and `jwt_user_claim` describe the tokens your
application already holds. With `jwt_issuer` empty, the stack creates a
Cognito user pool and a secretless app client whose ID is the JWT audience;
that client allows `USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for programmatic
clients. With your own issuer, the broker verifies issuer, audience,
signature, expiry, and the presence of `jwt_user_claim` against the JWKS
found through OIDC discovery (or `jwt_jwks_url`). Console sign-in against a
corporate IdP is described in
[DEPLOYMENT.md](../DEPLOYMENT.md#4-corporate-idp-and-console).

## Reconciliation

The ledger is an estimate. With `reconciliation_enabled: true` a daily Lambda
(`SpendReconciliationFn`, `cron(0 6 * * ? *)`) compares the ledger's total
for one settled day (`today - reconcile_lag_days`, UTC) with Cost Explorer's
Bedrock spend for the same day and Region:

- **Aggregate**: the sum of every subject's daily row against Cost Explorer
  `UnblendedCost` for `SERVICE` in `reconciliation_service_names` (default
  `Amazon Bedrock` and `Amazon Bedrock Service`), `REGION` = the stack
  Region, and `RECORD_TYPE` = `Usage` (credits, refunds, and tax lines are
  excluded).
- **Per workload**: each `workload:<name>` row against Cost Explorer
  filtered on the cost-allocation tag `bedrock-spend-controls-workload=<name>`
  that the stack stamps on every application inference profile.

Results are stored as `RECONCILE#<day>` rows in the usage table (same
retention as the ledger), served by `GET /admin/reconciliation`, shown on the
Operations tab, and emitted as `ReconciliationDeltaPercent`; the
`reconciliation_delta` alarm fires after two consecutive days over
`reconciliation_alarm_percent`. Two more alarms come with the feature:
`reconciliation_errors` (the Lambda's `Errors` metric) and
`reconciliation_timeout` (a `ReconciliationStarted` heartbeat without a
same-day `ReconciliationRuns` or `ReconciliationFailure`, which means the
run timed out). The function is granted `ce:GetCostAndUsage`
(Cost Explorer has no resource-level scoping; it is the only `ce:` action
granted). One run makes `1 + workloads` requests at $0.01 each.

Before the first run:

1. **Enable Cost Explorer on the payer account.** Open Billing and Cost
   Management → Cost Explorer once. The API can take up to 24 hours to start
   answering; until then the function emits `ReconciliationFailure` and one
   SNS message per day.
2. **Activate the cost-allocation tag** if you use workloads, or every
   workload reports `tag_inactive`. In Billing and Cost Management → Cost
   allocation tags, select `bedrock-spend-controls-workload` → Activate, or:
   ```bash
   aws ce update-cost-allocation-tags-status \
     --cost-allocation-tags-status TagKey=bedrock-spend-controls-workload,Status=Active
   ```
   The tag appears only after a tagged resource has incurred cost; Cost
   Explorer starts attributing about 24 hours after activation and does not
   backfill. **In an AWS Organization this is a management (payer) account
   action**: a linked account gets `AccessDeniedException: Linked account
   doesn't have access to cost allocation tags`. Aggregate reconciliation
   does not depend on the tag.

Limits: Cost Explorer data lags 24 to 48 hours, which is why the default
compares D-2. JWT users share one IAM role and therefore one line in the
bill, so there is no per-user reconciliation. A run never reprices the
ledger; it is a drift signal. How to read the delta:
[operations.md](operations.md#reading-the-reconciliation-card).

## TTL and reset

Quota windows change at their calendar boundary (00:00 UTC daily, Monday
00:00 UTC weekly, the 1st at 00:00 UTC monthly). DynamoDB TTL never resets a
quota: it only removes expired usage rows, request-ID markers, per-minute
rate counters, and session mappings, asynchronously, after
`usage_retention_days`.

A subject automatically blocked in an earlier window becomes active again
through the paths listed in
[quotas.md](quotas.md#blocking-and-unblocking); an admin block never lifts
automatically.

## Runtime lease dial

Every vended credential embeds an immutable session-policy deadline of 60,
300, or 900 seconds. Which one is a **runtime setting**, read from the
`CONFIG#ENFORCEMENT` row on every vend:

- `permission_lease_seconds` in the deployment file is only the **default**,
  used when no runtime override exists.
- `PUT /admin/enforcement` changes the effective value without a redeploy;
  the change applies to new vends immediately, is audited, and emits
  `EnforcementDialChanged`. Outstanding credentials keep the deadline they
  were issued with.
- `GET /admin/operations` → `configuration` reports
  `permission_lease_seconds` (effective), `permission_lease_source`
  (`deployment_default` or `runtime`), and
  `permission_lease_default_seconds`.

A shorter lease shortens the worst-case overspend and multiplies broker
vends (a 60-second lease is five times the vend traffic of 300 seconds; see
[cost-estimate.md](cost-estimate.md)). `refresh_overlap_seconds` and
`refresh_jitter_seconds` must fit inside the smallest lease you intend to
dial to.
