#!/usr/bin/env python3
"""Recompute docs/cost-estimate.md tables from a YAML/JSON assumptions file.

    cdk/.venv/bin/python tools/estimate_cost.py docs/cost-estimate-assumptions.json

Prints Markdown tables for every scenario in the file. Unit prices live in
the assumptions file too (with their source and date) so a reviewer can
refresh them from the AWS Pricing API without touching this code. The model
is deliberately simple arithmetic — every formula is visible below — and is
an *estimate*, not a bill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SECONDS_PER_MONTH = 30 * 24 * 3600


def _lambda(prices: dict, invocations: float, mb: int, ms: float) -> tuple[float, float]:
    requests = invocations * prices["lambda_request"]
    gb_seconds = invocations * (mb / 1024) * (ms / 1000)
    return requests, gb_seconds * prices["lambda_gb_second"]


def scenario(name: str, a: dict, p: dict) -> list[tuple[str, float, str]]:
    rows: list[tuple[str, float, str]] = []
    inv = a["invocations_per_month"]
    users = a["active_users"]
    # Vends: each active user refreshes once per lease while active.
    vends = users * a["active_hours_per_day"] * 3600 / a["lease_seconds"] * 30
    admin_calls = a["admin_api_calls_per_month"]

    # --- Lambda -----------------------------------------------------------
    for label, count, mb, ms in (
        ("Broker vends (BrokerApiFn)", vends, 1024, a["broker_ms"]),
        ("Admin API (BrokerApiFn)", admin_calls, 1024, a["admin_ms"]),
        ("Usage processor", inv / a["records_per_log_batch"], 256, a["processor_ms"]),
        ("Enforcement dispatcher", a["status_changes_per_month"] / 10, 128, 300),
        ("Revocation processor (schedule + dispatch)", SECONDS_PER_MONTH / (a["revocation_reconcile_minutes"] * 60) + a["status_changes_per_month"], 256, a["revocation_ms"]),
        ("Emergency processor (1-min schedule)", SECONDS_PER_MONTH / 60, 256, 400),
        ("Workload enforcer (5-min schedule)", (SECONDS_PER_MONTH / 300 if a["workloads"] else 0), 256, 600),
        ("Price refresher (daily)", 30, 256, 8000),
        ("Auto-block sweep (nightly)", 30, 256, 2000),
        ("Reconciliation (daily)", 30 if a["reconciliation_enabled"] else 0, 256, 3000),
    ):
        req, dur = _lambda(p, count, mb, ms)
        rows.append((f"Lambda: {label} — {count:,.0f} inv", req + dur, f"{count:,.0f} × ({mb} MB, {ms} ms)"))

    # --- DynamoDB on-demand ----------------------------------------------
    # Per metered invocation: 1 SESSION# GetItem, 1 transaction (3 items ->
    # billed as 2x WRU each = 6 WRU), 1 users GetItem + 1 rate UpdateItem
    # (rate-limited subjects only), evaluation reads: users GetItem + subject
    # ledger Query (<=37 rows, strongly consistent = 1 RRU per 4 KB, counted
    # as 2 RRU) + per model budget Query.
    model_budget_share = a["share_of_subjects_with_model_budgets"]
    rate_share = a["share_of_subjects_with_rate_limits"]
    wru = inv * (6 + 2 * rate_share)
    rru = inv * (1 + 1 + 2 + 2 * model_budget_share * a["model_budgets_per_subject"])
    # Warn/block writes are rare relative to invocations.
    wru += a["status_changes_per_month"] * 4
    # Per vend: users GetItem x3 (strongly consistent), VEND# UpdateItem,
    # lease UpdateItem, SESSION# PutItem, source_identity UpdateItem,
    # ledger Query, rate GetItem.
    rru += vends * (3 + 2 + rate_share)
    wru += vends * 4
    # Admin API: ~4 reads + 3 transactional writes per mutation on average.
    rru += admin_calls * 4
    wru += admin_calls * 6
    # Revocation scan: every users row per reconcile pass.
    rru += (SECONDS_PER_MONTH / (a["revocation_reconcile_minutes"] * 60)) * users * 0.5
    # Nightly auto-block sweep: one filtered scan of the users table (scan
    # RRUs are charged on rows read, not returned) plus a re-evaluation
    # Query and a 2-item transaction (2x WRU) per blocked row lifted.
    rru += 30 * users * 0.5 + 30 * a["status_changes_per_month"] / 30 * 2
    wru += a["status_changes_per_month"] / 30 * 30 * 0.5 * 4
    rows.append((f"DynamoDB writes — {wru/1e6:,.1f} M WRU", wru * p["ddb_wru"], "on-demand, transactions billed 2x"))
    rows.append((f"DynamoDB reads — {rru/1e6:,.1f} M RRU", rru * p["ddb_rru"], "on-demand, strongly consistent"))
    storage_gb = (users * 2 * 0.002) + (inv * 3 * 0.0000005 * a["usage_retention_days"] / 30)  # ~2 KB per user row, ~0.5 KB per ledger row
    rows.append((f"DynamoDB storage — {storage_gb:,.2f} GB", max(0.0, storage_gb - 25) * p["ddb_storage_gb"], "first 25 GB free"))
    stream_reads = (a["status_changes_per_month"] + vends * 0) * 2  # two consumers
    rows.append((f"DynamoDB Streams — {stream_reads:,.0f} reads", max(0.0, stream_reads - 2_500_000) * p["ddb_stream_read"], "first 2.5 M free"))

    # --- CloudWatch --------------------------------------------------------
    log_gb = inv * a["invocation_log_bytes"] / 1e9
    rows.append((f"CloudWatch Logs ingest: Bedrock invocation logs — {log_gb:,.2f} GB", log_gb * p["cw_logs_ingest_gb"], f"{a['invocation_log_bytes']} B/record (vended log)"))
    rows.append((f"CloudWatch Logs storage: invocation logs — {log_gb * 14/30:,.2f} GB-mo", log_gb * 14 / 30 * p["cw_logs_storage_gb"], "14-day retention (stack default)"))
    emf_gb = inv * a["emf_bytes"] / 1e9 + vends * 400 / 1e9
    rows.append((f"CloudWatch Logs ingest: Lambda/EMF logs — {emf_gb:,.2f} GB", emf_gb * p["cw_logs_ingest_gb"], f"{a['emf_bytes']} B per EMF record"))
    # Custom metrics = distinct (metric, dimension-set) streams that received
    # data in the month. Usage processor: 6 metrics on [UserId], [Model], []
    # plus 4 pricing-observability metrics on [Model], [] only. Broker:
    # CredentialsVended, Throttles, LeaseStarted/Refreshed/Retried on
    # [UserId], []. Per-UserId streams scale with active users and dominate.
    metrics = (
        6 * (users + a["distinct_models"] + 1)
        + 4 * (a["distinct_models"] + 1)
        + 5 * (users + 1)
        + a["operational_metrics"]
    )
    tiered = min(metrics, 10_000) * p["cw_metric_first_10k"] + max(0, metrics - 10_000) * p["cw_metric_next_240k"]
    rows.append((f"CloudWatch custom metrics — {metrics:,.0f} metric-months", tiered, "EMF; per-UserId dimensions dominate"))
    rows.append((f"CloudWatch alarms — {a['alarms']}", a["alarms"] * p["cw_alarm"], "standard resolution"))
    rows.append(("CloudWatch dashboard — 1", 0.0 if a["dashboards_free_tier"] else 3.0, "first 3 dashboards free"))
    gmd = a["admin_ui_page_loads_per_month"] * a["gmd_metrics_per_page_load"]
    rows.append((f"CloudWatch GetMetricData — {gmd:,.0f} metrics", gmd * p["cw_gmd_metric"], "Operations/Overview tabs"))

    # --- Messaging, secrets, params, misc --------------------------------
    rows.append(("SNS — email notifications", 0.0, "first 1 000 email deliveries/month free"))
    rows.append(("SQS — 2 DLQs", 0.0, "first 1 M requests free; idle unless failures"))
    rows.append(("Secrets Manager — 2 secrets", 2 * p["secret_month"], "admin + emergency keys"))
    rows.append(("SSM Parameter Store — 2 parameters (prices, workload roster)", 0.0, "standard tier, no charge; the roster uses intelligent tiering and is billed as advanced ($0.05/month + API charges) only if it exceeds 4 KB, roughly 12+ workloads"))
    rows.append(("STS AssumeRole", 0.0, "no charge"))
    rows.append((f"Cost Explorer API — {30 * (1 + a['workloads']) if a['reconciliation_enabled'] else 0} calls (reconciliation)", (30 * (1 + a["workloads"])) * p["ce_request"] if a["reconciliation_enabled"] else 0.0, "$0.01 per request; 1 + workloads per daily run"))
    if a["admin_ui"]:
        ui_requests = a["admin_ui_page_loads_per_month"] * 15
        rows.append((f"CloudFront — {ui_requests:,.0f} HTTPS requests", ui_requests * p["cf_https_request"], "admin UI static assets; data transfer negligible"))
        rows.append(("S3 — admin UI bucket", 0.01, "<1 GB"))
        rows.append(("Cognito user pool — demo IdP", 0.0, "<10 k MAU free"))
    return rows


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("docs/cost-estimate-assumptions.json")
    data = json.loads(path.read_text())
    prices = data["unit_prices"]
    for name, assumptions in data["scenarios"].items():
        rows = scenario(name, assumptions, prices)
        total = sum(cost for _, cost, _ in rows)
        print(f"\n### {name}\n")
        print("| Line item | USD / month | Basis |")
        print("|---|---:|---|")
        for label, cost, basis in rows:
            print(f"| {label} | {cost:,.2f} | {basis} |")
        print(f"| **Total** | **{total:,.2f}** | |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
