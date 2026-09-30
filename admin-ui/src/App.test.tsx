import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Dashboard,
  LimitsDialog,
  mergeCanonicalUser,
  mergeRefreshedUsers,
  mergeRefreshedWorkloads,
  QuotaUsage,
  StatusDialog,
  statusEnforcementMessage,
  UsersPanel,
} from "./App";
import { ApiError, api, isAutomaticBlock, type AdminUser, type AuditEvent, type CurrentUsage, type Operations, type QuotaPeriod, type Summary, type UsageMetrics, type UserRow, type WorkloadEntry, type WorkloadListResponse } from "./api";
import type { Session } from "./auth";
import type { AdminConfig } from "./config";

const cfg: AdminConfig = {
  gatewayUrl: "https://gateway.example.test",
  region: "us-east-1",
  issuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
  clientId: "client",
  identityPoolId: "us-east-1:identity",
  scopes: "openid email profile",
};

const session: Session = {
  email: "admin@example.test",
  authorization: vi.fn(),
  reauthenticate: vi.fn(),
  logout: vi.fn(),
};

const emptyUsageMetrics: UsageMetrics = {
  status: "available",
  as_of: "2026-09-02T10:00:00Z",
  start: "2026-08-31",
  end: "2026-09-02",
  period: "daily",
  days: ["2026-08-31", "2026-09-01", "2026-09-02"],
  models: [],
  totals: { cost_usd: 0, requests: 0, input_tokens: 0, output_tokens: 0 },
  top_users: [],
};

// Every Dashboard render mounts the Overview usage charts; keep their fetch
// deterministic by default. restoreMocks unwinds this spy after each test.
const emptyWorkloads: WorkloadListResponse = { workloads: [], roster_source: "parameter_store", tag_key: "bedrock-spend-controls-workload" };

beforeEach(() => {
  vi.spyOn(api, "usageMetrics").mockResolvedValue(emptyUsageMetrics);
  vi.spyOn(api, "listWorkloads").mockResolvedValue(emptyWorkloads);
});

const summary: Summary = {
  enforcement: {
    source: "dynamodb",
    as_of: "2026-09-02T10:00:00Z",
    window: "2026-09-02",
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    permission_lease_source: "deployment_default",
    refresh_overlap_seconds: 10,
    refresh_jitter_seconds: 5,
    vend_rate_limit_per_minute: 6,
    revocation_policy_shards: 19,
    revocation_reconcile_minutes: 5,
    total_users: 1,
    blocked_users: 0,
    blocked_user_ids: [],
    today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
    subjects: {
      users: { total: 1, blocked: 0, today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 } },
      workloads: {
        total: 0, blocked: 0, configured: 0, metering_only: 0, unregistered: 0, awaiting_traffic: 0,
        today: { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
      },
    },
  },
  observability: {
    source: "bedrock_model_invocation_logs",
    delivery: "cloudwatch_logs_subscription",
    metrics_namespace: "BedrockSpendControls",
    detection_lag_metric: "DetectionLagMilliseconds",
  },
};

const operations: Operations = {
  as_of: "2026-09-02T10:00:00Z",
  configuration: {
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    permission_lease_source: "deployment_default",
    permission_lease_default_seconds: 300,
    refresh_overlap_seconds: 10,
    refresh_jitter_seconds: 5,
    vend_rate_limit_per_minute: 6,
    revocation_policy_shards: 19,
    revocation_policy_max_characters: 6144,
    revocation_reconcile_minutes: 5,
  },
  emergency: {
    state: "inactive",
    desired_active: false,
    generation: 0,
    applied_generation: 0,
    requested_at: null,
    applied_at: null,
    converged: true,
  },
  metrics: {
    namespace: "BedrockSpendControls",
    detection_lag_metric: "DetectionLagMilliseconds",
    detection_lag_p95_ms: 100,
    detection_lag_timestamp: "2026-09-02T09:59:00Z",
    telemetry_status: "complete",
    last_reconciliation_at: null,
    reconciliation_status: "unknown",
    revoked_identities_desired: null,
    recent_sync_failure_count: 0,
    recent_overflow_count: 0,
    recent_emergency_failure_count: 0,
    window_minutes: 15,
  },
  alarms: [],
  cloudwatch: { status: "available" },
  auto_block_sweep: { schedule: "00:05 UTC daily", status: "never_ran", last_run: null },
};

function currentUsageRow(period: QuotaPeriod, overrides = {}) {
  const starts = { daily: "2026-09-02", weekly: "2026-08-31", monthly: "2026-09-01" };
  const ends = { daily: "2026-09-03", weekly: "2026-09-07", monthly: "2026-10-01" };
  return {
    period,
    window: starts[period],
    window_start: `${starts[period]}T00:00:00+00:00`,
    window_end: `${ends[period]}T00:00:00+00:00`,
    resets_at: `${ends[period]}T00:00:00+00:00`,
    cost_usd: 5,
    input_tokens: 50,
    output_tokens: 10,
    requests: 4,
    ...overrides,
  };
}

const currentUsage: CurrentUsage = {
  daily: currentUsageRow("daily"),
  weekly: currentUsageRow("weekly", { cost_usd: 7, requests: 6 }),
  monthly: currentUsageRow("monthly", { cost_usd: 9, requests: 8 }),
};

