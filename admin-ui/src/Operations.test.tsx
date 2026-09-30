import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AutoBlockSweepCard, EmergencyStopCard, EnforcementDialCard, OperationsView, SpendReconciliationCard, leaseWindowLabel, reconciliationTone } from "./Operations";
import { ApiError, api, type AutoBlockSweep, type EnforcementConfig, type Operations, type ReconciliationResponse } from "./api";
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

const enforcement: EnforcementConfig = {
  permission_lease_seconds: 300,
  source: "deployment_default",
  generation: 0,
  actor: "",
  reason: "",
  updated_at: null,
  valid_permission_lease_seconds: [60, 300, 900],
  default_permission_lease_seconds: 300,
};

const operations: Operations = {
  as_of: "2026-09-09T10:00:00Z",
  configuration: {
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    permission_lease_source: "runtime",
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
    detection_lag_timestamp: "2026-09-09T09:59:00Z",
    telemetry_status: "complete",
    last_reconciliation_at: "2026-09-09T09:58:00Z",
    reconciliation_status: "current",
    revoked_identities_desired: 0,
    recent_sync_failure_count: 0,
    recent_overflow_count: 0,
    recent_emergency_failure_count: 0,
    window_minutes: 15,
  },
  alarms: [
    { key: "enforcement_dispatch_dlq", state: "OK", updated_at: "2026-09-09T09:00:00Z" },
  ],
  cloudwatch: { status: "available" },
  auto_block_sweep: { schedule: "00:05 UTC daily", status: "never_ran", last_run: null },
};

describe("lease window labels", () => {
  it("renders minutes for round values and seconds otherwise", () => {
    expect(leaseWindowLabel(60)).toBe("1 minute");
    expect(leaseWindowLabel(300)).toBe("5 minutes");
    expect(leaseWindowLabel(900)).toBe("15 minutes");
    expect(leaseWindowLabel(90)).toBe("90 seconds");
  });
});

describe("enforcement dial", () => {
  it("offers only the broker-provided windows and applies a change with the reason", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    const put = vi.spyOn(api, "setEnforcement").mockResolvedValue({
      permission_lease_seconds: 60,
      source: "runtime",
      generation: 1,
      actor: "admin@example.test",
      reason: "demo",
      updated_at: "2026-09-09T10:05:00Z",
    });
    const onApplied = vi.fn();
    render(<EnforcementDialCard cfg={cfg} onApplied={onApplied} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    const radios = within(group).getAllByRole("radio");
    expect(radios.map((radio) => radio.textContent)).toEqual([
      "1 minute",
      "5 minutesdeployment default",
      "15 minutes",
    ]);
    expect(within(group).getByRole("radio", { name: /^5 minutes/ })).toHaveAttribute("aria-checked", "true");
    // No pending change: no Apply button visible yet.
    expect(screen.queryByRole("button", { name: /Apply/ })).not.toBeInTheDocument();

    await actor.click(within(group).getByRole("radio", { name: "1 minute" }));
    expect(screen.getByRole("button", { name: "Apply 1 minute lease" })).toBeDisabled();
    await actor.type(screen.getByLabelText("Reason for lease window change"), "demo");
    await actor.click(screen.getByRole("button", { name: "Apply 1 minute lease" }));

    expect(put).toHaveBeenCalledWith(cfg, session, 60, "demo", 0);
    expect(await screen.findByText("Permission lease is now 1 minute for newly vended credentials.")).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledTimes(1);
    // The status chip reflects the runtime source after the change.
    expect(screen.getByText("1 minute · runtime dial")).toBeInTheDocument();
  });

  it("keeps the pending selection and shows the broker validation error", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    vi.spyOn(api, "setEnforcement").mockRejectedValue(
      new ApiError("permission_lease_seconds must be one of 60, 300, 900.", 400, "invalid_request_error"),
    );
    const onApplied = vi.fn();
    render(<EnforcementDialCard cfg={cfg} onApplied={onApplied} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    await actor.click(within(group).getByRole("radio", { name: "15 minutes" }));
    await actor.type(screen.getByLabelText("Reason for lease window change"), "validation test");
    await actor.click(screen.getByRole("button", { name: "Apply 15 minutes lease" }));

    expect(await screen.findByText(/must be one of 60, 300, 900/)).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: "15 minutes" })).toHaveAttribute("aria-checked", "true");
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("reconciles a version conflict to the latest runtime dial", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    vi.spyOn(api, "setEnforcement").mockRejectedValue(
      new ApiError("Changed", 409, "version_conflict", {
        current_enforcement: {
          ...enforcement,
          permission_lease_seconds: 900,
          source: "runtime",
          generation: 4,
        },
      }),
    );
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    await actor.click(within(group).getByRole("radio", { name: "1 minute" }));
    await actor.type(screen.getByLabelText("Reason for lease window change"), "stale change");
    await actor.click(screen.getByRole("button", { name: "Apply 1 minute lease" }));

    expect(await screen.findByText(/changed in another session/)).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: "15 minutes" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("15 minutes · runtime dial")).toBeInTheDocument();
  });

  it("supports arrow-key navigation as a single accessible radio group", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    const current = within(group).getByRole("radio", { name: /^5 minutes/ });
    current.focus();
    await actor.keyboard("{ArrowRight}");

    const next = within(group).getByRole("radio", { name: "15 minutes" });
    expect(next).toHaveAttribute("aria-checked", "true");
    expect(next).toHaveFocus();
    expect(current).toHaveAttribute("tabindex", "-1");
    expect(next).toHaveAttribute("tabindex", "0");
  });

  it("recovers from a failed load with Retry", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement")
      .mockRejectedValueOnce(new ApiError("Unavailable", 503, "service_unavailable"))
      .mockResolvedValueOnce(enforcement);
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
  });
});

