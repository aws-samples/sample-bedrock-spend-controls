# Bedrock Spend Controls admin console

Static React console for the broker's administrative API.

It provides:

- **Overview**: spend and request totals split into users and workloads, the
  enforcement strip (credential lifetime, permission lease, refresh, vend
  rate), and per-model usage charts with 7/14/30-day ranges plus a
  top-spenders list. Charts come from `GET /admin/usage/metrics` (CloudWatch
  metrics, observability only); the DynamoDB daily ledger remains the quota
  source.
- **Users**: a 25-row server-paginated list of signed-in users with
  server-side status and search filters, a create wizard (`POST /admin/users`
  is conditional and never overwrites; duplicates return
  `409 user_already_exists`), reasoned block/unblock, and a detail drawer with
  all three calendar windows, rate limits, per-model budgets, usage history,
  and the per-user audit trail.
- **Workloads**: the deployed roster of applications on their own IAM roles,
  attributed by application inference profile, with the same limits, status,
  and drawer as users. Blocking a workload attaches an inline IAM Deny to its
  role; role-less workloads are metered and alerted but not enforced.
- **Limits**: simultaneous daily, weekly, and monthly UTC-calendar limits for
  USD, input tokens, and output tokens, with ordered warn/block thresholds,
  per-minute request and token rate limits, and optional per-model budgets.
  `null` disables a period and `0` means **Unlimited** for one dimension.
  Period enable/disable, Unlimited changes, alert-only thresholds, and finite
  limits below current-period usage require an audit reason.
- **Operations**: the audited runtime permission-lease dial (allowed values
  come from the broker; a reason is required; applies to new credentials
  without a redeploy), break-glass emergency stop/recovery with a separate
  key and exact confirmation phrase, live leases polled every five seconds,
  and health cards for enforcement, revocation, telemetry, the nightly
  auto-block sweep, spend reconciliation (ledger vs Cost Explorer), and
  CloudWatch alarms and DLQs.
- **Audit log**: newest-first create, limit, and status changes with an
  explicit refresh and its own freshness/error state.
- Independent stale/error states, so one failed panel keeps its last
  successful timestamp without presenting unrelated data as fresh. Missing
  telemetry is shown as unknown or unavailable, never as healthy.

The console never mutates IAM directly and receives no CloudWatch permissions,
secret ARN, or policy ARN. The emergency key is entered at action time, sent
only with that request, and never part of `config.js`, API responses, or
browser storage.

## Authentication

The console is IdP-agnostic: it speaks standard OIDC to whichever issuer is
configured, and the demo deployment uses an Amazon Cognito User Pool as that
issuer. The browser never receives the shared admin secret. It:

1. Resolves the authorization, token, and JWKS endpoints from the issuer's
   discovery document and redirects with an authorization-code + PKCE S256
   request. Only the short-lived verifier, state, and nonce are kept in
   `sessionStorage`; tokens remain in memory.
2. Validates callback state plus the ID token nonce, issuer, audience, and
   expiry before using it, then removes the code from the browser URL.
3. Exchanges the ID token through the Cognito Identity Pool for temporary AWS
   credentials and renews both as they approach expiry. The deployed CSP must
   allow the Lambda Function URL (`https://*.lambda-url.<region>.on.aws`) and
   Cognito Identity endpoint in `connect-src`.
4. SigV4-signs requests to the broker's `AWS_IAM` Function URL and sends the
   ID token in `X-Quota-User-Token`; the broker authorizes `/admin` from the
   configured administrator claim.
5. Ends the session through the issuer's end-session endpoint (Cognito
   `/logout` in the demo) and clears in-memory state.

Subject requests use the `/admin/user?user_id=` family: `GET /admin/user`,
`PUT /admin/user/limits`, `PUT /admin/user/status`, `GET /admin/user/usage`,
`GET /admin/user/usage-history`, `GET /admin/user/audit`,
`PUT|DELETE /admin/user/model-budget`, and `GET /admin/user/model-usage`, with
the raw identity as the `user_id` query parameter (encoded once before
signing). `POST|GET /admin/users` are the collection routes.

## Write safety and audit

Every mutation sends a UUID `Idempotency-Key`; limit and status changes also
send `If-Match` with the version the operator reviewed. Successful writes
return the complete canonical user, which the console validates rather than
merging assumed local state; duplicate-create, stale-version, and
idempotency-reuse conflicts stay visible. Create, limit, status, and
model-budget changes are written transactionally to the admin-audit table
(365-day retention) and can be read globally or per user; a missing reason is
recorded as `not provided`. Temporary overrides, bulk operations, user delete,
and usage reset are not implemented.

## Build and test

```bash
npm ci
npm test
npm run build   # tsc --noEmit && vite build
```

`public/config.js` holds the runtime configuration (`gatewayUrl`, `region`,
`issuer`, `clientId`, `identityPoolId`, optional `scopes`). The CDK deployment
writes it from the stack outputs and uploads `dist/`, so one build serves any
deployment. Callback (`/auth/callback`) and logout (`/`) URLs derive from the
CloudFront origin; the client does not register a localhost callback by
default, so authenticate through the deployed URL unless the deployment
configuration is changed to allow it. See [`../DEPLOYMENT.md`](../DEPLOYMENT.md)
for swapping in a corporate IdP.

### Local preview without a backend

`preview/` mounts the full dashboard against an in-memory fake broker (users,
workloads, leases, audit, reconciliation, emergency stop):

```bash
npx vite --port 5179 --strictPort --host 127.0.0.1
# then open http://127.0.0.1:5179/preview/index.html
```

Query switches: `?recon=on|tag|alarm|off|empty` and `?sweep=ok|failed|never`;
the emergency-stop dialog accepts the key `break-glass`. The harness is outside
`tsconfig`'s `include` and never imported by `src/main.tsx`; keep its fixtures
in step with `src/api.ts` when the API changes.

### Number presentation

All numbers use one fixed convention regardless of browser locale
(`src/format.ts`): `,` groups thousands, `.` separates decimals, money always
shows two decimals (`$1,234.56`), integers none (`31,000,000`), and token
counts abbreviate from a thousand (`21.6M`). Sub-cent amounts round to
`$0.00`. Timestamps stay in the browser locale.