const alice: UserRow = {
  user_id: "tenant/alice",
  name: "Alice Example",
  status: "active",
  status_reason: "User created",
  status_origin: "admin",
  granularity: "user",
  version: 1,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
  limits: {
    daily: { usd: 10, input_tokens: 100, output_tokens: 20 },
    weekly: { usd: 20, input_tokens: 200, output_tokens: 40 },
    monthly: null,
  },
  today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
  current_usage: currentUsage,
};

function canonical(overrides: Partial<AdminUser> = {}): AdminUser {
  const { today: _today, current_usage: _currentUsage, ...base } = alice;
  return { ...base, ...overrides };
}

function UsersHarness({ summaryRefresh = vi.fn().mockResolvedValue(undefined) }: { summaryRefresh?: () => Promise<void> }) {
  const [users, setUsers] = useState([alice]);
  return (
    <UsersPanel
      cfg={cfg}
      enforcement={summary.enforcement}
      error=""
      loading={false}
      onSummaryRefresh={summaryRefresh}
      onUserChanged={(updated) => setUsers((current) => current.map((item) =>
        item.user_id === updated.user_id ? { ...updated, today: item.today, current_usage: item.current_usage } : item,
      ))}
      session={session}
      stale={false}
      users={users}
    />
  );
}

// The dashboard opens on Overview; user management lives on its own tab.
async function openTab(actor: ReturnType<typeof userEvent.setup>, name: "Overview" | "Users" | "Workloads" | "Operations" | "Audit log") {
  await actor.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name }));
}

