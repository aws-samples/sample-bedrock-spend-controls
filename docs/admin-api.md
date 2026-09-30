# Broker API reference

The broker Lambda (`BrokerApiFn`) serves one `AWS_IAM` Function URL
(`BrokerApiUrl` output) with three families of routes: credential vending for
applications, routine administration, and the break-glass emergency stop.
Every request must be SigV4-signed for service `lambda` in the stack Region
by a principal allowed to invoke the URL (`invoker_principal_arns`).

## Authentication

| Route family | Additional header | Who |
|---|---|---|
| `POST /v1/credentials` | `X-Quota-User-Token: <end-user JWT>` | Application backends listed in `invoker_principal_arns` |
| `/admin/*` (routine) | `X-Quota-Admin-Key: <shared key>` from `AdminKeySecretArn`, **or** `X-Quota-User-Token: <admin JWT>` whose `admin_jwt_claim` contains `admin_jwt_value` | CLI and backends (key); console (JWT) |
| `POST /admin/emergency-stop` | `X-Quota-Emergency-Key: <break-glass key>` from `EmergencyKeySecretArn` | Operators only; routine credentials are refused |
| `GET /healthz` | none | Anyone who can invoke the URL |

A missing or wrong admin credential returns `403 forbidden`. `Authorization:
Bearer` is not used for these tokens because the header carries the SigV4
signature.

From a shell, `awscurl` signs requests (`BROKER_API_URL` is the `BrokerApiUrl`
output without its trailing `/`):

```bash
awscurl --service lambda --region "$AWS_REGION" -H "X-Quota-Admin-Key: $ADMIN_KEY" \
  "$BROKER_API_URL/admin/summary"
```

## Conventions

- **Identities are query parameters.** Exact-user routes take
  `?user_id=<raw claim value>`, supplied exactly once and URL-encoded once by
  the HTTP client before signing. Identities may contain `/`, `@`, or `:`.
  The `workload:` prefix and internal `#` prefixes (`SESSION#`, `VEND#`,
  `REVOCATION#`, `CONFIG#`, ...) are reserved.
