# BrokerApiErrorsAlarm

**Operations key:** `broker_api_errors` · **Alarm name:** `<stack>-broker-api-errors` · **Metric:** Lambda `Errors` on `BrokerApiFn` (Sum ≥ 1 over 5 min, 1 period; missing data = not breaching) · **Source:** the function's own error count; the FastAPI application behind the Lambda Web Adapter raised an exception that no route handler caught, or the runtime failed before serving (init failure, timeout, out of memory).

## What it means

At least one broker invocation ended in an unhandled error. The Web Adapter
returns `502` to the caller, so:

- A **vend** (`POST /v1/credentials`) failed without a typed refusal. The
  client library treats a 5xx as non-terminal, retries up to three attempts
  with backoff, and then raises `BrokerCredentialError`; the application
  sees "service unavailable", not a quota decision. Already-vended sessions
  keep working until their lease deadline, so **enforcement is not weakened**
  by this alarm: a broker that cannot vend only denies new access.
- An **admin** request failed; the console shows the panel as stale or
  errored and the CLI prints the 502.

Routine refusals (`401`, `403`, `429`, `503 emergency_stop`) are not errors
and never trip this alarm.

## Severity guidance

**Page if `Errors` is a large share of `Invocations`** (every vend failing
means every user whose lease expires loses Bedrock access within one lease
window, 60 to 900 s). **Next business day** for an isolated error with
`Invocations` otherwise healthy.

## Likely causes

1. **DynamoDB unavailable or throttled** on the users, usage, or admin-audit
   table (`ProvisionedThroughputExceededException` is impossible on
   on-demand tables, but `InternalServerError` and account-level throttles
   surface here).
2. **Cold-start failure**: the function reads the admin and emergency keys
   from Secrets Manager at start (`secretsmanager:GetSecretValue` denied or
   the secret deleted), or an environment variable is missing after a manual
   edit (`OPERATIONS_ALARM_NAMES_JSON`, `JWT_*`, table names).
3. **Timeouts**: a slow JWKS fetch on the first request after a container
   start, or CloudWatch `GetMetricData` on the Operations endpoint taking
   longer than the function timeout.
4. **A defect** in a code path a test did not cover; the traceback is in the
   log.

Read the tracebacks:
```bash
aws logs filter-log-events --log-group-name /aws/lambda/<BrokerApiFn> \
  --filter-pattern '?Traceback ?"Task timed out" ?"Runtime exited"' \
  --start-time $(( $(date +%s) - 3600 ))000 --query 'events[].message' --output text | tail -40
```

## Remediation

1. Classify from the traceback: infrastructure (DynamoDB, Secrets Manager,
   IAM) or code.
2. For infrastructure, restore the permission or wait out the service event;
   the broker is stateless and recovers on the next request.
3. For a cold-start failure after a manual change, redeploy the stack so the
   environment and IAM policy match the template again.
4. For a defect, roll back to the previous deployment (`cdk deploy` of the
   last known-good revision) and file the traceback.
5. Confirm with `GET /healthz` (signed) and one real vend from the
   application.

## How to verify recovery

- `Errors` Sum = 0 for one 5-minute period → alarm `OK`.
- `CredentialsVended` resumes at its usual rate; `/admin/operations` answers
  and the console panels lose their stale markers.

## Related

- Component: [components/gateway.md](../components/gateway.md)
- Vend refusals that are *not* errors: [admin-api.md § Credential vending](../../admin-api.md#credential-vending)
- Metrics: Lambda `Errors`, `Invocations`, `Duration`, `Throttles` (namespace `AWS/Lambda`)
