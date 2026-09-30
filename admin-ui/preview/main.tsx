// Local preview harness: NOT part of the production build (excluded via
// tsconfig / not imported by main.tsx). Mounts the full authenticated
// Dashboard against an in-memory fake broker so the UI can be reviewed
// without a deployment or an identity provider. Edits made in the UI persist
// for the life of the page.
import { createRoot } from "react-dom/client";
import "../src/styles.css";
import { Dashboard } from "../src/App";
import {
  ApiError,
  api,
  type AdminUser,
  type AuditEvent,
  type CurrentUsage,
  type QuotaLimits,
  type QuotaPeriod,
  type RateLimits,
  type UserRow,
  type WorkloadIdentity,
} from "../src/api";

/* ------------------------------------------------------------------ data */

const NOW = new Date("2026-09-15T08:10:00Z");
const TODAY = "2026-09-15";
const MODELS = [
  "anthropic.claude-opus-4-7",
  "anthropic.claude-haiku-4-5-20251001-v1:0",
  "amazon.nova-pro-v1:0",
  "amazon.nova-canvas-v1:0",
];

function iso(d: Date) { return d.toISOString(); }
function daysAgo(n: number) { const d = new Date(NOW); d.setUTCDate(d.getUTCDate() - n); return d; }
function dateOnly(d: Date) { return d.toISOString().slice(0, 10); }

function usageRow(period: QuotaPeriod, cost: number, inTok: number, outTok: number, req: number, extra: Partial<Record<string, number>> = {}) {
  const starts = { daily: "2026-09-15", weekly: "2026-09-14", monthly: "2026-09-01" };
  const ends = { daily: "2026-09-16", weekly: "2026-09-21", monthly: "2026-10-01" };
  return {
    period, window: starts[period],
    window_start: `${starts[period]}T00:00:00+00:00`, window_end: `${ends[period]}T00:00:00+00:00`, resets_at: `${ends[period]}T00:00:00+00:00`,
    cost_usd: cost, input_tokens: inTok, output_tokens: outTok, requests: req,
    cache_read_tokens: Math.round(inTok * 0.35), cache_write_tokens: Math.round(inTok * 0.08), images: 0, unpriced_requests: 0, ...extra,
  };
}
function usage(dailyCost: number, scale = 1): CurrentUsage {
  return {
    daily: usageRow("daily", dailyCost, 120_000 * scale, 18_000 * scale, 140 * scale),
    weekly: usageRow("weekly", dailyCost * 2.3, 280_000 * scale, 41_000 * scale, 330 * scale),
    monthly: usageRow("monthly", dailyCost * 9.1, 1_090_000 * scale, 160_000 * scale, 1_280 * scale),
  };
}

type Row = UserRow & { model_budgets?: Record<string, QuotaLimits>; rate?: RateLimits | null };

