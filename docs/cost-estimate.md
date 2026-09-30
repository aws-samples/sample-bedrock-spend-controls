# Cost to run

**Region assumed:** `us-east-1`. **Prices:** public on-demand rates read from
the AWS Pricing API (`pricing:GetProducts`) on **2026-09-14**; every unit
price is in [`cost-estimate-assumptions.json`](cost-estimate-assumptions.json)
with that date, and the tables below are regenerated with
`cdk/.venv/bin/python tools/estimate_cost.py docs/cost-estimate-assumptions.json`.
This is an estimate of the *solution's own* running cost. It excludes the
Bedrock inference spend the solution meters, and it excludes any AWS free-tier
allowance beyond the always-free items noted per line.

## Assumptions

| | Demo | Production |
|---|---|---|
| Active quota subjects (users) | 100 | 1 000 |
| Metered Bedrock invocations / month | 10 000 | 1 000 000 |
| Hours per day a user is actively calling Bedrock | 1 | 4 |
| Permission lease (runtime dial) | 60 s (demo.json default) → one broker vend per active user every minute | 300 s → one broker vend per active user every ~5 min |
| Admin API calls / month (UI + CLI) | 2 000 | 20 000 |
| Invocation-log records per subscription batch | 1 | 3 |
| Subjects with rate limits / with model budgets (×2 models) | 0 % / 0 % | 50 % / 20 % |
| Workload-mode workloads | 0 | 3 |
| Reconciliation (`reconciliation_enabled`) | off | on |
| `usage_retention_days` | 35 | 90 |
| Distinct models invoked | 5 | 12 |
| Invocation-log record size | 700 B (measured on a real Converse record with payload delivery off) | same |
| EMF record size | 900 B | same |
| Admin UI page loads / month | 300 | 3 000 |
| Lambda durations | broker 350 ms, processor 250 ms, revocation 1.5 s | broker 300 ms, processor 350 ms, revocation 3 s (bigger scan) |

Schedule-driven invocations are read from the stack: emergency processor
every 1 min (43 200/month), revocation processor every
`revocation_reconcile_minutes` = 5 (8 640/month) plus one dispatch per
status change, workload enforcer every 5 min (when workloads exist), price
refresher daily, auto-block sweep nightly (one users-table scan plus one
2-item transaction per lifted row), reconciliation daily.

DynamoDB request units are derived from the code paths: the metering
transaction is 3 items (marker Put + subject ledger Update + model ledger
Update) and transactional writes bill at 2× → 6 WRU per invocation; strongly
consistent reads bill 1 RRU per 4 KB, counted here as 2 RRU per ledger
Query (≤ 37 rows) and 1 RRU per GetItem; each vend performs three
strongly consistent users-row reads, a ledger Query, and four small writes
(vend-rate counter, lease, `SESSION#` map, `source_identity`).


### Demo: 100 users, 10 000 invocations / month