describe("quota presentation", () => {
  it("renders zero limits as Unlimited while keeping nonzero usage visible and no progressbar", () => {
    render(<QuotaUsage current={25} format={(value) => String(value)} limit={0} />);

    expect(screen.getByText("25")).toBeInTheDocument();
    expect(screen.getByText("of Unlimited")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(document.querySelector(".progress-critical")).not.toBeInTheDocument();
  });

  it("retains finite normal, warning, and critical states with accessible value text", () => {
    const { rerender } = render(<QuotaUsage current={5} format={(value) => `${value} units`} limit={10} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "5 units of 10 units used (50 percent)");
    expect(document.querySelector(".progress-normal")).toBeInTheDocument();

    rerender(<QuotaUsage current={8} format={(value) => `${value} units`} limit={10} />);
    expect(document.querySelector(".progress-warning")).toBeInTheDocument();

    rerender(<QuotaUsage current={12} format={(value) => `${value} units`} limit={10} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "12 units of 10 units used (120 percent)");
    expect(document.querySelector(".progress-critical")).toBeInTheDocument();
  });

  it("switches the displayed calendar usage while surfacing highest utilization", async () => {
    const actor = userEvent.setup();
    render(<UsersHarness />);

    expect(screen.getByText("Highest: Daily 50%")).toBeInTheDocument();
    await actor.selectOptions(screen.getByLabelText("Usage period"), "weekly");
    expect(screen.getByRole("columnheader", { name: "Weekly USD" })).toBeInTheDocument();
    expect(screen.getByText("$7.00")).toBeInTheDocument();
    await actor.selectOptions(screen.getByLabelText("Usage period"), "monthly");
    expect(screen.getAllByText("Disabled").length).toBeGreaterThanOrEqual(3);
  });
});

describe("limit safety dialog", () => {
  it("round-trips a thresholds list and rpm/tpm through the limits editor", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    const withThresholds: UserRow = {
      ...alice,
      rate: { rpm: 30, tpm: 0 },
      limits: {
        ...alice.limits,
        daily: {
          usd: 10,
          input_tokens: 100,
          output_tokens: 20,
          thresholds: [
            { at: 0.5, action: "warn" },
            { at: 1, action: "block" },
          ],
        },
      },
    };
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={withThresholds} />);

    // Stored thresholds render as percentages in order.
    expect(screen.getByLabelText("Daily threshold 1 percent")).toHaveValue(50);
    expect(screen.getByLabelText("Daily threshold 1 action")).toHaveValue("warn");
    expect(screen.getByLabelText("Daily threshold 2 percent")).toHaveValue(100);
    expect(screen.getByLabelText("Daily threshold 2 action")).toHaveValue("block");
    // Weekly has no stored list: the editor shows the default (80 % / 100 %).
    expect(screen.getByLabelText("Weekly threshold 1 percent")).toHaveValue(80);
    expect(screen.getByLabelText("Requests per minute limit")).toHaveValue(30);
    expect(screen.getByLabelText("Tokens per minute limit")).toHaveValue(0);

    // Raise the block level to 150 % and add a 90 % warning before it.
    const dailyBlock = screen.getByLabelText("Daily threshold 2 percent");
    await actor.clear(dailyBlock);
    await actor.type(dailyBlock, "150");
    await actor.click(within(screen.getByTestId("daily-thresholds")).getByRole("button", { name: "Add threshold" }));
    const inserted = screen.getByLabelText("Daily threshold 2 percent");
    await actor.clear(inserted);
    await actor.type(inserted, "90");
    expect(screen.getByLabelText("Daily threshold 3 action")).toHaveValue("block");

    // Change the rate limits.
    const rpm = screen.getByLabelText("Requests per minute limit");
    await actor.clear(rpm);
    await actor.type(rpm, "60");
    const tpm = screen.getByLabelText("Tokens per minute limit");
    await actor.clear(tpm);
    await actor.type(tpm, "100000");

    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeEnabled();
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: {
          usd: 10,
          input_tokens: 100,
          output_tokens: 20,
          thresholds: [
            { at: 0.5, action: "warn" },
            { at: 0.9, action: "warn" },
            { at: 1.5, action: "block" },
          ],
        },
        // Untouched periods omit thresholds so the server keeps its list.
        weekly: alice.limits.weekly,
        monthly: null,
      },
      rate: { rpm: 60, tpm: 100000 },
    });
  });

  it("flags an alert-only thresholds list, requires a reason, and rejects an out-of-order block", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    // Default list is 80 % warn, 100 % block. Turn the block into a warn.
    await actor.selectOptions(screen.getByLabelText("Daily threshold 2 action"), "warn");
    expect(within(screen.getByTestId("daily-thresholds")).getByRole("status")).toHaveTextContent("Alert-only");
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeDisabled();
    await actor.type(screen.getByLabelText(/Reason/), "Soft budget for the pilot team");
    expect(save).toBeEnabled();

    // Now make the first entry a block (non-terminal): the editor refuses.
    await actor.selectOptions(screen.getByLabelText("Daily threshold 1 action"), "block");
    expect(within(screen.getByTestId("daily-thresholds")).getByRole("alert")).toHaveTextContent("must be the last entry");
    await actor.click(save);
    expect(onSave).not.toHaveBeenCalled();

    // Restore a valid alert-only list and save.
    await actor.selectOptions(screen.getByLabelText("Daily threshold 1 action"), "warn");
    await actor.click(save);
    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: {
          usd: 10,
          input_tokens: 100,
          output_tokens: 20,
          thresholds: [
            { at: 0.8, action: "warn" },
            { at: 1, action: "warn" },
          ],
        },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Soft budget for the pilot team",
    });
  });

  it("sends rate: null when both rate limits are cleared", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={{ ...alice, rate: { rpm: 10, tpm: 5 } }} />);
    const rpm = screen.getByLabelText("Requests per minute limit");
    await actor.clear(rpm);
    await actor.type(rpm, "0");
    const tpm = screen.getByLabelText("Tokens per minute limit");
    await actor.clear(tpm);
    await actor.type(tpm, "0");
    await actor.click(screen.getByRole("button", { name: "Save limits" }));
    expect(onSave).toHaveBeenCalledWith({
      limits: { daily: alice.limits.daily, weekly: alice.limits.weekly, monthly: null },
      rate: null,
    });
  });

  it("requires confirmation and a reason for Unlimited or below-usage limits", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    expect(screen.getByText(/Enter 0 for an Unlimited/)).toBeInTheDocument();
    const usd = screen.getByLabelText("Daily USD limit");
    await actor.clear(usd);
    await actor.type(usd, "0");
    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "40");

    expect(screen.getByRole("status")).toHaveTextContent("input tokens");
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeDisabled();
    const confirmation = screen.getByRole("checkbox", { name: /should change from a finite value to Unlimited/ });
    await actor.click(confirmation);
    expect(save).toBeDisabled();
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveAttribute("aria-required", "true");
    await actor.type(reason, "   ");
    expect(save).toBeDisabled();
    await actor.clear(reason);
    await actor.type(reason, "  Capacity exception review  ");
    expect(save).toBeEnabled();
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 0, input_tokens: 40, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Capacity exception review",
    });
  });

  it("requires a reason when a finite limit is below current usage", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "40");
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(screen.queryByRole("checkbox", { name: /should change from a finite value to Unlimited/ })).not.toBeInTheDocument();
    expect(save).toBeDisabled();
    await actor.type(screen.getByLabelText(/Reason/), "Below-usage test");
    expect(save).toBeEnabled();
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 10, input_tokens: 40, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Below-usage test",
    });
  });

  it("includes an optional trimmed reason for an ordinary limit change", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "101");
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveAttribute("aria-required", "false");
    await actor.type(reason, "  Annual allocation  ");
    await actor.click(screen.getByRole("button", { name: "Save limits" }));

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 10, input_tokens: 101, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Annual allocation",
    });
  });

  it("requires confirmation and a reason when enabling an all-Unlimited period", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    await actor.click(screen.getByRole("checkbox", { name: "Monthly" }));
    expect(screen.getByText(/include usage accumulated since their UTC boundary/)).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeDisabled();
    await actor.click(screen.getByRole("checkbox", { name: /monthly usd.*Unlimited/i }));
    await actor.type(screen.getByLabelText(/Reason/), "Enable monthly accounting");
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: alice.limits.daily,
        weekly: alice.limits.weekly,
        monthly: { usd: 0, input_tokens: 0, output_tokens: 0 },
      },
      reason: "Enable monthly accounting",
    });
  });
});