const WORKLOAD_ROSTER: Record<string, WorkloadIdentity> = {
  "workload:payments": { workload_id: "workload:payments", name: "payments", model: "us.anthropic.claude-haiku-4-5-20251001-v1:0", profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/km20esrc3ebe", role_arn: "arn:aws:iam::123456789012:role/payments-batch", enforcement_ready: true, registered: true, tag: { key: "bedrock-spend-controls-workload", value: "payments" } },
  "workload:reports": { workload_id: "workload:reports", name: "reports", model: "us.amazon.nova-pro-v1:0", profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/23cw7puqnobh", role_arn: null, enforcement_ready: false, registered: true, tag: { key: "bedrock-spend-controls-workload", value: "reports" } },
  "workload:nightly-etl": { workload_id: "workload:nightly-etl", name: "nightly-etl", model: "us.amazon.nova-micro-v1:0", profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/9x8y7z6w5v4u", role_arn: "arn:aws:iam::123456789012:role/nightly-etl", enforcement_ready: true, registered: true, tag: { key: "bedrock-spend-controls-workload", value: "nightly-etl" } },
};

const users: Row[] = [
  {
    user_id: "tenant/alice", name: "Alice Ferrante", status: "active", status_reason: "User created", status_origin: "admin", granularity: "user", version: 4,
    created_at: iso(daysAgo(40)), updated_at: iso(daysAgo(2)),
    limits: {
      daily: { usd: 25, input_tokens: 2_000_000, output_tokens: 400_000, thresholds: [{ at: 0.5, action: "warn" }, { at: 0.8, action: "warn" }, { at: 1, action: "block" }] },
      weekly: { usd: 120, input_tokens: 0, output_tokens: 0 },
      monthly: null,
    },
    rate: { rpm: 60, tpm: 400_000 },
    model_budgets: { "anthropic.claude-opus-4-7": { daily: { usd: 15, input_tokens: 0, output_tokens: 0 }, weekly: null, monthly: null } },
    today: { cost_usd: 18.42, input_tokens: 1_240_000, output_tokens: 210_000, requests: 812 },
    current_usage: usage(18.42, 8),
  },
  {
    user_id: "tenant/bob", name: "Bob Moreau", status: "blocked", status_reason: "auto: daily usd limit reached (block at 100%)", status_origin: "automatic", granularity: "user", version: 7,
    created_at: iso(daysAgo(60)), updated_at: iso(daysAgo(0)),
    limits: { daily: { usd: 5, input_tokens: 500_000, output_tokens: 100_000 }, weekly: null, monthly: { usd: 80, input_tokens: 0, output_tokens: 0 } },
    today: { cost_usd: 5.12, input_tokens: 388_000, output_tokens: 61_000, requests: 244 },
    current_usage: usage(5.12, 3),
  },
  {
    user_id: "tenant/carla", name: "Carla Nwosu", status: "active", status_reason: "Alert-only budget for the data-science pilot", status_origin: "admin", granularity: "user", version: 2,
    created_at: iso(daysAgo(12)), updated_at: iso(daysAgo(12)),
    limits: { daily: { usd: 40, input_tokens: 0, output_tokens: 0, thresholds: [{ at: 0.8, action: "warn" }] }, weekly: null, monthly: null },
    today: { cost_usd: 33.9, input_tokens: 2_900_000, output_tokens: 380_000, requests: 1_410 },
    current_usage: usage(33.9, 14),
  },
  {
    user_id: "workload:payments", name: "payments", status: "blocked", status_reason: "auto: daily USD quota exhausted in " + TODAY, status_origin: "automatic", version: 7,
    created_at: iso(daysAgo(30)), updated_at: iso(daysAgo(0)),
    limits: { daily: { usd: 400, input_tokens: 0, output_tokens: 0 }, weekly: null, monthly: { usd: 9_000, input_tokens: 0, output_tokens: 0 } },
    today: { cost_usd: 412.1, input_tokens: 31_000_000, output_tokens: 4_100_000, requests: 22_000 },
    current_usage: usage(412.1, 180),
    granularity: "workload", enforcement_ready: true,
    workload: WORKLOAD_ROSTER["workload:payments"],
  },
  {
    user_id: "workload:reports", name: "reports", status: "active", status_reason: "Workload created", status_origin: "automatic", version: 1,
    created_at: iso(daysAgo(30)), updated_at: iso(daysAgo(30)),
    limits: { daily: { usd: 150, input_tokens: 0, output_tokens: 0 }, weekly: null, monthly: null },
    today: { cost_usd: 96.3, input_tokens: 7_000_000, output_tokens: 900_000, requests: 5_100 },
    current_usage: usage(96.3, 40),
    granularity: "workload", enforcement_ready: false,
    workload: WORKLOAD_ROSTER["workload:reports"],
  },
  ...Array.from({ length: 9 }, (_, i) => {
    const cost = +(0.4 + i * 1.7).toFixed(2);
    return {
      user_id: `tenant/user-${String(i + 4).padStart(2, "0")}`, name: `Team member ${i + 4}`, status: "active" as const,
      status_reason: "User created", status_origin: "admin", granularity: "user" as const, version: 1,
      created_at: iso(daysAgo(20 - i)), updated_at: iso(daysAgo(20 - i)),
      limits: { daily: { usd: 10, input_tokens: 1_000_000, output_tokens: 200_000 }, weekly: null, monthly: null },
      today: { cost_usd: cost, input_tokens: 40_000 * (i + 1), output_tokens: 6_000 * (i + 1), requests: 30 * (i + 1) },
      current_usage: usage(cost, i + 1),
    } as Row;
  }),
];

const audit: AuditEvent[] = [];
function snapshot(u: Row) {
  const period = (p: QuotaLimits[QuotaPeriod]) => p && {
    usd_micro: Math.round(p.usd * 1_000_000), input_tokens: p.input_tokens, output_tokens: p.output_tokens,
    ...(p.thresholds ? { thresholds: p.thresholds.map((t) => ({ at_bps: Math.round(t.at * 10_000), action: t.action })) } : {}),
  };
  return {
    user_id: u.user_id, name: u.name, status: u.status, status_reason: u.status_reason, status_origin: u.status_origin, version: u.version,
    created_at: u.created_at, updated_at: u.updated_at,
    limits: { daily: period(u.limits.daily), weekly: period(u.limits.weekly), monthly: period(u.limits.monthly) },
    rate: u.rate ?? null,
  };
}
function record(u: Row, before: Row | null, event_type: string, reason: string, actor = "admin@example.test") {
  audit.unshift({
    user_id: u.user_id, event_key: `${Date.now()}#${Math.random().toString(16).slice(2, 8)}`, event_type, actor, auth_method: "oidc",
    reason, request_id: crypto.randomUUID(), created_at: new Date().toISOString(),
    before: before ? snapshot(before) : null, after: snapshot(u),
  });
}
// Seed a little history.
{
  const a = users[0], b = users[1];
  record({ ...a, version: 1, limits: { daily: { usd: 10, input_tokens: 1_000_000, output_tokens: 200_000 }, weekly: null, monthly: null } }, null, "user.created", "Onboarding", "system");
  record({ ...a, version: 2 }, { ...a, version: 1 }, "user.limits.updated", "Raised for the Q3 pilot");
  record({ ...a, version: 3 }, { ...a, version: 2 }, "user.model_budget.updated", "Cap Opus spend at $15/day");
  record(b, { ...b, status: "active", status_reason: "User created", version: 6 }, "user.status.updated", "auto: daily usd limit reached (block at 100%)", "usage-processor");
  audit.forEach((e, i) => { e.created_at = iso(daysAgo(i * 3 + 1)); });
}

let enforcement = { permission_lease_seconds: 300, source: "runtime", generation: 3, actor: "admin@example.test", reason: "Tighter lease during the pilot", updated_at: iso(daysAgo(5)), valid_permission_lease_seconds: [60, 300, 900], default_permission_lease_seconds: 300 };
let emergency = { state: "inactive", desired_active: false, generation: 0, applied_generation: 0, requested_at: null as string | null, applied_at: null as string | null, converged: true };

const mode = new URLSearchParams(location.search).get("recon") ?? "on";
// ?sweep=ok (default) | failed | never — the nightly auto-block sweep card.
const sweepMode = new URLSearchParams(location.search).get("sweep") ?? "ok";
// Lifted and failed ids are users from the fixture set above; bob stays blocked.
const sweepRun = { ran_at: iso(new Date(Date.UTC(NOW.getUTCFullYear(), NOW.getUTCMonth(), NOW.getUTCDate(), 0, 5, 4))), dry_run: false, evaluated: 3, lifted: 2, still_blocked: 1, admin_blocked: 0, raced: 0, lifted_users: ["tenant/user-05", "tenant/user-09"], failures: sweepMode === "failed" ? [{ user_id: "tenant/user-11", error: "ProvisionedThroughputExceededException" }] : [] };
const autoBlockSweep = { schedule: "00:05 UTC daily", status: sweepMode === "never" ? "never_ran" : sweepMode === "failed" ? "failed" : "ok", last_run: sweepMode === "never" ? null : sweepRun };
const reconRun = (day: string, est: number, billed: number, tagInactive: boolean) => ({
  day, run_at: `${dateOnly(new Date(new Date(day).getTime() + 2 * 86_400_000))}T06:00:12+00:00`, region: "us-east-1",
  service_names: ["Amazon Bedrock", "Amazon Bedrock Service"],
  aggregate: { estimated_usd: est, billed_usd: billed, delta_usd: +(billed - est).toFixed(6), delta_percent: billed ? +(((billed - est) / billed) * 100).toFixed(3) : null },
  workloads: [
    { workload_id: "workload:payments", name: "payments", estimated_usd: +(est * 0.5).toFixed(4), billed_usd: +(billed * 0.49).toFixed(4), delta_usd: +(billed * 0.49 - est * 0.5).toFixed(4), delta_percent: 1.9, tag_inactive: false },
    { workload_id: "workload:reports", name: "reports", estimated_usd: +(est * 0.12).toFixed(4), billed_usd: tagInactive ? 0 : +(billed * 0.12).toFixed(4), delta_usd: tagInactive ? -est * 0.12 : 0.2, delta_percent: tagInactive ? null : 0.4, tag_inactive: tagInactive },
  ],
  tag_inactive_workloads: tagInactive ? ["reports"] : [],
});
const reconRuns = Array.from({ length: 10 }, (_, i) => reconRun(dateOnly(daysAgo(2 + i)), 790 + i * 11, 840 + i * 13, mode === "tag" && i === 0));

/* ------------------------------------------------------------------ fake api */

const delay = (ms = 180) => new Promise((r) => setTimeout(r, ms));
const userRows = () => users.filter((u) => u.granularity !== "workload");
const workloadRows = () => users.filter((u) => u.granularity === "workload");
const sumToday = (rows: Row[]) => rows.reduce((acc, u) => ({ cost_usd: +(acc.cost_usd + u.today.cost_usd).toFixed(6), input_tokens: acc.input_tokens + u.today.input_tokens, output_tokens: acc.output_tokens + u.today.output_tokens, requests: acc.requests + u.today.requests }), { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 });
const wrap = <T,>(data: T, etag?: string) => ({ data, etag: etag ?? null, requestId: crypto.randomUUID(), status: 200 });
const publicUser = (u: Row): AdminUser => { const { today: _t, current_usage: _c, ...rest } = u; return rest; };
const find = (id: string) => { const u = users.find((x) => x.user_id === id); if (!u) throw new ApiError("User not found", 404, "not_found"); return u; };

Object.assign(api as Record<string, unknown>, {
  summary: async () => { await delay(); return {
    enforcement: {
      source: "dynamodb", as_of: new Date().toISOString(), window: TODAY, mode: "layered", credential_ttl_seconds: 900, permission_lease_seconds: enforcement.permission_lease_seconds,
      permission_lease_source: enforcement.source, refresh_overlap_seconds: 10, refresh_jitter_seconds: 5, vend_rate_limit_per_minute: 6, revocation_policy_shards: 19, revocation_reconcile_minutes: 5,
      total_users: users.length, blocked_users: users.filter((u) => u.status === "blocked").length, blocked_user_ids: users.filter((u) => u.status === "blocked").map((u) => u.user_id),
      today: sumToday(users),
      subjects: {
        users: { total: userRows().length, blocked: userRows().filter((u) => u.status === "blocked").length, today: sumToday(userRows()) },
        workloads: {
          total: workloadRows().length, blocked: workloadRows().filter((u) => u.status === "blocked").length, today: sumToday(workloadRows()),
          configured: Object.keys(WORKLOAD_ROSTER).length,
          metering_only: workloadRows().filter((u) => !WORKLOAD_ROSTER[u.user_id]?.enforcement_ready).length,
          unregistered: workloadRows().filter((u) => !WORKLOAD_ROSTER[u.user_id]).length,
          awaiting_traffic: Object.keys(WORKLOAD_ROSTER).filter((id) => !users.some((u) => u.user_id === id)).length,
        },
      },
    },
    observability: { source: "bedrock_model_invocation_logs", delivery: "cloudwatch_logs_subscription", metrics_namespace: "BedrockSpendControls", detection_lag_metric: "DetectionLagMilliseconds" },
  }; },

  operations: async () => { await delay(); return {
    as_of: new Date().toISOString(),
    configuration: { mode: "layered", credential_ttl_seconds: 900, permission_lease_seconds: enforcement.permission_lease_seconds, permission_lease_source: enforcement.source, permission_lease_default_seconds: 300, refresh_overlap_seconds: 10, refresh_jitter_seconds: 5, vend_rate_limit_per_minute: 6, revocation_policy_shards: 19, revocation_policy_max_characters: 6144, revocation_reconcile_minutes: 5 },
    emergency,
    metrics: { namespace: "BedrockSpendControls", detection_lag_metric: "DetectionLagMilliseconds", detection_lag_p95_ms: 1_840, detection_lag_timestamp: iso(new Date(NOW.getTime() - 60_000)), telemetry_status: "complete", last_reconciliation_at: iso(new Date(NOW.getTime() - 120_000)), reconciliation_status: "current", revoked_identities_desired: 1, recent_sync_failure_count: 0, recent_overflow_count: 0, recent_emergency_failure_count: 0, window_minutes: 15 },
    alarms: [
      { key: "enforcement_dispatch_dlq", state: "OK", updated_at: iso(daysAgo(1)) },
      { key: "enforcement_dispatch_iterator_age", state: "OK", updated_at: iso(daysAgo(1)) },
      { key: "revocation_sync_failure", state: "OK", updated_at: iso(daysAgo(3)) },
      { key: "revocation_policy_overflow", state: "OK", updated_at: iso(daysAgo(3)) },
      { key: "emergency_stop_failure", state: "OK", updated_at: iso(daysAgo(7)) },
      { key: "emergency_stop_dlq", state: "OK", updated_at: iso(daysAgo(7)) },
      { key: "workload_enforcement_failure", state: "OK", updated_at: iso(daysAgo(2)) },
      { key: "auto_block_sweep_failure", state: sweepMode === "failed" ? "ALARM" : sweepMode === "never" ? "INSUFFICIENT_DATA" : "OK", updated_at: sweepMode === "never" ? null : sweepRun.ran_at },
      { key: "pricing_fallback", state: "INSUFFICIENT_DATA", updated_at: null },
      ...(mode === "off" ? [] : [{ key: "reconciliation_delta", state: mode === "alarm" ? "ALARM" : "OK", updated_at: iso(new Date(NOW.getTime() - 2 * 3_600_000)) }]),
    ],
    cloudwatch: { status: "available" },
    auto_block_sweep: autoBlockSweep,
  }; },

  reconciliation: async (_c: unknown, _s: unknown, limit = 14) => { await delay(); return mode === "off"
    ? { enabled: false, runs: [], message: "Reconciliation is disabled for this deployment. Set reconciliation_enabled=true in the deployment config to compare the ledger against Cost Explorer daily." }
    : mode === "empty" ? { enabled: true, lag_days: 2, runs: [], latest: null }
    : { enabled: true, lag_days: 2, runs: reconRuns.slice(0, limit), latest: reconRuns[0] }; },

  usageMetrics: async (_c: unknown, _s: unknown, days: number) => { await delay(250);
    const dayList = Array.from({ length: days }, (_, i) => dateOnly(daysAgo(days - 1 - i)));
    const shape = (base: number, k: number) => dayList.map((_, i) => +(base * (0.6 + 0.4 * Math.sin(i / 2 + k)) * (i === dayList.length - 1 ? 0.35 : 1)).toFixed(4));
    const models = MODELS.map((model, k) => {
      const cost = shape([320, 60, 140, 22][k], k);
      const requests = shape([9_000, 24_000, 6_000, 400][k], k + 1).map(Math.round);
      const input = shape([26e6, 41e6, 12e6, 0][k], k + 2).map(Math.round);
      const output = shape([3.1e6, 5.2e6, 1.4e6, 0][k], k + 3).map(Math.round);
      const sum = (a: number[]) => +a.reduce((x, y) => x + y, 0).toFixed(4);
      return { model, series: { cost_usd: cost, requests, input_tokens: input, output_tokens: output }, totals: { cost_usd: sum(cost), requests: sum(requests), input_tokens: sum(input), output_tokens: sum(output) } };
    });
    const totals = { cost_usd: 0, requests: 0, input_tokens: 0, output_tokens: 0 };
    models.forEach((m) => { (Object.keys(totals) as (keyof typeof totals)[]).forEach((key) => { totals[key] = +(totals[key] + m.totals[key]).toFixed(4); }); });
    return { status: "available", as_of: new Date().toISOString(), start: dayList[0], end: dayList[dayList.length - 1], period: "daily", days: dayList, models, totals,
      top_users: [...users].sort((a, b) => b.today.cost_usd - a.today.cost_usd).slice(0, 8).map((u) => ({ user_id: u.user_id, name: u.name, cost_usd: +(u.today.cost_usd * 9.1).toFixed(4), requests: u.today.requests * 9, granularity: u.granularity })) };
  },

  getEnforcement: async () => { await delay(); return enforcement; },
  setEnforcement: async (_c: unknown, _s: unknown, seconds: number, reason?: string, expectedGeneration = 0) => { await delay();
    if (expectedGeneration !== enforcement.generation) throw new ApiError("Changed since you loaded it", 409, "version_conflict", { current: enforcement });
    enforcement = { ...enforcement, permission_lease_seconds: seconds, source: "runtime", generation: enforcement.generation + 1, actor: "admin@example.test", reason: reason ?? "", updated_at: new Date().toISOString() };
    return enforcement; },
  setEmergencyStop: async (_c: unknown, _s: unknown, req: { action: string; emergencyKey: string }) => { await delay(300);
    if (req.emergencyKey !== "break-glass") throw new ApiError("Emergency key rejected", 403, "forbidden");
    const activate = req.action === "activate";
    emergency = { state: activate ? "active" : "inactive", desired_active: activate, generation: emergency.generation + 1, applied_generation: emergency.generation + 1, requested_at: new Date().toISOString(), applied_at: new Date().toISOString(), converged: true };
    return { state: emergency.state, desired_active: activate, generation: emergency.generation, requested_at: emergency.requested_at }; },

  listUsersPage: async (_c: unknown, _s: unknown, opts: { limit?: number; cursor?: string | null; status?: string; query?: string; granularity?: "user" | "workload" } = {}) => { await delay();
    let rows = users.slice();
    if (opts.granularity === "user") rows = userRows();
    if (opts.granularity === "workload") rows = workloadRows();
    if (opts.status && opts.status !== "all") rows = rows.filter((u) => u.status === opts.status);
    if (opts.query) { const q = opts.query.toLowerCase(); rows = rows.filter((u) => u.user_id.toLowerCase().includes(q) || u.name.toLowerCase().includes(q)); }
    const limit = opts.limit ?? 25; const start = opts.cursor ? Number(opts.cursor) : 0; const page = rows.slice(start, start + limit);
    return { users: page.map((u) => ({ ...u, model_budgets: undefined })), next_cursor: start + limit < rows.length ? String(start + limit) : null }; },
  listWorkloads: async () => { await delay(); return {
    roster_source: "parameter_store", tag_key: "bedrock-spend-controls-workload",
    workloads: Object.values(WORKLOAD_ROSTER).map((identity) => { const row = users.find((u) => u.user_id === identity.workload_id); return { ...identity, subject: row ? { ...row, model_budgets: undefined } : null }; }),
  }; },
  leaseSnapshot: async () => { await delay(); return { users: [
    { ...publicUser(users[0]), lease: { active: true, expires_at: iso(new Date(Date.now() + 210_000)), refresh_after: iso(new Date(Date.now() + 190_000)), generation: 41, granted_at: iso(new Date(Date.now() - 90_000)), lease_seconds: 300 } },
    { ...publicUser(users[2]), lease: { active: true, expires_at: iso(new Date(Date.now() + 40_000)), refresh_after: null, generation: 12, granted_at: iso(new Date(Date.now() - 260_000)), lease_seconds: 300 } },
  ], next_cursor: null }; },
  getUser: async (_c: unknown, _s: unknown, id: string) => { await delay(); const u = find(id); return wrap({ user: { ...publicUser(u), model_budgets: u.model_budgets ?? {}, rate: u.rate ?? null }, current_usage: u.current_usage }, `"${u.version}"`); },
  usageHistory: async (_c: unknown, _s: unknown, id: string, opts: { period?: QuotaPeriod; limit?: number } = {}) => { await delay(); const u = find(id); const period = opts.period ?? "daily";
    const rows = Array.from({ length: Math.min(opts.limit ?? 25, 30) }, (_, i) => { const d = daysAgo(i); const f = 0.55 + 0.45 * Math.sin(i / 1.7); return { ...u.current_usage[period], user_id: id, window: dateOnly(d), window_start: `${dateOnly(d)}T00:00:00+00:00`, window_end: `${dateOnly(daysAgo(i - 1))}T00:00:00+00:00`, resets_at: `${dateOnly(daysAgo(i - 1))}T00:00:00+00:00`, cost_usd: +(u.today.cost_usd * f).toFixed(4), requests: Math.round(u.today.requests * f), input_tokens: Math.round(u.today.input_tokens * f), output_tokens: Math.round(u.today.output_tokens * f) }; });
    return { user_id: id, period, start: rows[rows.length - 1].window, end: rows[0].window, usage: rows, next_cursor: null }; },
  listAuditPage: async (_c: unknown, _s: unknown, opts: { limit?: number; cursor?: string | null } = {}) => { await delay(); const limit = opts.limit ?? 25; const start = opts.cursor ? Number(opts.cursor) : 0; return { events: audit.slice(start, start + limit), next_cursor: start + limit < audit.length ? String(start + limit) : null }; },
  listUserAuditPage: async (_c: unknown, _s: unknown, id: string, opts: { limit?: number } = {}) => { await delay(); return { user_id: id, events: audit.filter((e) => e.user_id === id).slice(0, opts.limit ?? 25), next_cursor: null }; },
  modelUsage: async (_c: unknown, _s: unknown, id: string, modelId: string) => { await delay(); const u = find(id); const scale = (v: number) => Math.round(v * 0.62); const scaled = Object.fromEntries((Object.keys(u.current_usage) as QuotaPeriod[]).map((p) => { const r = u.current_usage[p]; return [p, { ...r, cost_usd: +(r.cost_usd * 0.62).toFixed(4), requests: scale(r.requests), input_tokens: scale(r.input_tokens), output_tokens: scale(r.output_tokens) }]; })) as CurrentUsage; return { user_id: id, model_id: modelId, current_usage: scaled }; },

  createUser: async (_c: unknown, _s: unknown, req: { user_id: string; name: string; limits: QuotaLimits; rate?: RateLimits | null }) => { await delay();
    if (users.some((u) => u.user_id === req.user_id)) throw new ApiError("A user with this identity already exists", 409, "user_already_exists");
    const row: Row = { user_id: req.user_id, name: req.name, status: "active", status_reason: "User created", status_origin: "admin", granularity: "user", version: 1, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), limits: req.limits, rate: req.rate ?? null, today: { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 }, current_usage: usage(0, 0) };
    users.unshift(row); record(row, null, "user.created", "Created from the console"); return wrap({ user_id: row.user_id, provisioned: true, limits: row.limits, user: publicUser(row) }, '"1"'); },
  setLimits: async (_c: unknown, _s: unknown, user: AdminUser, req: { limits: QuotaLimits; rate?: RateLimits | null; reason?: string }) => { await delay(); const u = find(user.user_id);
    if (u.version !== user.version) throw new ApiError("This user changed since you opened it", 409, "version_conflict", { current: publicUser(u) });
    const before = { ...u }; u.limits = req.limits; if (req.rate !== undefined) u.rate = req.rate; u.version += 1; u.updated_at = new Date().toISOString();
    record(u, before, "user.limits.updated", req.reason ?? ""); return wrap({ user_id: u.user_id, updated: true, limits: u.limits, user: publicUser(u) }, `"${u.version}"`); },
  setStatus: async (_c: unknown, _s: unknown, user: AdminUser, status: "active" | "blocked", reason: string) => { await delay(); const u = find(user.user_id);
    if (u.version !== user.version) throw new ApiError("This user changed since you opened it", 409, "version_conflict", { current: publicUser(u) });
    const before = { ...u }; u.status = status; u.status_reason = reason; u.status_origin = "admin"; u.version += 1; u.updated_at = new Date().toISOString();
    record(u, before, "user.status.updated", reason); return wrap({ user_id: u.user_id, status, reason, user: publicUser(u) }, `"${u.version}"`); },
  setModelBudget: async (_c: unknown, _s: unknown, user: AdminUser, modelId: string, limits: QuotaLimits, reason?: string) => { await delay(); const u = find(user.user_id);
    if (!MODELS.includes(modelId)) throw new ApiError(`Model ${modelId} is not in allowed_model_arns for this deployment`, 400, "invalid_request_error");
    const before = { ...u }; u.model_budgets = { ...(u.model_budgets ?? {}), [modelId]: limits }; u.version += 1; record(u, before, "user.model_budget.updated", reason ?? "");
    return wrap({ user_id: u.user_id, model_id: modelId, updated: true, model_budgets: u.model_budgets, user: { ...publicUser(u), model_budgets: u.model_budgets } }, `"${u.version}"`); },
  removeModelBudget: async (_c: unknown, _s: unknown, user: AdminUser, modelId: string, reason?: string) => { await delay(); const u = find(user.user_id);
    const before = { ...u }; const next = { ...(u.model_budgets ?? {}) }; delete next[modelId]; u.model_budgets = next; u.version += 1; record(u, before, "user.model_budget.removed", reason ?? "");
    return wrap({ user_id: u.user_id, model_id: modelId, removed: true, model_budgets: next, user: { ...publicUser(u), model_budgets: next } }, `"${u.version}"`); },
});

/* ------------------------------------------------------------------ mount */

const cfg = { gatewayUrl: "https://gateway.preview.local", region: "us-east-1", issuer: "https://issuer.preview.local", clientId: "preview", identityPoolId: "us-east-1:preview", scopes: "openid email profile" };
const session = { email: "admin@example.test", authorization: async () => ({ signer: { sign: async (r: Request) => r }, expiresAt: Date.now() + 3_600_000 }), reauthenticate: () => {}, logout: () => { /* sign-out is a no-op in the preview harness */ } };

createRoot(document.getElementById("root")!).render(
  <Dashboard cfg={cfg as never} session={session as never} onSignOut={() => { /* sign-out is a no-op in the preview harness */ }} />,
);