| Line item | USD / month | Basis |
|---|---:|---|
| Lambda: Broker vends (BrokerApiFn) — 180,000 inv | 1.09 | 180,000 × (1024 MB, 350 ms) |
| Lambda: Admin API (BrokerApiFn) — 2,000 inv | 0.01 | 2,000 × (1024 MB, 250 ms) |
| Lambda: Usage processor — 10,000 inv | 0.01 | 10,000 × (256 MB, 250 ms) |
| Lambda: Enforcement dispatcher — 20 inv | 0.00 | 20 × (128 MB, 300 ms) |
| Lambda: Revocation processor (schedule + dispatch) — 8,840 inv | 0.06 | 8,840 × (256 MB, 1500 ms) |
| Lambda: Emergency processor (1-min schedule) — 43,200 inv | 0.08 | 43,200 × (256 MB, 400 ms) |
| Lambda: Workload enforcer (5-min schedule) — 0 inv | 0.00 | 0 × (256 MB, 600 ms) |
| Lambda: Price refresher (daily) — 30 inv | 0.00 | 30 × (256 MB, 8000 ms) |
| Lambda: Auto-block sweep (nightly) — 30 inv | 0.00 | 30 × (256 MB, 2000 ms) |
| Lambda: Reconciliation (daily) — 0 inv | 0.00 | 0 × (256 MB, 3000 ms) |
| DynamoDB writes — 0.8 M WRU | 0.50 | on-demand, transactions billed 2x |
| DynamoDB reads — 1.4 M RRU | 0.17 | on-demand, strongly consistent |
| DynamoDB storage — 0.42 GB | 0.00 | first 25 GB free |
| DynamoDB Streams — 400 reads | 0.00 | first 2.5 M free |
| CloudWatch Logs ingest: Bedrock invocation logs — 0.01 GB | 0.00 | 700 B/record (vended log) |
| CloudWatch Logs storage: invocation logs — 0.00 GB-mo | 0.00 | 14-day retention (stack default) |
| CloudWatch Logs ingest: Lambda/EMF logs — 0.08 GB | 0.04 | 900 B per EMF record |
| CloudWatch custom metrics — 1,205 metric-months | 361.50 | EMF; per-UserId dimensions dominate |
| CloudWatch alarms — 8 | 0.80 | standard resolution |
| CloudWatch dashboard — 1 | 0.00 | first 3 dashboards free |
| CloudWatch GetMetricData — 18,000 metrics | 0.18 | Operations/Overview tabs |
| SNS — email notifications | 0.00 | first 1 000 email deliveries/month free |
| SQS — 2 DLQs | 0.00 | first 1 M requests free; idle unless failures |
| Secrets Manager — 2 secrets | 0.80 | admin + emergency keys |
| SSM Parameter Store — 2 parameters (prices, workload roster) | 0.00 | standard tier, no charge; the roster uses intelligent tiering and is billed as advanced ($0.05/month + API charges) only if it exceeds 4 KB, roughly 12+ workloads |
| STS AssumeRole | 0.00 | no charge |
| Cost Explorer API — 0 calls (reconciliation) | 0.00 | $0.01 per request; 1 + workloads per daily run |
| CloudFront — 4,500 HTTPS requests | 0.00 | admin UI static assets; data transfer negligible |
| S3 — admin UI bucket | 0.01 | <1 GB |
| Cognito user pool — demo IdP | 0.00 | <10 k MAU free |
| **Total** | **365.25** | |


### Production: 1 000 users, 1 000 000 invocations / month, 300 s lease
| Line item | USD / month | Basis |
|---|---:|---|
| Lambda: Broker vends (BrokerApiFn) — 1,440,000 inv | 7.49 | 1,440,000 × (1024 MB, 300 ms) |
| Lambda: Admin API (BrokerApiFn) — 20,000 inv | 0.09 | 20,000 × (1024 MB, 250 ms) |
| Lambda: Usage processor — 333,333 inv | 0.55 | 333,333 × (256 MB, 350 ms) |
| Lambda: Enforcement dispatcher — 500 inv | 0.00 | 500 × (128 MB, 300 ms) |
| Lambda: Revocation processor (schedule + dispatch) — 13,640 inv | 0.17 | 13,640 × (256 MB, 3000 ms) |
| Lambda: Emergency processor (1-min schedule) — 43,200 inv | 0.08 | 43,200 × (256 MB, 400 ms) |
| Lambda: Workload enforcer (5-min schedule) — 8,640 inv | 0.02 | 8,640 × (256 MB, 600 ms) |
| Lambda: Price refresher (daily) — 30 inv | 0.00 | 30 × (256 MB, 8000 ms) |
| Lambda: Auto-block sweep (nightly) — 30 inv | 0.00 | 30 × (256 MB, 2000 ms) |
| Lambda: Reconciliation (daily) — 30 inv | 0.00 | 30 × (256 MB, 3000 ms) |
| DynamoDB writes — 12.9 M WRU | 8.07 | on-demand, transactions billed 2x |
| DynamoDB reads — 17.1 M RRU | 2.14 | on-demand, strongly consistent |
| DynamoDB storage — 8.50 GB | 0.00 | first 25 GB free |
| DynamoDB Streams — 10,000 reads | 0.00 | first 2.5 M free |
| CloudWatch Logs ingest: Bedrock invocation logs — 0.70 GB | 0.35 | 700 B/record (vended log) |
| CloudWatch Logs storage: invocation logs — 0.33 GB-mo | 0.01 | 14-day retention (stack default) |
| CloudWatch Logs ingest: Lambda/EMF logs — 1.48 GB | 0.74 | 900 B per EMF record |
| CloudWatch custom metrics — 11,175 metric-months | 3,117.50 | EMF; per-UserId dimensions dominate |
| CloudWatch alarms — 10 | 1.00 | standard resolution |
| CloudWatch dashboard — 1 | 0.00 | first 3 dashboards free |
| CloudWatch GetMetricData — 180,000 metrics | 1.80 | Operations/Overview tabs |
| SNS — email notifications | 0.00 | first 1 000 email deliveries/month free |
| SQS — 2 DLQs | 0.00 | first 1 M requests free; idle unless failures |
| Secrets Manager — 2 secrets | 0.80 | admin + emergency keys |
| SSM Parameter Store — 2 parameters (prices, workload roster) | 0.00 | standard tier, no charge; the roster uses intelligent tiering and is billed as advanced ($0.05/month + API charges) only if it exceeds 4 KB, roughly 12+ workloads |
| STS AssumeRole | 0.00 | no charge |
| Cost Explorer API — 120 calls (reconciliation) | 1.20 | $0.01 per request; 1 + workloads per daily run |
| CloudFront — 45,000 HTTPS requests | 0.04 | admin UI static assets; data transfer negligible |
| S3 — admin UI bucket | 0.01 | <1 GB |
| Cognito user pool — demo IdP | 0.00 | <10 k MAU free |
| **Total** | **3,142.07** | |