describe("canonical local reconciliation", () => {
  it("keeps the latest cached usage while applying returned configuration", () => {
    const latestUsage = { ...alice, today: { ...alice.today, cost_usd: 9, requests: 8 } };
    const updated = canonical({ status: "blocked", version: 2, status_reason: "Policy request" });

    expect(mergeCanonicalUser([latestUsage], updated)[0]).toEqual({
      ...updated,
      today: latestUsage.today,
      current_usage: latestUsage.current_usage,
    });
  });

  it("keeps newer canonical configuration when an eventually consistent refresh is older", () => {
    const current = { ...alice, status: "blocked" as const, version: 2, status_reason: "Policy request" };
    const staleRefresh = { ...alice, today: { ...alice.today, cost_usd: 9, requests: 8 } };

    expect(mergeRefreshedUsers([current], [staleRefresh])[0]).toEqual({
      ...current,
      today: staleRefresh.today,
      current_usage: staleRefresh.current_usage,
    });
  });
});

describe("status safety dialog", () => {
  it("requires a trimmed reason, describes bounded overspend, cancels with Escape, and restores focus", async () => {
    const actor = userEvent.setup();
    const onConfirm = vi.fn();

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)} type="button">Open status</button>
          {open && (
            <StatusDialog
              apiError=""
              busy={false}
              enforcement={summary.enforcement}
              onClose={() => setOpen(false)}
              onConfirm={onConfirm}
              user={alice}
            />
          )}
        </>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open status" });
    await actor.click(opener);
    expect(screen.getByText("Current status").parentElement).toHaveTextContent("active");
    expect(screen.getByText("Next status").parentElement).toHaveTextContent("blocked");
    expect(screen.getByText(/bounded overspend is limited/)).toHaveTextContent("5 min");
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveFocus();
    expect(screen.getByRole("button", { name: "Block user" })).toBeDisabled();
    await actor.type(reason, "   ");
    expect(screen.getByRole("button", { name: "Block user" })).toBeDisabled();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await actor.click(opener);
    await actor.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("keeps focus inside the modal while a status write is busy", async () => {
    const actor = userEvent.setup();
    const props = {
      apiError: "",
      enforcement: summary.enforcement,
      onClose: vi.fn(),
      onConfirm: vi.fn(),
      user: alice,
    };
    const renderDialog = (busy: boolean) => (
      <StatusDialog
        apiError={props.apiError}
        busy={busy}
        enforcement={props.enforcement}
        onClose={props.onClose}
        onConfirm={props.onConfirm}
        user={props.user}
      />
    );
    const { rerender } = render(renderDialog(false));

    rerender(renderDialog(true));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveFocus();
    await actor.tab();
    expect(dialog).toHaveFocus();
  });

  it("persists a trimmed reason and replaces the row locally even when summary refresh fails", async () => {
    const actor = userEvent.setup();
    const summaryRefresh = vi.fn().mockRejectedValue(new Error("summary failed"));
    const updated = canonical({
      status: "blocked",
      status_reason: "Policy request",
      status_origin: "admin",
      version: 2,
      updated_at: "2026-09-02T10:05:00Z",
    });
    const setStatus = vi.spyOn(api, "setStatus").mockResolvedValue({
      data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated },
      etag: '"2"',
      requestId: "status-request",
      status: 200,
    });
    render(<UsersHarness summaryRefresh={summaryRefresh} />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "  Policy request  ");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(setStatus).toHaveBeenCalledWith(cfg, session, alice, "blocked", "Policy request");
    expect(screen.getByText("blocked", { selector: ".status-badge" })).toBeInTheDocument();
    expect(screen.getByText("Policy request")).toBeInTheDocument();
    expect(screen.getByText("Alice Example is now blocked.")).toBeInTheDocument();
    expect(summaryRefresh).toHaveBeenCalledOnce();
  });

  it("keeps the confirmation open and reports a distinct mutation error", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setStatus").mockRejectedValue(new ApiError("Unavailable", 503, "transaction_unavailable"));
    render(<UsersHarness />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText("The broker is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("active", { selector: ".status-badge" })).toBeInTheDocument();
  });

  it("rejects a version-conflict payload for a different identity", async () => {
    const actor = userEvent.setup();
    const wrongUser = canonical({ user_id: "tenant/bob", name: "Bob", version: 2 });
    vi.spyOn(api, "setStatus").mockRejectedValue(new ApiError(
      "Changed",
      409,
      "version_conflict",
      { current_user: wrongUser },
      "conflict-request",
    ));
    render(<UsersHarness />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText(/version conflict response did not identify the requested user/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.queryByText("Bob")).not.toBeInTheDocument();
  });
});

describe("independent dashboard refresh state", () => {
  it("keeps only a failed summary cached while successful users and operations stay fresh", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary")
      .mockResolvedValueOnce(summary)
      .mockRejectedValueOnce(new ApiError("Summary unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    await openTab(actor, "Users");
    expect(await screen.findByText("Alice Example")).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Refresh data" }));

    expect(screen.queryByText("Cached users · refresh failed")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /until a fresh enforcement summary loads/ })).toBeDisabled();

    await openTab(actor, "Overview");
    expect(await screen.findByText("Cached summary · refresh failed")).toBeInTheDocument();
    expect(screen.getByText("The broker is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(within(screen.getByRole("article", { name: "Users" })).getByText("Managed").nextElementSibling).toHaveTextContent("1");

    await openTab(actor, "Operations");
    expect(screen.queryByText(/Cached from/)).not.toBeInTheDocument();
  });

  it("shows users as unavailable rather than as a verified empty population", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "listUsersPage").mockRejectedValue(new ApiError("Users unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    await openTab(actor, "Users");
    expect(await screen.findByText("Users unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No users yet")).not.toBeInTheDocument();

    await openTab(actor, "Overview");
    expect(screen.getByLabelText("Quota summary by subject kind")).toBeInTheDocument();

    await openTab(actor, "Operations");
    expect(screen.getByRole("heading", { name: "Operations", level: 2 })).toBeInTheDocument();
  });
});


const bob: UserRow = {
  ...alice,
  user_id: "tenant/bob",
  name: "Bob Example",
  status: "blocked",
  status_reason: "Policy request",
  version: 2,
};

function auditFor(user: UserRow): AuditEvent {
  const snapshot = {
    user_id: user.user_id,
    name: user.name,
    status: user.status,
    status_reason: user.status_reason,
    status_origin: user.status_origin,
    version: user.version,
    created_at: user.created_at,
    updated_at: user.updated_at,
    limits: Object.fromEntries((["daily", "weekly", "monthly"] as const).map((period) => {
      const limits = user.limits[period];
      return [period, limits ? {
        usd_micro: limits.usd * 1_000_000,
        input_tokens: limits.input_tokens,
        output_tokens: limits.output_tokens,
      } : null];
    })) as unknown as AuditEvent["after"]["limits"],
  };
  return {
    user_id: user.user_id,
    event_key: `2026-09-02T10:00:00Z#${user.user_id}`,
    event_type: "user.created",
    actor: "admin@example.test",
    auth_method: "jwt",
    reason: "admin user creation",
    request_id: `create-${user.user_id}`,
    created_at: "2026-09-02T10:00:00Z",
    before: null,
    after: snapshot,
  };
}

describe("server-side user pagination", () => {
  it("loads one page, navigates opaque cursor history, and resets cursors for explicit query/status filters", async () => {
    const actor = userEvent.setup();
    const listUsers = vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockResolvedValueOnce({ users: [bob], next_cursor: "cursor-two" })
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(listUsers).toHaveBeenCalledTimes(1);
    // The Users tab is signed-in users only; workloads live on their own tab.
    expect(listUsers.mock.calls[0][2]).toEqual({ limit: 25, cursor: null, status: undefined, granularity: "user", query: "" });
    expect(screen.getByText("1 user on this page", { selector: ".users-panel .panel-heading p" })).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[1][2]).toMatchObject({ cursor: "cursor-one" });
    await actor.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[2][2]).toMatchObject({ cursor: null });

    await actor.type(screen.getByLabelText("Search users"), "bob");
    expect(listUsers).toHaveBeenCalledTimes(3);
    await actor.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[3][2]).toMatchObject({ cursor: null, query: "bob" });

    await actor.selectOptions(screen.getByLabelText("Filter users"), "blocked");
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(5));
    expect(listUsers.mock.calls[4][2]).toMatchObject({ cursor: null, query: "bob", status: "blocked" });
  });

  it("keeps the current page visible when Next fails", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockRejectedValueOnce(new ApiError("Unavailable", 503, "service_unavailable"));
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("The broker is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.getByText("Cached users · refresh failed")).toBeInTheDocument();
  });
});