describe("emergency stop", () => {
  it("requires the key, the exact phrase, and a reason before stopping all sessions", async () => {
    const actor = userEvent.setup();
    const post = vi.spyOn(api, "setEmergencyStop").mockResolvedValue({
      state: "activating",
      desired_active: true,
      generation: 1,
      requested_at: "2026-09-09T10:06:00Z",
      idempotent: false,
      retry: false,
    });
    const onApplied = vi.fn();
    render(<EmergencyStopCard cfg={cfg} emergency={operations.emergency} onApplied={onApplied} session={session} />);

    await actor.click(screen.getByRole("button", { name: "Activate emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Activate emergency stop" });
    const submit = within(dialog).getByRole("button", { name: "Stop all sessions" });
    expect(submit).toBeDisabled();

    await actor.type(within(dialog).getByLabelText("Emergency key"), "break-glass-secret");
    await actor.type(within(dialog).getByLabelText("Emergency reason"), "incident 4711");
    expect(submit).toBeDisabled();
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "STOP_ALL_BEDROCK");
    expect(submit).toBeDisabled();
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "_SESSIONS");
    expect(submit).toBeEnabled();
    await actor.click(submit);

    expect(post).toHaveBeenCalledWith(cfg, session, {
      action: "activate",
      confirmation: "STOP_ALL_BEDROCK_SESSIONS",
      reason: "incident 4711",
      emergencyKey: "break-glass-secret",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText(/Emergency stop requested/)).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  it("offers recovery with its own phrase when the stop is active", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setEmergencyStop").mockResolvedValue({
      state: "recovering",
      desired_active: false,
      generation: 2,
      requested_at: "2026-09-09T10:07:00Z",
    });
    render(
      <EmergencyStopCard
        cfg={cfg}
        emergency={{ ...operations.emergency, state: "active", desired_active: true, converged: true }}
        onApplied={vi.fn()}
        session={session}
      />,
    );

    await actor.click(screen.getByRole("button", { name: "Recover from emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Recover from emergency stop" });
    expect(within(dialog).getByText("RESTORE_ALL_BEDROCK_SESSIONS", { selector: "code" })).toBeInTheDocument();
  });

  it("keeps the dialog open and surfaces a rejected break-glass key", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setEmergencyStop").mockRejectedValue(
      new ApiError("Break-glass emergency authorization required.", 403, "forbidden"),
    );
    render(<EmergencyStopCard cfg={cfg} emergency={operations.emergency} onApplied={vi.fn()} session={session} />);

    await actor.click(screen.getByRole("button", { name: "Activate emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Activate emergency stop" });
    await actor.type(within(dialog).getByLabelText("Emergency key"), "wrong-key");
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "STOP_ALL_BEDROCK_SESSIONS");
    await actor.type(within(dialog).getByLabelText("Emergency reason"), "incident");
    await actor.click(within(dialog).getByRole("button", { name: "Stop all sessions" }));

    expect(await within(dialog).findByText(/not authorized/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Activate emergency stop" })).toBeInTheDocument();
  });
});