- **Mutations** accept `Idempotency-Key` (1 to 256 characters; a UUID) and,
  where noted, `If-Match` with the user's ETag or integer version. Both are
  optional on the wire; see [operations.md](operations.md#safe-routine-writes)
  for why you should always send them.
- **Mutation responses** return the complete canonical `user`, an `ETag`
  header (`"<version>"`), and `X-Request-Id`.
- **Reasons.** Bodies accept an optional `reason` string; omitted or blank
  reasons are stored as `not provided`. The emergency route requires one.
- **Errors** use one envelope:
  ```json
  {"error": {"type": "version_conflict", "code": "version_conflict",
             "message": "...", "details": {"current_user": {...}}}}
  ```
- **Pagination** uses `limit` and an opaque `cursor`; responses carry
  `next_cursor` (`null` on the last page).

## Credential vending

### `POST /v1/credentials`

Empty body. Headers: `X-Quota-User-Token` (required),
`X-Quota-Lease-Id` (client UUID; a new one for every renewal, the same one
only to retry a request whose outcome was not seen; omitted means the broker
generates one).

Each identity has one logical lease. The broker answers a vend in one of
three ways:

| Request | Result |
|---|---|
| Same `lease_id` as the identity's current lease, before its deadline | Retry: new STS keys, original deadline |
| Different `lease_id`, before the current lease's `refresh_after` | Join: new STS keys with the current lease's deadline; `lease_id` in the response is the current lease (another process holds it) |
| Different `lease_id`, at or after `refresh_after`, or no lease yet | New generation with a new deadline; concurrent starters race and the losers join the winner |

No path extends the identity's deadline before its refresh window.

**200**

```json
{
  "aws_access_key_id": "...", "aws_secret_access_key": "...", "aws_session_token": "...",
  "expiration": "2026-09-23T10:05:00+00:00",
  "sts_expiration": "2026-09-23T10:15:00+00:00",
  "refresh_after": "2026-09-23T10:04:45+00:00",
  "lease_id": "…", "lease_generation": 3,
  "region": "us-east-1",
  "endpoint": "https://bedrock-runtime.us-east-1.amazonaws.com",
  "user_id": "…"
}
```

`expiration` is the permission-lease deadline and is the value to use as the
credential expiry; `sts_expiration` is when the keys themselves stop
working. Renew after `refresh_after` with a new `lease_id`.

Response headers on every vend response: `X-Quota-Window` (current UTC day)
and `X-Quota-Enabled-Periods` (comma-separated). Refusals add:

| Status | `type` | Extra headers | Meaning |
|---|---|---|---|
| 401 | `authentication_error` | | JWT missing, invalid, expired, wrong issuer/audience, or claim missing |
| 400 | `invalid_lease` | | Malformed `X-Quota-Lease-Id` |
| 403 | `quota_blocked` | quota headers | Subject status is `blocked` |
| 429 | `quota_exceeded` | `X-Quota-Breached-Period`, `X-Quota-Breached-Dimension`, `X-Quota-Resets-At` | A block threshold was reached at vend time; the subject is now blocked |
| 429 | `lease_rate_limited` | `Retry-After`, `X-Quota-Refresh-After` | More than `vend_rate_limit_per_minute` vends for this identity |
| 429 | `lease_not_refreshable` | `Retry-After`, `X-Quota-Refresh-After` | A concurrent renewal left no lease to join (rare race); retry after `Retry-After` |
| 409 | `lease_expired` | | The supplied `lease_id` has expired; start a new lease |
| 503 | `quota_state_conflict` | `Retry-After: 1` | Concurrent status change; retry once |
| 503 | `emergency_stop` | `Retry-After: 60` | Vending closed by the emergency stop |
| 5xx | `broker_error` | | STS `AssumeRole` failure |

Treat 401, 403, 409, and 429 as final for this attempt; retry only 5xx and
transport errors with backoff. The reference client is
`examples/refreshable_bedrock.py` ([integration.md](integration.md)).

### `GET /healthz`

Returns `{"status": "ok", "inference_endpoint": "bedrock-runtime",
"metering": "cloudwatch-logs-subscription"}`. No quota or IAM state.

## Users

### `POST /admin/users`

Create a subject. Conditional: never overwrites.

Body: `{"user_id": "<claim value>", "name": "<display name>", "limits":
{...}, "rate": {"rpm": N, "tpm": N}, "reason": "..."}`. `limits` follows the
period shape in [quotas.md](quotas.md#calendar-periods) and defaults to
`default_limits` when omitted; `rate` defaults to `default_limits.rate`.
Header: `Idempotency-Key`.

Responses: `200` with `{"user_id", "provisioned": true, "limits", "user"}`;
`400 invalid_request_error` (reserved prefix, `workload:` namespace, bad
limits); `409 user_already_exists` with `details.current_user` and the
current `ETag`; `409 idempotency_conflict`.

### `GET /admin/users`

List subjects. Query: `limit` (1 to 1000, default 50), `cursor`, `status`
(`active` | `blocked`), `query` (substring search), `granularity` (`user` |
`workload`), `include_usage` (default `true`; `false` returns identity, status,
limits, and lease only, which is what the live-lease poll uses).

Response: `{"users": [...], "next_cursor"}`. Each user carries `user_id`,
`name`, `status`, `status_origin`, `status_reason`, `limits`, `rate`,
`model_budgets`, `lease` (generation and timing, no lease ID), `version`,
`created_at`, `updated_at`, and, with usage, `today` plus `current_usage`
per enabled period.

### `GET /admin/user?user_id=`

One subject with `current_usage` for every period. Returns `ETag`. `404
not_found` for unknown subjects.

### `PUT /admin/user/limits?user_id=`

Replace limits. Body: `{"limits": {daily|weekly|monthly: {...} | null},
"rate": {"rpm", "tpm"}, "reason"}`; either `limits` or `rate` may be sent
alone. Headers: `If-Match`, `Idempotency-Key`. The subject's automatic
status is reconciled immediately against the new limits.

Responses: `200` with the canonical user and new `ETag`; `400`; `404`; `409
version_conflict` (`details.current_user`); `409 idempotency_conflict`;
`503 transaction_unavailable` (retry).

### `PUT /admin/user/status?user_id=`

Body: `{"status": "active" | "blocked", "reason": "..."}`. Headers:
`If-Match`, `Idempotency-Key`. Sets `status_origin: admin`. Same responses
as the limits route.

### `PUT /admin/user/model-budget?user_id=&model_id=`

Set or replace one per-model budget. Body: `{"limits": {daily|weekly|monthly:
{...} | null}, "reason"}`; at least one period must be enabled; `rate` is
rejected. `model_id` is a model or inference-profile ID (not an ARN) covered
by `allowed_model_arns`. Headers: `If-Match`, `Idempotency-Key`. Reconciles
the automatic status immediately.

Response: `200` with `{"user_id", "model_id", "updated": true,
"model_budgets", "user"}`.

### `DELETE /admin/user/model-budget?user_id=&model_id=`

Remove one budget. Body: `{"reason"}` (optional). Headers: `If-Match`,
`Idempotency-Key`. `404 not_found` when the budget does not exist. Removing
the binding budget lifts an automatic block.

### `GET /admin/user/model-usage?user_id=&model_id=`

Current daily, weekly, and monthly usage of one subject × model ledger.

### `GET /admin/user/usage?user_id=&period=&window=`

Usage for one calendar window. `period` defaults to `daily`; `window` is the
window start (`YYYY-MM-DD`) and defaults to the current one. Response fields:
`period`, `window`, `window_start`, `window_end`, `resets_at`, `cost_usd`,
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`images`, `requests`, `unpriced_requests`.

### `GET /admin/user/usage-history?user_id=&period=&start=&end=&limit=&cursor=`

Retained windows for one subject, newest first, bounded by
`usage_retention_days`. A range outside retention returns `400
usage_range_outside_retention` with `oldest_available_date` and
`latest_available_date` in `details`.

### `GET /admin/user/audit?user_id=&limit=&cursor=`

Routine audit events for one subject (`limit` 1 to 100).

## Deployment-wide reads

| Route | Query | Returns |
|---|---|---|
| `GET /admin/summary` | | Today's aggregate usage; `enforcement` (`mode: layered`, effective lease and its source, refresh, vend rate limit, shard layout, `total_users`, `blocked_users`, `blocked_user_ids`, `subjects` split into `users` and `workloads` with `configured`, `metering_only`, `unregistered`, `awaiting_traffic` counts); `observability` (metrics namespace, detection-lag metric) |
| `GET /admin/workloads` | | `{"workloads": [...], "roster_source", "tag_key"}`. Each workload: `workload_id`, `name`, `model`, `profile_arn`, `role_arn`, `enforcement_ready`, `registered` (present in the deployed roster), and `subject` (the metered row with usage, or `null` until the first invocation) |
| `GET /admin/usage/metrics` | `days` (1 to 30, default 14) | Daily per-model `EstimatedCostUSD`, `Requests`, `InputTokens`, `OutputTokens` series and `top_users` (each with `granularity`) from CloudWatch EMF; `status` is `available`, `partial`, or `unavailable` |
| `GET /admin/audit` | `user_id`, `limit` (1 to 100), `cursor` | Global routine audit events |
| `GET /admin/operations` | | Operations tab payload ([operations.md](operations.md#operations-tab)) |
| `GET /admin/reconciliation` | `limit` (1 to 90, default 14) | `{"enabled", "lag_days", "runs", "latest"}`, or `{"enabled": false, "message"}` when reconciliation is not deployed |

## Enforcement dial

### `GET /admin/enforcement`

Returns `permission_lease_seconds` (effective), `source`, `generation`,
`updated_at`, `actor`, `reason`, `valid_permission_lease_seconds` (`[60,
300, 900]`), `default_permission_lease_seconds`. `ETag` is the generation.

### `PUT /admin/enforcement`

Body: `{"permission_lease_seconds": 60 | 300 | 900, "reason": "..."}`.
Headers: `If-Match` (generation), `Idempotency-Key`. Applies to new vends
immediately. Responses: `200` with the new configuration and `ETag`; `400`;
`409 version_conflict` (`details.current_enforcement`); `409
idempotency_conflict`.

## Emergency stop

### `GET /admin/emergency-stop`

Routine authorization. Returns the control row: `state` (`inactive`,
`activating`, `active`, `recovering`), `desired_active`, `generation`,
`applied_generation`, `requested_at`, `applied_at`, `actor`, `reason`.

### `POST /admin/emergency-stop`

Break-glass authorization (`X-Quota-Emergency-Key`). Body:

```json
{"action": "activate", "confirmation": "STOP_ALL_BEDROCK_SESSIONS", "reason": "..."}
{"action": "recover",  "confirmation": "RESTORE_ALL_BEDROCK_SESSIONS", "reason": "..."}
```

Returns `202` with the control state plus `idempotent` (already in the
requested stable state) and `retry` (same action requested again while
converging). `400 confirmation_required` when the phrase does not match the
action; `400 invalid_request_error` for a missing reason; `403 forbidden`
without the key. The emergency processor converges IAM asynchronously; poll
`GET /admin/emergency-stop` until `state` is `active` or `inactive`.

## Error codes

| Status | `type` | Where |
|---|---|---|
| 400 | `invalid_request_error` | Bad body, query, `If-Match`, or `Idempotency-Key` |
| 400 | `usage_range_outside_retention` | Usage history requested before `usage_retention_days` |
| 400 | `confirmation_required` | Emergency stop phrase mismatch |
| 400 | `invalid_lease` | Vend: malformed lease ID |
| 401 | `authentication_error` | Vend: JWT rejected |
| 403 | `forbidden` | Admin credential missing or wrong |
| 403 | `quota_blocked` | Vend: subject blocked |
| 404 | `not_found` | Unknown subject or model budget |
| 409 | `user_already_exists` | Create on an existing identity |
| 409 | `version_conflict` | Stale `If-Match` |
| 409 | `idempotency_conflict` | `Idempotency-Key` reused with a different request |
| 409 | `lease_expired` | Vend: expired lease ID |
| 429 | `quota_exceeded`, `lease_rate_limited`, `lease_not_refreshable` | Vend refusals |
| 503 | `emergency_stop`, `quota_state_conflict` | Vend: closed or racing |
| 503 | `transaction_unavailable` | Admin: DynamoDB transaction cancelled for a non-conditional reason; retry |
| 5xx | `broker_error` | Vend: STS failure |