describe("dashboard operational navigation", () => {
  it("loads global audit only after navigation and opens a current-page target in the detail drawer", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    const audit = vi.spyOn(api, "listAuditPage").mockResolvedValue({ events: [auditFor(alice)], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    expect(audit).not.toHaveBeenCalled();
    await actor.click(screen.getByRole("button", { name: "Audit log" }));
    expect(await screen.findByRole("button", { name: alice.user_id })).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: alice.user_id }));
    expect(await screen.findByRole("dialog", { name: "Alice Example" })).toBeInTheDocument();
  });

  it("uses a fresh server-side user search for an audit target outside the current page", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    const listUsers = vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: null })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null });
    vi.spyOn(api, "listAuditPage").mockResolvedValue({ events: [auditFor(bob)], next_cursor: null });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(screen.getByRole("button", { name: "Audit log" }));
    await actor.click(await screen.findByRole("button", { name: bob.user_id }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[1][2]).toMatchObject({ cursor: null, query: bob.user_id, status: undefined });
  });
});

describe("create and canonical detail synchronization", () => {
  it("adds a successful create with zero today usage and can open its details", async () => {
    const actor = userEvent.setup();
    const created = canonical({ user_id: "tenant/new", name: "New User", version: 1 });
    const zeroUsage = Object.fromEntries((["daily", "weekly", "monthly"] as const).map((period) => [period, { ...currentUsage[period], cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 }])) as unknown as CurrentUsage;
    vi.spyOn(api, "summary").mockResolvedValue({ ...summary, enforcement: { ...summary.enforcement, total_users: 0 } });
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [], next_cursor: null });
    vi.spyOn(api, "createUser").mockResolvedValue({ data: { user_id: created.user_id, provisioned: true, limits: created.limits, user: created }, etag: '"1"', requestId: "create-new", status: 200 });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: created, current_usage: zeroUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByText("No users yet");
    await actor.click(screen.getByRole("button", { name: "Create user" }));
    const wizard = screen.getByRole("dialog", { name: "Create user" });
    await actor.type(within(wizard).getByLabelText("User identity claim value"), created.user_id);
    await actor.type(within(wizard).getByLabelText("Display name"), created.name);
    await actor.click(within(wizard).getByRole("button", { name: "Next" }));
    await actor.click(within(wizard).getByRole("button", { name: "Next" }));
    await actor.click(within(wizard).getByRole("button", { name: "Create user" }));

    expect(await screen.findByRole("button", { name: "New User" })).toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "New User" })).toHaveTextContent("$0.00");
    expect(screen.getByText("New User was created.")).toBeInTheDocument();
  });

  it("keeps an open detail drawer synchronized with canonical status mutations", async () => {
    const actor = userEvent.setup();
    const updated = canonical({ status: "blocked", status_reason: "Policy request", version: 2 });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    vi.spyOn(api, "setStatus").mockResolvedValue({ data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated }, etag: '"2"', requestId: "status-two", status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await actor.click(await screen.findByRole("button", { name: "Alice Example" }));
    await actor.click(screen.getByRole("button", { name: "Block user" }));
    const statusDialog = screen.getByRole("dialog", { name: "Confirm block" });
    await actor.type(within(statusDialog).getByLabelText(/Reason/), "Policy request");
    await actor.click(within(statusDialog).getByRole("button", { name: "Block user" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument());

    const statuses = screen.getAllByText("blocked", { selector: ".status-badge" });
    expect(statuses).toHaveLength(2);
    expect(screen.getByRole("dialog", { name: "Alice Example" })).toHaveTextContent("Policy request");
  });
});


describe("additional canonical and modal safety", () => {
  it("never lets a late detail response replace a newer canonical configuration", () => {
    const current = { ...alice, status: "blocked" as const, status_reason: "Newer mutation", version: 3 };
    const delayedDetail = canonical({ status: "active", status_reason: "Older detail", version: 2 });

    expect(mergeCanonicalUser([current], delayedDetail)).toEqual([current]);
  });

  it("removes a status mutation that no longer matches the active server-side filter", async () => {
    const actor = userEvent.setup();
    const updated = canonical({ status: "blocked", status_reason: "Policy request", version: 2 });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: null })
      .mockResolvedValueOnce({ users: [alice], next_cursor: null });
    vi.spyOn(api, "setStatus").mockResolvedValue({ data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated }, etag: '"2"', requestId: "status-filter", status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.selectOptions(screen.getByLabelText("Filter users"), "active");
    await waitFor(() => expect(screen.getByLabelText("Filter users")).toHaveValue("active"));
    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText("No matching users")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Alice Example" })).not.toBeInTheDocument();
  });

  it("suspends the drawer so Escape closes only a nested status dialog", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    const opener = await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(opener);
    const drawerAction = screen.getByRole("button", { name: "Block user" });
    await actor.click(drawerAction);
    expect(screen.getByRole("dialog", { name: "Confirm block" })).toBeInTheDocument();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Alice Example" })).toBeInTheDocument();
    expect(drawerAction).toHaveFocus();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Alice Example" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});


describe("filtered detail and precision edge cases", () => {
  it("does not evict a newer filtered row when an older detail response has another status", async () => {
    const actor = userEvent.setup();
    const current = { ...bob, version: 3 };
    let resolveDetail!: (value: { data: { user: AdminUser; current_usage: CurrentUsage }; etag: string; requestId: null; status: number }) => void;
    const delayedDetail = new Promise<{ data: { user: AdminUser; current_usage: CurrentUsage }; etag: string; requestId: null; status: number }>((resolve) => { resolveDetail = resolve; });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [current], next_cursor: null });
    vi.spyOn(api, "getUser").mockReturnValue(delayedDetail);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Bob Example" });
    await actor.selectOptions(screen.getByLabelText("Filter users"), "blocked");
    await actor.click(await screen.findByRole("button", { name: "Bob Example" }));
    resolveDetail({ data: { user: canonical({ user_id: bob.user_id, name: bob.name, status: "active", version: 2 }), current_usage: currentUsage }, etag: '"2"', requestId: null, status: 200 });

    await waitFor(() => expect(screen.getByRole("button", { name: "Bob Example" })).toBeInTheDocument());
    expect(screen.getAllByText("blocked", { selector: ".status-badge" })).toHaveLength(2);
  });

  it("supports micro-dollar limits in the edit form without native step rejection", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={{
      ...alice,
      limits: { ...alice.limits, daily: { ...alice.limits.daily!, usd: 0.123457 } },
      today: { ...alice.today, cost_usd: 0.1 },
      current_usage: { ...alice.current_usage, daily: { ...alice.current_usage.daily, cost_usd: 0.1 } },
    }} />);

    const usd = screen.getByLabelText("Daily USD limit");
    expect(usd).toHaveAttribute("step", "0.000001");
    expect((usd as HTMLInputElement).checkValidity()).toBe(true);
    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "101");
    await actor.click(screen.getByRole("button", { name: "Save limits" }));
    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 0.123457, input_tokens: 101, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
    });
  });

  it("disables drawer status changes until a fresh enforcement summary is available", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockRejectedValue(new ApiError("Summary unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await actor.click(await screen.findByRole("button", { name: "Alice Example" }));
    const drawer = screen.getByRole("dialog", { name: "Alice Example" });
    expect(within(drawer).getByRole("button", { name: /Status change unavailable/ })).toBeDisabled();
    expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument();
  });
});