## What dominates and how to reduce it

**CloudWatch custom metrics are the cost of this design — roughly 90 % of
the total in both scenarios.** Every EMF metric is billed per distinct
*(metric, dimension-set)* stream that receives data in a month. The usage
processor publishes six quota metrics on three dimension sets
(`[UserId]`, `[Model]`, `[]`) and the broker publishes five lease metrics
on `[UserId]` and `[]`, so **each active user adds ~11 metric-streams per
month** (about $3.30 at the first-10 000 tier, $1.10 beyond it). The
per-`Model` and service-wide streams are a fixed few dozen. Everything else
combined — Lambda, DynamoDB, logs, alarms, secrets — is under $25/month for
1 000 users and 1 M invocations.

Options, in order of impact:

1. **Drop the `UserId` dimension from the metering metrics.** The DynamoDB
   ledger, not CloudWatch, is the canonical per-user quota source; the
   Overview tab's *top users by spend* is the only consumer of per-user
   EMF, and `GET /admin/users?include_usage=true` can serve it from DynamoDB
   instead. Removing `["UserId"]` from `_emit_emf` in
   `usage_processor/handler.py` and from `gateway/app/emf.py` cuts the
   production estimate from ~$3 100 to ~$50/month. The dashboard's
   `SEARCH('{BedrockSpendControls,UserId} ...')` widgets would need to
   switch to the `Model` dimension. This is the single change worth making
   before running at hundreds of users.
2. **Emit per-user metrics only for the top N spenders or on state change**
   (warn/block events), keeping per-user *event* visibility without a
   continuous stream per user.
3. **Publish `EstimatedCostUSD` per user only** (one stream instead of six)
   if per-user charting must stay.
4. **Use a shorter lease dial with care.** A 60 s lease quintuples broker
   vends (≈ $37/month of Lambda at 1 000 users × 4 h/day) and DynamoDB vend
   traffic; 300 s is the production default and the cost/latency balance
   assumed here. 900 s halves vend cost again at the price of a longer
   post-detection bound.
5. **DynamoDB is cheap and stays cheap.** Model-scoped ledger rows double
   ledger writes (~$4/month at 1 M invocations); strongly consistent reads
   are fractions of a cent per thousand. Storage stays in the free 25 GB
   until several million retained ledger rows.
6. **Invocation-log ingestion is negligible** with payload delivery disabled
   (700 B/record). Enabling `imageDataDeliveryEnabled` to price image
   generations ([pricing.md](pricing.md#priced-dimensions)) adds the image
   bytes to ingestion; budget for it before turning it on.
7. **Reconciliation costs $1.20/month** with three workloads (Cost Explorer
   API, $0.01/request, `1 + workloads` requests per daily run = 120/month).
   The broker never calls Cost Explorer; it reads stored runs. Leave it off
   in demo accounts.

What is *not* here: Bedrock inference itself (the thing being metered),
CloudTrail (not used), NAT/VPC (none — all Lambdas are VPC-less),
KMS (default encryption only), and data transfer out (the admin UI is a
few hundred KB per load).