describe("operations view", () => {
  // OperationsView hosts LiveLeases, which polls the lease snapshot; keep it
  // deterministic in every operations-view test.
  beforeEach(() => {
    vi.spyOn(api, "leaseSnapshot").mockResolvedValue({ users: [], next_cursor: null });
    vi.spyOn(api, "reconciliation").mockResolvedValue({ enabled: false, runs: [], message: "off" });
  });

  it("renders controls, health cards, and alarms together", async () => {
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(
      <OperationsView
        cfg={cfg}
        error=""
        loading={false}
        onChanged={vi.fn()}
        operations={operations}
        session={session}
        stale={false}
      />,
    );

    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
    expect(screen.getByText("Permission lease dial")).toBeInTheDocument();
    expect(screen.getByText("Emergency stop", { selector: "#emergency-title" })).toBeInTheDocument();
    expect(screen.getByText("Always on · 19 shards")).toBeInTheDocument();
    expect(screen.getByText("5 min · runtime dial")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Operational alarms")).getByText(/enforcement dispatch dlq/)).toBeInTheDocument();
  });

  it("shows live leases on the operations panel", async () => {
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    const expiresAt = new Date(Date.now() + 240_000).toISOString();
    vi.spyOn(api, "leaseSnapshot").mockResolvedValue({
      users: [
        {
          user_id: "lease-holder",
          name: "Lease Holder",
          status: "active",
          status_reason: "",
          status_origin: "admin",
          granularity: "user",
          version: 3,
          created_at: "2026-09-09T09:00:00Z",
          updated_at: "2026-09-09T10:00:00Z",
          limits: {
            daily: { usd: 1, input_tokens: 100, output_tokens: 50 },
            weekly: null,
            monthly: null,
          },
          lease: {
            active: true,
            expires_at: expiresAt,
            refresh_after: null,
            generation: 1,
            granted_at: new Date(Date.now() - 60_000).toISOString(),
            lease_seconds: 300,
          },
        },
      ],
      next_cursor: null,
    });
    render(
      <OperationsView
        cfg={cfg}
        error=""
        loading={false}
        onChanged={vi.fn()}
        operations={operations}
        session={session}
        stale={false}
      />,
    );

    const leases = await screen.findByLabelText("Live leases");
    expect(await within(leases).findByText(/Lease Holder · granted/)).toBeInTheDocument();
  });

  it("keeps the controls reachable when operational telemetry is unavailable", async () => {
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(
      <OperationsView
        cfg={cfg}
        error="Operations unavailable"
        loading={false}
        onChanged={vi.fn()}
        operations={null}
        session={session}
        stale={false}
      />,
    );

    expect(screen.getByText("Operations unavailable")).toBeInTheDocument();
    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate emergency stop" })).toBeDisabled();
  });
});

const reconciliationRun = {
  day: "2026-09-12",
  run_at: "2026-09-14T06:00:12Z",
  region: "us-east-1",
  aggregate: { estimated_usd: 5, billed_usd: 5.5, delta_usd: 0.5, delta_percent: 9.091 },
  workloads: [
    { workload_id: "workload:payments", name: "payments", estimated_usd: 2, billed_usd: 2.1, delta_usd: 0.1, delta_percent: 4.762, tag_inactive: false },
  ],
  tag_inactive_workloads: [],
};

describe("spend reconciliation card", () => {
  it("says the feature is off instead of showing a zero delta", async () => {
    vi.spyOn(api, "reconciliation").mockResolvedValue({ enabled: false, runs: [], message: "off" });
    render(<SpendReconciliationCard alarmState={null} cfg={cfg} session={session} />);
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("Not compared")).toBeInTheDocument();
    expect(screen.getByText(/reconciliation_enabled=true/)).toBeInTheDocument();
    expect(screen.queryByText(/Ledger estimate/)).not.toBeInTheDocument();
  });

  it("renders the latest ledger-vs-bill comparison with a signed delta", async () => {
    const response: ReconciliationResponse = {
      enabled: true,
      lag_days: 2,
      runs: [reconciliationRun],
      latest: reconciliationRun,
    };
    const get = vi.spyOn(api, "reconciliation").mockResolvedValue(response);
    render(<SpendReconciliationCard alarmState="OK" cfg={cfg} session={session} />);
    expect(await screen.findByText("Compared")).toBeInTheDocument();
    expect(screen.getByText("2026-09-12 (D-2)")).toBeInTheDocument();
    expect(screen.getByText("$5.00")).toBeInTheDocument();
    expect(screen.getByText("$5.50")).toBeInTheDocument();
    expect(screen.getByText("$0.50 · +9.09%")).toBeInTheDocument();
    expect(screen.getByText("payments +4.76%")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(cfg, session, 14);
  });

  it("flags an inactive cost-allocation tag and an active drift alarm", async () => {
    const inactive = {
      ...reconciliationRun,
      workloads: [{ ...reconciliationRun.workloads[0], billed_usd: 0, delta_usd: -2, delta_percent: null, tag_inactive: true }],
      tag_inactive_workloads: ["payments"],
    };
    vi.spyOn(api, "reconciliation").mockResolvedValue({ enabled: true, lag_days: 2, runs: [inactive], latest: inactive });
    render(<SpendReconciliationCard alarmState="ALARM" cfg={cfg} session={session} />);
    expect(await screen.findByText("Tag inactive")).toBeInTheDocument();
    expect(screen.getByText("Cost-allocation tag inactive: payments")).toBeInTheDocument();
  });

  it("shows an awaiting state before the first scheduled run and surfaces load errors", async () => {
    vi.spyOn(api, "reconciliation").mockResolvedValueOnce({ enabled: true, lag_days: 2, runs: [], latest: null });
    const { unmount } = render(<SpendReconciliationCard alarmState={null} cfg={cfg} session={session} />);
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    expect(screen.getByText("Daily · compares day D-2")).toBeInTheDocument();
    unmount();

    vi.spyOn(api, "reconciliation").mockRejectedValueOnce(new ApiError("quota service down", 503, "service_unavailable"));
    render(<SpendReconciliationCard alarmState={null} cfg={cfg} session={session} />);
    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/temporarily unavailable/)).toBeInTheDocument();
  });

  it("derives the card tone from the latest run", () => {
    expect(reconciliationTone(null, 10)).toBe("gray");
    expect(reconciliationTone({ enabled: false, runs: [] }, 10)).toBe("gray");
    expect(reconciliationTone({ enabled: true, runs: [reconciliationRun], latest: reconciliationRun }, 10)).toBe("green");
    expect(reconciliationTone({ enabled: true, runs: [reconciliationRun], latest: reconciliationRun }, 5)).toBe("red");
    const inactive = { ...reconciliationRun, tag_inactive_workloads: ["payments"] };
    expect(reconciliationTone({ enabled: true, runs: [inactive], latest: inactive }, 10)).toBe("amber");
    const noBill = { ...reconciliationRun, aggregate: { ...reconciliationRun.aggregate, delta_percent: null } };
    expect(reconciliationTone({ enabled: true, runs: [noBill], latest: noBill }, 10)).toBe("gray");
  });
});

