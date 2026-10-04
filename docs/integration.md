# Integrating an application with Bedrock Spend Controls

This guide is for the developer who owns the application that calls Amazon
Bedrock. The stack itself is deployed and operated by whoever follows
[DEPLOYMENT.md](../DEPLOYMENT.md); you need three things from them and
nothing else:

| You need | Stack output / config key |
|---|---|
| The broker URL | `BrokerApiUrl` |
| Your backend's IAM role listed as an allowed caller | `invoker_principal_arns` |
| Confirmation that the stack trusts your IdP and which claim is the quota identity | `jwt_issuer`, `jwt_audience`, `jwt_user_claim` |

Nothing in the inference path changes: no proxy, no new endpoint, no change
to prompts, streaming, or response handling. What changes is **where your
backend gets the AWS credentials it signs Bedrock calls with**.

Pick the path that matches your application. One account can use both.

| Your application | Path |
|---|---|
| Serves authenticated end users and already holds their OIDC JWT | [Per-user](#per-user-integration) |
| Runs with its own IAM role and has no end user (batch, ETL, internal service, agent) | [Per-workload](#per-workload-integration) |

---

## Per-user integration

### Why the credentials have to be per user

Today your backend calls Bedrock with the server's role. Every call looks
identical to AWS, so AWS cannot cap Alice without capping Bruno. The broker
fixes this by vending, for each end user, short-lived STS credentials whose
session carries that user's identity (`aws:SourceIdentity`). Bedrock logs that
identity on every invocation, the usage processor sums it into the user's
ledger, and the enforcement layers cut that identity and only that identity.

So the backend remains one process, but instead of one global Bedrock client
it asks for **the client of the user making this request**. That is the whole
change.

### Install

Copy `examples/refreshable_bedrock.py` into your project; the file is
self-contained (it carries its own minimal SigV4 signer). Dependencies:
`boto3` and `httpx` at the versions pinned in `examples/requirements.txt`.
Python 3.10 or later.

### The change

```python
# before
import boto3
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

def handle_chat(request):
    user = verify_jwt(request.jwt)               # you already do this
    return bedrock.converse(modelId=MODEL, messages=request.messages)
```

```python
# after
from refreshable_bedrock import (
    AuthenticationError, BedrockSpendControls, EmergencyStopError,
    QuotaExceededError, UserBlockedError, VendRateLimitedError,
)

# one per process, created at startup
spend_controls = BedrockSpendControls(
    gateway_url=BROKER_API_URL,       # stack output
    region="us-east-1",
    identity_claim="sub",             # must equal the deployment's jwt_user_claim
)

def handle_chat(request):
    user = verify_jwt(request.jwt)                          # unchanged, and REQUIRED first
    bedrock = spend_controls.client_for(request.jwt, verified_identity=user.sub)  # new
    try:
        return bedrock.converse(modelId=MODEL, messages=request.messages)  # unchanged
    except UserBlockedError as exc:
        # The common refusal: metering blocked the user before this request.
        # resets_at is None for admin blocks (no window to wait for).
        when = f"; resets at {exc.resets_at:%Y-%m-%d %H:%M} UTC" if exc.resets_at else ""
        return http_403(f"Bedrock access is blocked for this account{when}")
    except QuotaExceededError as exc:
        return http_429(
            f"{exc.breached_period} {exc.breached_dimension} quota exhausted; "
            f"resets at {exc.resets_at:%Y-%m-%d %H:%M} UTC"
        )
    except (VendRateLimitedError, EmergencyStopError) as exc:
        return http_503(retry_after=exc.retry_after)
    except AuthenticationError:
        return http_401("Sign in again")
```

`client_for` returns an ordinary boto3 `bedrock-runtime` client. Converse,
InvokeModel, streaming, CountTokens, the OpenAI-compatible endpoints: all of
it works unchanged because only the credential object underneath is
different.

**Pass only verified tokens.** `client_for` reads the identity claim from
the JWT *without* checking its signature, because the cache key has to be
known before any broker call; the broker verifies the token again when it
vends. An unverified token naming another user would therefore be handed
that user's cached client until the next renewal. Verify the token in your
authentication layer first, and either pass the identity you established as
`verified_identity=` (a token whose claim differs raises `ValueError`) or
use `client_for_identity(identity, jwt)`.

### What the factory does for you

`BedrockSpendControls` keeps a small in-memory table: one entry per quota
identity currently active in this process. Each entry holds a
`QuotaBrokerCredentialProvider` (one broker lease), the boto3 client built on
it, and the newest JWT seen for that identity.

1. **First call for a user.** `client_for(jwt)` reads the identity claim from
   the JWT (without verifying it; see above), creates the entry outside the
   factory lock, and returns the client. No network call yet. One boto3
   session and one `httpx.Client` (pass your own with `http_client=`) are
   shared by every identity; `close()` releases them.
2. **First Bedrock call.** boto3 asks the provider for credentials. The
   provider `POST`s to `{BrokerApiUrl}/v1/credentials`, SigV4-signed with
   **your backend's role** (the Function URL is `AWS_IAM`; only roles in
   `invoker_principal_arns` may call it), with the user's JWT in the
   `X-Quota-User-Token` header and a client-generated `X-Quota-Lease-Id`.
   Each attempt has a 2 s connect / 5 s read timeout; transport errors and
   non-terminal broker errors are retried up to three attempts in total with
   backoff, so a dead broker costs about 22 s worst case before the call
   fails.
3. **Broker decision.** It verifies the JWT against the issuer's JWKS, loads
   the user's row, and if the user is active and under every enabled quota it
   assumes `BedrockUserRole` with the user's identity as `SourceIdentity` and
   a session policy that expires in 1, 5, or 15 minutes (the runtime lease
   dial). It returns the STS keys, `expiration` (the lease deadline),
   `sts_expiration` (when the keys physically die), and `refresh_after`.
4. **Steady state.** Every Bedrock call by that user reuses the cached keys.
   There is no broker call per inference. When `refresh_after` passes, the
   next Bedrock call triggers a renewal under a **new** lease ID with the
   **latest JWT** the factory has seen for that identity, so a user whose
   session refreshed its ID token keeps working. A lease ID is reused only to
   retry a request whose response was lost, and such a retry always returns
   the original deadline.
5. **Refusal.** When the user is over quota or blocked, the renewal fails
   with a typed error (table below) and no credentials are returned. The
   Bedrock call that triggered it raises that error. A terminal refusal is
   remembered per identity for up to `refusal_cache_seconds` (default 30,
   shortened to the broker's `Retry-After`, `refresh_after`, or `resets_at`
   when earlier), so a burst of requests from a blocked user costs one broker
   call, not one per request. Clock skew between your host and the broker is
   estimated from the broker's `Date` header (`provider.clock_offset`) and
   applied to every server timestamp.
6. **Cleanup.** Identities idle for `idle_ttl_seconds` (default one hour) are
   dropped, and the table is capped at `max_users` (default 1,000, least
   recently used first; each cached identity costs about 0.25 MiB). Call
   `spend_controls.forget(jwt_or_identity)` on sign-out if you want it
   immediate.

### Using an authentication middleware

Most backends validate the JWT in one place. Bind the user there, **after**
verification, and keep the JWT out of every call site:

```python
# middleware (FastAPI shown; any framework with per-request context works)
@app.middleware("http")
async def bind_quota_user(request, call_next):
    raw = request.headers.get("authorization", "")[7:]
    claims = verify_jwt(raw)                 # signature, issuer, audience, expiry: reject here
    token = BedrockSpendControls.set_current_user(raw)   # bind only a verified token
    try:
        return await call_next(request)
    finally:
        BedrockSpendControls.reset_current_user(token)

# anywhere below
bedrock = spend_controls.client_for()      # reads the context variable
```

Binding an unverified header would let any caller choose whose cached client
they receive, so the `verify_jwt` call must come first and must raise on
failure.

If your code already obtains the Bedrock client from a single helper such as
`get_bedrock_client()`, replace its body with `return spend_controls.client_for()`
and no call site changes at all.

### Errors your code will see

| Exception | HTTP from broker | Meaning | What to do |
|---|---|---|---|
| `UserBlockedError` | 403 `quota_blocked` | Status is `blocked`. This is the usual refusal, because metering blocks the user between vends. For an automatic block `breached_period`, `breached_dimension`, and `resets_at` are filled in and the block lifts alone when the window resets; for an admin block they are `None` and an operator must act. | Tell the user; do not retry in a loop. |
| `QuotaExceededError` | 429 `quota_exceeded` | A `block` threshold was reached at vend time (subject period, rate minute, or model budget). `breached_period`, `breached_dimension`, `resets_at` are filled in; there is no `Retry-After`. | Tell the user; retry after `resets_at`. |
| `VendRateLimitedError` | 429 `lease_rate_limited` / `lease_not_refreshable` | More than `vend_rate_limit_per_minute` vends for this identity in one minute, across every process serving it. `lease_not_refreshable` is a rare concurrent-renewal race. | Usually a provider per request instead of per user, or more replicas per identity than the vend budget allows (see below). Honour `retry_after` / `refresh_after`. |
| `AuthenticationError` | 401 `authentication_error` | The broker rejected the JWT (expired, wrong issuer or audience, missing claim, `#` in the claim) or the user is not provisioned and auto-provisioning is off. | Re-authenticate the user; for unprovisioned users, create them through the admin API. |
| `EmergencyStopError` | 503 `emergency_stop` | An operator closed vending. Not terminal: `retry_after` is 60 s and the next refresh retries. | Surface as temporarily unavailable; do not page your own on-call, the operators already know. |
| `BrokerCredentialError` (base) | 5xx, network | Broker unavailable. The provider already made three attempts with backoff. | Surface as service unavailable. |
| `botocore` `ClientError` `AccessDeniedException` | none (from Bedrock) | Either the model is not in `allowed_model_arns` (or is an application inference profile, a provisioned model, or an unmetered API, which vended sessions are always denied), or the user was blocked **while holding valid keys** and the revocation layer cut the session. | Check the model first. Otherwise treat as blocked: the credentials stay cached until the lease's refresh window, and the renewal then returns `UserBlockedError`. Call `spend_controls.forget(jwt)` to force an immediate re-vend if you want the typed error now. |
| `botocore` `ClientError` `ExpiredTokenException` | none | The permission lease deadline passed before boto3 refreshed (clock skew, long pause). | Retry once; boto3 refreshes on the retry. |

All broker errors subclass `botocore.exceptions.CredentialRetrievalError`, so
existing `except botocore.exceptions.BotoCoreError` handlers still catch them.
Every `BrokerCredentialError` carries `.status_code`, `.error_type`,
`.message`, `.terminal`, and the optional `.retry_after` (seconds),
`.refresh_after`, `.resets_at` (UTC `datetime` or `None`),
`.breached_period`, and `.breached_dimension`; guard the optional ones before
formatting them.

### Multiple instances, threads, async

- **Several pods or containers.** Each identity has **one** logical lease at
  the broker, shared by every process that serves it. The first process to
  vend starts the lease; any other process that vends for the same identity
  before `refresh_after` **joins** it and receives its own STS keys with the
  same deadline (the response's `lease_id` is the shared lease, not the ID
  it sent). When the refresh window opens, the first process to renew starts
  the next generation and the others join that one. No process can extend
  the identity's deadline, so the overspend bound is unchanged however many
  replicas you run.
- **Threads.** The factory and the provider are thread-safe. botocore
  single-flights the refresh, so concurrent first calls by the same user make
  one broker request.
- **Async frameworks.** boto3 is synchronous; use it as you do today. The
  context variable follows `asyncio` tasks correctly.
- **Vend budget.** The broker allows a small number of vends per identity per
  minute (`vend_rate_limit_per_minute`, default 6), counted across all
  processes. One factory per process keeps each process at about one vend per
  lease window, but at each renewal every process serving the identity vends
  within a few seconds of the others. Set `vend_rate_limit_per_minute` to at
  least the number of replicas that can serve one identity at once (plus
  headroom for retries); this matters most with a tenant or team claim.
  Creating a provider per request would hit the limit on the sixth request.

### Identity claim

`identity_claim` must equal the deployment's `jwt_user_claim`. With `sub`,
each user is a quota identity. With a tenant or team claim, all members share
one quota **and** one cached lease per process, which is what you want: the
vend budget above is per identity.

### Users that do not exist yet

With `auto_provision_users: true` the first vend creates the user with the
deployment's `default_limits`. With `false` (recommended in production) the
broker returns `401 authentication_error` (`AuthenticationError`) until an
operator creates the user through the admin UI or `POST /admin/users`; wire
that into your own onboarding if you need it automatic.

### Reimplementing the provider in another language

The reference implementation is Python, but the contract is small. Any AWS
SDK accepts a custom refreshable credential provider.

**Request.** `POST {BrokerApiUrl}/v1/credentials`, empty body, SigV4-signed
for service `lambda` in the stack Region using the backend's own credentials,
headers `X-Quota-User-Token: <user JWT>` and `X-Quota-Lease-Id: <client UUID>`.

**Success (200).**

```json
{
  "aws_access_key_id": "...", "aws_secret_access_key": "...", "aws_session_token": "...",
  "expiration": "2026-09-23T10:05:00+00:00",
  "sts_expiration": "2026-09-23T10:15:00+00:00",
  "refresh_after": "2026-09-23T10:04:45+00:00",
  "lease_id": "…", "lease_generation": 3,
  "region": "us-east-1", "endpoint": "https://bedrock-runtime.us-east-1.amazonaws.com",
  "user_id": "…"
}
```

Use `expiration` as the credential expiry, not `sts_expiration`. Generate a
fresh `lease_id` for every renewal: cache until `refresh_after`, then renew
with a **new** UUID. Reuse an ID only to retry a request whose outcome you
did not see (5xx or transport error); a same-ID retry returns the original
deadline and never extends it. On `409 lease_expired` generate a new
`lease_id` and retry once. The response's `lease_id` may differ from the one
you sent when another process already holds the identity's lease; do not
reuse it for renewal.

**Refusals.** `401 authentication_error`, `403 quota_blocked` (automatic
blocks carry `X-Quota-Breached-Period`, `X-Quota-Breached-Dimension`,
`X-Quota-Resets-At`; admin blocks do not), `429 quota_exceeded` (the same
three headers, no `Retry-After`), `429 lease_rate_limited` /
`lease_not_refreshable` (`Retry-After`, `X-Quota-Refresh-After`), `503
quota_state_conflict` (`Retry-After: 1`), `503 emergency_stop`
(`Retry-After: 60`). Bodies are `{"error": {"type": "...", "code": "...",
"message": "..."}}`. Treat 401/403/409/429 as final for this attempt; retry
only 5xx and transport errors, with backoff. The broker's `Date` header is
the reference clock for every timestamp it returns.

### What the vended credentials cannot do

They can call only the Runtime actions (`InvokeModel`,
`InvokeModelWithResponseStream`, `CountTokens`) on the models in
`allowed_model_arns`, for the lease duration. They are explicitly denied
application inference profiles (the workload path), provisioned models, and
the unmetered `StartAsyncInvoke` / `InvokeModelWithBidirectionalStream`
APIs. They cannot list models, call other services, or obtain Bedrock bearer
tokens. Do not try to reuse them for anything else.

---

## Per-workload integration

### When

Your process calls Bedrock with its own IAM role and there is no end user
behind the call: nightly batches, ETL, internal agents, evaluation jobs. There
is no JWT to present, so there is no vend. Attribution is done by **which
application inference profile** the call goes through, and enforcement is an
IAM Deny on your role.

### What changes in your code

One string.

```python
# before
bedrock.converse(modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0", messages=...)

# after
bedrock.converse(modelId=os.environ["BEDROCK_PROFILE_ARN"], messages=...)
# BEDROCK_PROFILE_ARN = arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/abc123
```

Nothing else. Same client, same role, same code. Streaming and Converse
accept inference-profile ARNs as `modelId`.

### What you need from the stack operator

Give them your workload's name, the model you use, and the ARN of the role
your process runs with. They register it in the deployment
([DEPLOYMENT.md § Per-workload quotas](../DEPLOYMENT.md#per-workload-quotas))
and hand you back the `WorkloadProfileArn<Name>` output: the ARN of an
application inference profile created for your workload alone. The stack
also attaches to your role a policy that allows invoking **that profile
only**. Put the ARN in your deployment configuration.

If your role's own policy already allows the foundation model directly,
remove that allow: the quota applies only to calls made through the
profile, so a direct allow makes the budget unenforceable.

### What you will observe

- **Normal operation.** Nothing. Calls go straight to Bedrock as before.
- **Budget exhausted.** Bedrock returns `AccessDeniedException` within
  seconds of the metering processor blocking the workload (an inline
  `bedrock:InvokeModel*` Deny was attached to your role). Treat it as "budget
  exhausted until the window resets", not as a transient error: back off,
  checkpoint, and resume after 00:00 UTC (daily), Monday 00:00 UTC (weekly),
  or the first of the month. Do not retry in a tight loop.
- **Window reset.** The workload enforcer removes the Deny once every enabled
  period is under quota again; your next call succeeds. No action needed.
- **Alerts.** Warnings and the block itself are published to the stack's SNS
  topic (`WARNING workload:payments-batch …`, `BLOCKED workload:payments-batch
  …`). Subscribe your on-call channel if you want to know before your job
  fails.

### Without `role_arn`

If the operator cannot list your role (for example it lives in another
account), the stack emits the invoke policy as an output for you to attach by
hand, and the workload is **metered but not enforced**: it shows as "Metering
only" in the console, alerts fire, but nothing blocks. For a workload that can
spend real money, insist on providing `role_arn`.

### Limits

Same-account IAM roles only (synthesis rejects a `role_arn` from another
account, a wildcard, or a role shared by two workloads); about 1,000
workloads per account. There is no permission lease on this path, so the
cut-off after a block is metering lag plus IAM propagation, usually well
under a minute, with no in-flight deadline as a backstop.

---

## Checklist

**Per-user**

- [ ] Backend role ARN is in `invoker_principal_arns`.
- [ ] `identity_claim` equals the deployment's `jwt_user_claim`.
- [ ] One `BedrockSpendControls` per process; `client_for(jwt)` per request,
      called only with tokens your middleware has already verified
      (`verified_identity=` or `client_for_identity`).
- [ ] `UserBlockedError`, `QuotaExceededError`, `VendRateLimitedError`,
      `AuthenticationError`, `EmergencyStopError` mapped to user-facing
      responses (guard `resets_at` for `None`); `AccessDeniedException` from
      Bedrock treated as a mid-lease block.
- [ ] Users provisioned (auto or via admin API) before go-live.

**Per-workload**

- [ ] Workload listed in `workloads.json` **with** `role_arn`.
- [ ] `modelId` replaced by `WorkloadProfileArn<Name>`.
- [ ] `AccessDeniedException` handled as budget exhausted, with resume after
      the window resets.

**Both**

- [ ] An SCP or `DenyDirectBedrockPolicyArn` prevents any other principal in
      the account from calling Bedrock directly. Without it the quota can be
      bypassed.