describe("workload mode", () => {
  const paymentsSubject: UserRow = {
    ...alice,
    user_id: "workload:payments",
    name: "payments",
    granularity: "workload",
    enforcement_ready: true,
    workload: {
      workload_id: "workload:payments",
      name: "payments",
      model: "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
      role_arn: "arn:aws:iam::123456789012:role/payments-batch",
      enforcement_ready: true,
      registered: true,
      tag: { key: "bedrock-spend-controls-workload", value: "payments" },
    },
  };
  const reportsSubject: UserRow = {
    ...alice,
    user_id: "workload:reports",
    name: "reports",
    status: "blocked",
    status_origin: "automatic",
    status_reason: "auto: daily USD quota exhausted in 2026-09-02",
    granularity: "workload",
    enforcement_ready: false,
    workload: {
      workload_id: "workload:reports",
      name: "reports",
      model: "us.amazon.nova-pro-v1:0",
      profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/def456",
      role_arn: null,
      enforcement_ready: false,
      registered: true,
      tag: { key: "bedrock-spend-controls-workload", value: "reports" },
    },
  };
  const roster: WorkloadListResponse = {
    roster_source: "parameter_store",
    tag_key: "bedrock-spend-controls-workload",
    workloads: [
      { ...paymentsSubject.workload!, subject: paymentsSubject },
      { ...reportsSubject.workload!, subject: reportsSubject },
      {
        workload_id: "workload:silent",
        name: "silent",
        model: "us.amazon.nova-micro-v1:0",
        profile_arn: "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/ghi789",
        role_arn: "arn:aws:iam::123456789012:role/silent",
        enforcement_ready: true,
        registered: true,
        tag: { key: "bedrock-spend-controls-workload", value: "silent" },
        subject: null,
      },
    ],
  };
  const splitSummary: Summary = {
    ...summary,
    enforcement: {
      ...summary.enforcement,
      total_users: 3,
      blocked_users: 1,
      subjects: {
        users: { total: 1, blocked: 0, today: { cost_usd: 0.25, input_tokens: 10, output_tokens: 2, requests: 2 } },
        workloads: {
          total: 2, blocked: 1, configured: 3, metering_only: 1, unregistered: 0, awaiting_traffic: 1,
          today: { cost_usd: 4.75, input_tokens: 40, output_tokens: 8, requests: 2 },
        },
      },
    },
  };

  it("keeps workload rows off the Users tab and lists them on the Workloads tab with identity and enforcement", async () => {
    const actor = userEvent.setup();
    const listUsers = vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "listWorkloads").mockResolvedValue(roster);
    vi.spyOn(api, "summary").mockResolvedValue(splitSummary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    await openTab(actor, "Users");
    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[0][2]).toMatchObject({ granularity: "user" });
    // The kind filter is gone: status only.
    expect(within(screen.getByLabelText("Filter users")).queryByRole("option", { name: "Workloads" })).not.toBeInTheDocument();
    expect(screen.queryByText("workload:payments")).not.toBeInTheDocument();

    await openTab(actor, "Workloads");
    const panel = screen.getByRole("region", { name: "Configured workloads" });
    expect(screen.getByText("3 configured · 2 metered", { selector: ".panel-heading p" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "payments" })).toBeInTheDocument();
    expect(within(panel).getByText("anthropic.claude-haiku-4-5-20251001-v1:0")).toBeInTheDocument();
    expect(within(panel).getAllByText("Enforced")).toHaveLength(2); // payments, silent
    expect(within(panel).getByText("Metering only")).toBeInTheDocument();
    // Configured but silent since deploy: no row, so no limits/status actions yet.
    expect(within(panel).getByText("Awaiting traffic")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "silent" })).not.toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /silent has no quota row yet; limits/ })).toBeDisabled();
    // Role-less workload: the action is honest about what it does.
    expect(within(panel).getByRole("button", { name: "Unblock reports" })).toBeEnabled();
    expect(within(panel).getByRole("button", { name: "Block payments" })).toBeEnabled();
  });

  it("shows workload identity in the drawer and enforcement-aware copy in the status dialog", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "listWorkloads").mockResolvedValue(roster);
    vi.spyOn(api, "summary").mockResolvedValue(splitSummary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: paymentsSubject, current_usage: paymentsSubject.current_usage }, requestId: "r1", etag: '"1"' } as never);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Workloads");

    await actor.click(await screen.findByRole("button", { name: "payments" }));
    const drawer = await screen.findByRole("dialog", { name: "payments" });
    expect(within(drawer).getByText("Workload details")).toBeInTheDocument();
    const identity = within(drawer).getByRole("region", { name: "Workload identity" });
    expect(within(identity).getByText("arn:aws:iam::123456789012:role/payments-batch")).toBeInTheDocument();
    expect(within(identity).getByText("arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123")).toBeInTheDocument();
    expect(within(identity).getByText("bedrock-spend-controls-workload=payments")).toBeInTheDocument();

    await actor.click(within(drawer).getByRole("button", { name: "Block workload" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm block" });
    expect(within(dialog).getByText("IAM Deny on workload role")).toBeInTheDocument();
    expect(within(dialog).getByText(/attaches an inline IAM Deny/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Block workload" })).toBeDisabled();
  });

  it("never promises enforcement for a role-less workload", () => {
    expect(statusEnforcementMessage(summary.enforcement, "blocked", reportsSubject)).toMatch(/NOT stopped/);
    expect(statusEnforcementMessage(summary.enforcement, "active", reportsSubject)).toMatch(/no Deny was in place/);
    expect(statusEnforcementMessage(summary.enforcement, "blocked", paymentsSubject)).toMatch(/inline IAM Deny/);
    expect(statusEnforcementMessage(summary.enforcement, "blocked", { ...paymentsSubject, workload: { ...paymentsSubject.workload!, registered: false } })).toMatch(/not in the deployed roster/);
    // Users keep the credential-vend wording.
    expect(statusEnforcementMessage(summary.enforcement, "blocked", alice)).toMatch(/prevents new credentials/);
    expect(statusEnforcementMessage(summary.enforcement, "blocked")).toMatch(/prevents new credentials/);
  });

  it("tells the operator an automatic block lifts by itself, and stays quiet for admin blocks", () => {
    const autoBlocked: UserRow = { ...alice, status: "blocked", status_origin: "automatic", status_reason: "auto: daily USD quota exhausted in 2026-09-14" };
    const adminBlocked: UserRow = { ...alice, status: "blocked", status_origin: "admin", status_reason: "incident freeze" };

    expect(isAutomaticBlock(autoBlocked)).toBe(true);
    expect(isAutomaticBlock(adminBlocked)).toBe(false);
    // Only the origin decides; an "auto:" reason on an admin row is still an admin block.
    const adminWithAutoReason: UserRow = { ...adminBlocked, status_reason: "auto: daily quota exhausted" };
    expect(isAutomaticBlock(adminWithAutoReason)).toBe(false);
    expect(isAutomaticBlock({ ...autoBlocked, status: "active" })).toBe(false);

    const unblockAuto = statusEnforcementMessage(summary.enforcement, "active", autoBlocked);
    expect(unblockAuto).toMatch(/Unblocking allows new credentials/);
    expect(unblockAuto).toMatch(/nightly sweep/);
    expect(unblockAuto).not.toMatch(/UTC/);
    expect(unblockAuto).toMatch(/next credential request/);
    expect(statusEnforcementMessage(summary.enforcement, "active", adminBlocked)).not.toMatch(/nightly sweep/);
    // Blocking copy is about what the block does, never about lifting.
    expect(statusEnforcementMessage(summary.enforcement, "blocked", autoBlocked)).not.toMatch(/nightly sweep/);
  });

  it("splits the overview by subject kind and links each group to its tab", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "listWorkloads").mockResolvedValue(roster);
    vi.spyOn(api, "summary").mockResolvedValue(splitSummary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    const users = await screen.findByRole("article", { name: "Users" });
    const workloads = screen.getByRole("article", { name: "Workloads" });
    expect(within(users).getByText("Managed").nextElementSibling).toHaveTextContent("1");
    expect(within(users).getByText("Spend today").nextElementSibling).toHaveTextContent("$0.25");
    expect(within(workloads).getByText("Configured").nextElementSibling).toHaveTextContent("3");
    expect(within(workloads).getByText("Blocked").nextElementSibling).toHaveTextContent("1");
    expect(within(workloads).getByText("Spend today").nextElementSibling).toHaveTextContent("$4.75");
    expect(within(workloads).getByText("1 awaiting traffic")).toBeInTheDocument();
    expect(within(workloads).getByText("1 metering only")).toBeInTheDocument();
    expect(within(workloads).queryByText(/unregistered/)).not.toBeInTheDocument();
    // The all-subject figure is still visible, once, in the enforcement strip.
    expect(screen.getByText(/All subjects today \$5\.00 · 4 req/)).toBeInTheDocument();
    // Raw identifiers sit behind a collapsed "Technical details" element.
    const technical = screen.getByText("Technical details").closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("BedrockSpendControls")).toBeInTheDocument();
    expect(within(technical).getByText("DetectionLagMilliseconds")).toBeInTheDocument();

    await actor.click(within(workloads).getByRole("button", { name: "Manage workloads" }));
    expect(screen.getByRole("heading", { level: 1, name: "Workload management" })).toBeInTheDocument();
  });

  it("keeps a newer cached subject when a workload refresh is stale", () => {
    const entry: WorkloadEntry = { ...paymentsSubject.workload!, subject: { ...paymentsSubject, version: 3, status: "blocked" } };
    const stale: WorkloadEntry = { ...entry, subject: { ...paymentsSubject, version: 2, today: { cost_usd: 9, input_tokens: 1, output_tokens: 1, requests: 1 } } };
    const merged = mergeRefreshedWorkloads([entry], [stale])[0];
    expect(merged.subject?.version).toBe(3);
    expect(merged.subject?.status).toBe("blocked");
    // Usage always comes from the refresh.
    expect(merged.subject?.today.cost_usd).toBe(9);
    // A silent roster entry passes through untouched.
    expect(mergeRefreshedWorkloads([entry], [{ ...entry, subject: null }])[0].subject).toBeNull();
  });
});