describe("auto-block sweep card", () => {
  const run: AutoBlockSweep["last_run"] = {
    ran_at: "2026-09-15T00:05:04Z",
    dry_run: false,
    evaluated: 3,
    lifted: 2,
    still_blocked: 1,
    admin_blocked: 0,
    raced: 0,
    lifted_users: ["alice", "bob"],
    failures: [],
  };

  it("shows the last nightly pass with lifted and still-blocked counts", () => {
    render(<AutoBlockSweepCard alarmState="OK" sweep={{ schedule: "00:05 UTC daily", status: "ok", last_run: run }} />);
    expect(screen.getByText("Auto-block sweep")).toBeInTheDocument();
    expect(screen.getByText("Ran")).toBeInTheDocument();
    expect(screen.getByText("00:05 UTC daily")).toBeInTheDocument();
    expect(screen.getByText("2 / 1")).toBeInTheDocument();
    expect(screen.getByText("3 blocked")).toBeInTheDocument();
    expect(screen.getByText("Admin blocks kept")).toBeInTheDocument();
    expect(screen.queryByText("Failures")).not.toBeInTheDocument();
  });

  it("says never ran before the first pass instead of showing zeros", () => {
    render(<AutoBlockSweepCard alarmState={null} sweep={{ schedule: "00:05 UTC daily", status: "never_ran", last_run: null }} />);
    expect(screen.getByText("Never ran")).toBeInTheDocument();
    expect(screen.getByText("No pass recorded yet")).toBeInTheDocument();
    expect(screen.queryByText(/\d+ \/ \d+/)).not.toBeInTheDocument();
  });

  it("turns red with the failure detail when the last pass failed", () => {
    const failedRun = { ...run, failures: [{ user_id: "carol", error: "throttled" }] };
    render(<AutoBlockSweepCard alarmState="ALARM" sweep={{ schedule: "00:05 UTC daily", status: "failed", last_run: failedRun }} />);
    expect(screen.getByText("Failed")).toHaveClass("ops-status-red");
    expect(screen.getByText("carol: throttled")).toBeInTheDocument();
    expect(screen.queryByText(/earlier pass failed/)).not.toBeInTheDocument();
  });

  it("does not let the one-day alarm call a repaired pass failed", () => {
    // Failed at 00:05, repaired by a manual pass: the state row says ok, the
    // alarm still rings for the rest of its one-day period.
    render(<AutoBlockSweepCard alarmState="ALARM" sweep={{ schedule: "00:05 UTC daily", status: "ok", last_run: run }} />);
    expect(screen.getByText("Alarm clearing")).toHaveClass("ops-status-amber");
    expect(screen.getByText(/earlier pass failed today/)).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("Failures")).not.toBeInTheDocument();
  });
});
