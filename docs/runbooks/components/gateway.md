# Component: broker / admin API (`BrokerApiFn`)

**Code:** `gateway/app/` (FastAPI behind AWS Lambda Web Adapter; `main.py`
routes, `quota.py` DynamoDB store, `broker.py` STS vend, `auth.py` JWT,
`session_policy.py` lease policy). **Log group:** `/aws/lambda/<BrokerApiFn>`.
**Invocation:** `AWS_IAM` Function URL (`BrokerApiUrl` output), buffered.

## What it does

Authenticates an application's OIDC JWT (the first configured audience
only; the console client's audience is accepted on `/admin/*` and nowhere
else), decides from DynamoDB whether the configured identity is active and
under quota (every enabled calendar period, the current rate minute, and
every model budget, re-checked after the lease reservation), reserves a
logical lease, assumes `BedrockUserRole` with
`RoleSessionName = SourceIdentity = <sanitized claim>` and an inline session
policy carrying the permission-lease deadline (`aws:CurrentTime
DateLessThan`), records the session→identity map, and returns the STS keys.
The same function serves the routine `/admin/*` API (shared key or admin JWT
claim) and the break-glass `POST /admin/emergency-stop` (separate key).

It never inspects inference traffic and cannot call Bedrock itself.

## IAM scope

- `dynamodb:*Item`/`Query`/`Scan` on the users and admin-audit tables (read/write), read-only on the usage table.
- `sts:AssumeRole`, `sts:SetSourceIdentity`, `sts:TagSession` on `BedrockUserRole` only.
- `secretsmanager:GetSecretValue` on the two key secrets.
- `cloudwatch:GetMetricData`, `DescribeAlarms`, `ListMetrics` (`*`) for the Operations and Overview endpoints.
- `ssm:GetParameter` on `WorkloadRosterParameter` only (the deployed workload roster: name, model, profile ARN, role ARN, enforceability). Read with a five-minute in-memory cache; the roster is static deploy configuration.
- No `iam:*`, no `bedrock:*`.

## Inputs / outputs

| Input | Output |
|---|---|
| `POST /v1/credentials` + JWT in `X-Quota-User-Token` (`Authorization: Bearer` only on local runs without SigV4) | STS keys, `expiration` (lease deadline), `sts_expiration`, `lease_id`, `refresh_after`; quota headers `X-Quota-*` (automatic 403 blocks carry the breached period, dimension, and reset time; admin blocks do not) |
| `/admin/*` with `X-Quota-Admin-Key` or admin JWT; `POST /admin/emergency-stop` with `X-Quota-Emergency-Key` | JSON; user mutations echo `ETag`, `X-Request-Id`; the enforcement dial echoes `ETag` only; the emergency route returns the control state (409 `version_conflict` when two operators race) |
| DynamoDB `CONFIG#ENFORCEMENT` (runtime dial), `CONFIG#EMERGENCY_STOP` | 503 when emergency active; lease length from the dial |
| EMF metrics | `CredentialsVended`, `LeaseStarted/Refreshed/Retried`, `Throttles` on dimension sets `[UserId]` and none; `EnforcementDialChanged` without dimensions |

## Failure modes and where they surface

| Symptom | Where | Notes |
|---|---|---|
| All vends `401` | Lambda log `authentication_error` reasons | JWKS unreachable / issuer or audience mismatch (a token minted for the console client is rejected here by design); with auto-provisioning off, an unknown user; a claim containing `#`. Check `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_JWKS_URL` env; the verifier caches JWKS in memory per container. |
| All vends `503 emergency_stop` | `GET /admin/emergency-stop` | Intended while the emergency state is not `inactive`. See emergency runbooks. |
| All vends `429 lease_rate_limited` | `Throttles` metric, `Reason: vend-rate-limit` | More than `vend_rate_limit_per_minute` (6) vends per identity per minute across all processes: a client not honouring `refresh_after`, a provider per request, or more backend replicas serving one identity than the budget allows. Raise the limit to at least the replica count per identity. |
| `429 lease_not_refreshable` | same | A concurrent renewal race left no lease to join. Transient; the client retries after `Retry-After`. A different `X-Quota-Lease-Id` before `refresh_after` is not refused: it joins the current lease (`LeaseRetried` with `LeaseJoined: true` in the broker log). |
| Vend `broker_error` 5xx | log | STS `AssumeRole` failure: role trust, `max_session_duration` < `vended_ttl_seconds`, or STS throttling (600 rps regional). |
| Admin mutation `503 transaction_unavailable` | log | DynamoDB `TransactWriteItems` cancelled for a non-conditional reason. Retry. |
| Admin `409 version_conflict` / `idempotency_conflict` | response | Expected concurrency outcomes, not failures. |
| Lambda `Errors` > 0 | [broker-api-errors](../alarms/broker-api-errors.md) | Unhandled exception; the Web Adapter returns 502. Check the log. |
| Workloads tab shows every row as *Unregistered* / `GET /admin/workloads` reports `roster_source: fallback` | log `workload roster parameter ... unreadable` | Parameter Store read failed (permissions, throttling, parameter deleted). Rows still list and enforce — the enforcer has its own copy of the roster — but the console loses model/profile/role identity. Check the `WorkloadRosterParameter` exists and the broker role's `ssm:GetParameter` grant; a redeploy recreates both. A stale-but-present cache is preferred over the fallback, so a transient failure is invisible for up to five minutes. |
| Workload added to `workloads.json` and deployed, still *Awaiting traffic* | `GET /admin/workloads` | Expected until its first invocation through the new inference profile creates the quota row. If the app is invoking, check it uses the `WorkloadProfileArn<Name>` output as `modelId` — a direct model ID is denied by the attached policy and never attributed. |

## Manual operations

- **Read the runtime lease dial:** `GET /admin/enforcement` (ETag = generation).
  Change: `PUT /admin/enforcement {"permission_lease_seconds": 60|300|900,
  "reason": "..."}` with `If-Match`. Applies to new vends only.
- **Inspect one identity:** `GET /admin/user?user_id=<raw claim>`; current
  usage across periods, thresholds, rate, model budgets, lease timing.
- **Find the STS session for an identity:** the users row's
  `source_identity` attribute is the exact `RoleSessionName`; `SESSION#<name>`
  rows map back (`maps_to`).
- **Rotate the admin key:** update the secret value in Secrets Manager; the
  Lambda caches the key per container, so force a new deployment or wait for
  container recycling. The emergency key is separate and rotates the same
  way.
- **Local run:** `uvicorn app.main:app` with a `.env`; `JWT_SHARED_SECRET`
  switches the verifier to HS256 for tests only.
