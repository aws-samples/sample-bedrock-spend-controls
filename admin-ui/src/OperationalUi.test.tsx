import { useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CreateUserWizard, GlobalAuditView, UserDetailDrawer } from "./OperationalUi";
import { ApiError, api, type AdminUser, type AuditEvent, type CurrentUsage, type UsageHistoryResponse, type UserRow } from "./api";
import type { AdminConfig } from "./config";
import type { Session } from "./auth";

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

const currentUsage: CurrentUsage = {
  daily: { period: "daily", window: "2026-09-02", window_start: "2026-09-02T00:00:00+00:00", window_end: "2026-09-03T00:00:00+00:00", resets_at: "2026-09-03T00:00:00+00:00", cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
  weekly: { period: "weekly", window: "2026-08-31", window_start: "2026-08-31T00:00:00+00:00", window_end: "2026-09-07T00:00:00+00:00", resets_at: "2026-09-07T00:00:00+00:00", cost_usd: 6, input_tokens: 60, output_tokens: 12, requests: 5 },
  monthly: { period: "monthly", window: "2026-09-01", window_start: "2026-09-01T00:00:00+00:00", window_end: "2026-10-01T00:00:00+00:00", resets_at: "2026-10-01T00:00:00+00:00", cost_usd: 7, input_tokens: 70, output_tokens: 14, requests: 6 },
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
    weekly: null,
    monthly: null,
  },
  today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
  current_usage: currentUsage,
};

function adminUser(overrides: Partial<AdminUser> = {}): AdminUser {
  const { today: _today, current_usage: _currentUsage, ...user } = alice;
  return { ...user, ...overrides };
}

const auditEvent: AuditEvent = {
  user_id: alice.user_id,
  event_key: "2026-09-02T10:00:00Z#one",
  event_type: "user.limits.updated",
  actor: "admin@example.test",
  auth_method: "jwt",
  reason: "admin quota update",
  request_id: "request-one",
  created_at: "2026-09-02T10:00:00Z",
  before: {
    ...adminUser(),
    limits: { daily: { usd_micro: 5_000_000, input_tokens: 100, output_tokens: 20 }, weekly: null, monthly: null },
  },
  after: {
    ...adminUser({ version: 2 }),
    limits: { daily: { usd_micro: 10_000_000, input_tokens: 100, output_tokens: 20 }, weekly: null, monthly: null },
  },
};

function createResult(user: AdminUser) {
  return {
    data: { user_id: user.user_id, provisioned: true, limits: user.limits, user },
    etag: '"1"',
    requestId: "create-request",
    status: 200,
  };
}

function DrawerHarness() {
  const [open, setOpen] = useState(false);
  const [user, setUser] = useState(alice);
  return <><button onClick={() => setOpen(true)} type="button">Open Alice</button>{open && <UserDetailDrawer cfg={cfg} onCanonical={(updated) => setUser((current) => ({ ...updated, today: current.today, current_usage: current.current_usage }))} onClose={() => setOpen(false)} onEdit={vi.fn()} onStatus={vi.fn()} session={session} user={user} />}</>;
}

describe("create user wizard", () => {
  it("validates steps, preserves Back state, confirms Unlimited, reviews, and creates only at the final action", async () => {
    const actor = userEvent.setup();
    const created = adminUser({ user_id: "tenant/new", name: "New User" });
    const createUser = vi.spyOn(api, "createUser").mockResolvedValue(createResult(created));
    const onCreated = vi.fn();
    const onClose = vi.fn();
    render(<CreateUserWizard cfg={cfg} onClose={onClose} onCreated={onCreated} session={session} />);

    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("alert", { name: "Create user errors" })).toHaveTextContent("immutable user identity");
    expect(createUser).not.toHaveBeenCalled();

    await actor.type(screen.getByLabelText("User identity claim value"), "tenant/new");
    await actor.type(screen.getByLabelText("Display name"), "New User");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    const usd = screen.getByLabelText("Create Daily USD limit");
    await actor.clear(usd);
    await actor.type(usd, "0");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("alert", { name: "Create user errors" })).toHaveTextContent("Unlimited");
    expect(createUser).not.toHaveBeenCalled();

    await actor.click(screen.getByRole("checkbox", { name: /each 0 value above means Unlimited/ }));
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("tenant/new")).toBeInTheDocument();
    expect(screen.getByText(/Unlimited ·/)).toBeInTheDocument();
    expect(createUser).not.toHaveBeenCalled();

    await actor.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("Create Daily USD limit")).toHaveValue(0);
    expect(screen.getByRole("checkbox", { name: /each 0 value above means Unlimited/ })).toBeChecked();
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("button", { name: "Create user" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created, true));
    expect(createUser).toHaveBeenCalledWith(cfg, session, {
      user_id: "tenant/new",
      name: "New User",
      limits: {
        daily: { usd: 0, input_tokens: 1_000_000, output_tokens: 200_000 },
        weekly: null,
        monthly: null,
      },
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps duplicate conflicts in the wizard with existing-user guidance", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "createUser").mockRejectedValue(new ApiError("Already exists", 409, "user_already_exists"));
    render(<CreateUserWizard cfg={cfg} onClose={vi.fn()} onCreated={vi.fn()} session={session} />);

    await actor.type(screen.getByLabelText("User identity claim value"), alice.user_id);
    await actor.type(screen.getByLabelText("Display name"), alice.name);
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("button", { name: "Create user" }));

    expect(await screen.findByText(/already exists.*search for the existing user/i)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Create user" })).toBeInTheDocument();
  });

  it("creates an optional weekly calendar limit as part of one mutation", async () => {
    const actor = userEvent.setup();
    const created = adminUser({ user_id: "tenant/weekly", name: "Weekly User" });
    const createUser = vi.spyOn(api, "createUser").mockResolvedValue(createResult(created));
    render(<CreateUserWizard cfg={cfg} onClose={vi.fn()} onCreated={vi.fn()} session={session} />);

    await actor.type(screen.getByLabelText("User identity claim value"), "tenant/weekly");
    await actor.type(screen.getByLabelText("Display name"), "Weekly User");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("checkbox", { name: "Weekly" }));
    await actor.clear(screen.getByLabelText("Create Weekly USD limit"));
    await actor.type(screen.getByLabelText("Create Weekly USD limit"), "5");
    await actor.clear(screen.getByLabelText("Create Weekly input token limit"));
    await actor.type(screen.getByLabelText("Create Weekly input token limit"), "5000000");
    await actor.clear(screen.getByLabelText("Create Weekly output token limit"));
    await actor.type(screen.getByLabelText("Create Weekly output token limit"), "1000000");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("button", { name: "Create user" }));

    expect(createUser).toHaveBeenCalledWith(cfg, session, {
      user_id: "tenant/weekly",
      name: "Weekly User",
      limits: {
        daily: { usd: 1, input_tokens: 1_000_000, output_tokens: 200_000 },
        weekly: { usd: 5, input_tokens: 5_000_000, output_tokens: 1_000_000 },
        monthly: null,
      },
    });
  });

  it("does not allow every calendar period to be disabled", async () => {
    const actor = userEvent.setup();
    render(<CreateUserWizard cfg={cfg} onClose={vi.fn()} onCreated={vi.fn()} session={session} />);
    await actor.type(screen.getByLabelText("User identity claim value"), "tenant/unbounded");
    await actor.type(screen.getByLabelText("Display name"), "Unbounded");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    await actor.click(screen.getByRole("checkbox", { name: "Daily" }));
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("alert", { name: "Create user errors" })).toHaveTextContent("Enable at least one calendar quota period");
  });
});

describe("user detail drawer", () => {
  it("lists, adds, and removes per-model budgets with a reason", async () => {
    const actor = userEvent.setup();
    const withBudget = adminUser({
      version: 2,
      model_budgets: {
        "us.anthropic.claude-opus-4-7": {
          daily: { usd: 2, input_tokens: 0, output_tokens: 0, thresholds: [{ at: 1, action: "warn" }] },
          weekly: null,
          monthly: null,
        },
      },
    });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: withBudget, current_usage: currentUsage }, etag: '"2"', requestId: null, status: 200 });
    vi.spyOn(api, "modelUsage").mockResolvedValue({ user_id: alice.user_id, model_id: "us.anthropic.claude-opus-4-7", current_usage: { ...currentUsage, daily: { ...currentUsage.daily, cost_usd: 1.25 } } });
    const afterAdd = adminUser({
      version: 3,
      model_budgets: {
        ...withBudget.model_budgets,
        "openai.gpt-oss-20b": { daily: { usd: 0.5, input_tokens: 1000, output_tokens: 0, thresholds: [{ at: 0.5, action: "warn" }, { at: 1, action: "block" }] }, weekly: null, monthly: null },
      },
    });
    const setModelBudget = vi.spyOn(api, "setModelBudget").mockResolvedValue({ data: { user_id: alice.user_id, model_id: "openai.gpt-oss-20b", updated: true, model_budgets: afterAdd.model_budgets!, user: afterAdd }, etag: '"3"', requestId: null, status: 200 });
    const afterRemove = adminUser({ version: 4, model_budgets: { "openai.gpt-oss-20b": afterAdd.model_budgets!["openai.gpt-oss-20b"] } });
    const removeModelBudget = vi.spyOn(api, "removeModelBudget").mockResolvedValue({ data: { user_id: alice.user_id, model_id: "us.anthropic.claude-opus-4-7", removed: true, model_budgets: afterRemove.model_budgets!, user: afterRemove }, etag: '"4"', requestId: null, status: 200 });
    vi.spyOn(window, "prompt").mockReturnValue("No longer needed");

    render(<DrawerHarness />);
    await actor.click(screen.getByRole("button", { name: "Open Alice" }));
    const table = await screen.findByRole("region", { name: "Model budgets" });
    expect(table).toHaveTextContent("us.anthropic.claude-opus-4-7");
    expect(table).toHaveTextContent("100:warn");
    expect(table).toHaveTextContent("alert-only");
    await waitFor(() => expect(table).toHaveTextContent("$1.25 of $2.00"));

    // Add a new model budget with thresholds and a reason.
    await actor.click(screen.getByRole("button", { name: "Add model budget" }));
    await actor.type(screen.getByLabelText("Model budget model ID"), "openai.gpt-oss-20b");
    const usd = screen.getByLabelText("Model budget daily USD limit");
    await actor.clear(usd);
    await actor.type(usd, "0.5");
    const input = screen.getByLabelText("Model budget daily input token limit");
    await actor.clear(input);
    await actor.type(input, "1000");
    await actor.type(screen.getByLabelText("Model budget daily thresholds"), "50:warn,100:block");
    await actor.type(screen.getByLabelText("Model budget reason"), "Cheap model, tight cap");
    await actor.click(screen.getByRole("button", { name: "Add model budget" }));

    await waitFor(() => expect(setModelBudget).toHaveBeenCalledWith(
      cfg, session, expect.objectContaining({ user_id: alice.user_id, version: 2 }), "openai.gpt-oss-20b",
      { daily: { usd: 0.5, input_tokens: 1000, output_tokens: 0, thresholds: [{ at: 0.5, action: "warn" }, { at: 1, action: "block" }] }, weekly: null, monthly: null },
      "Cheap model, tight cap",
    ));
    expect(await screen.findByText("openai.gpt-oss-20b")).toBeInTheDocument();

    // Remove the first one; the prompt supplies the audit reason.
    await actor.click(screen.getByRole("button", { name: "Remove us.anthropic.claude-opus-4-7 budget" }));
    await waitFor(() => expect(removeModelBudget).toHaveBeenCalledWith(
      cfg, session, expect.objectContaining({ version: 3 }), "us.anthropic.claude-opus-4-7", "No longer needed",
    ));
    await waitFor(() => expect(screen.queryByText("us.anthropic.claude-opus-4-7")).not.toBeInTheDocument());
  });

  it("rejects an ARN and a malformed thresholds spec in the model budget form", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: adminUser(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    const setModelBudget = vi.spyOn(api, "setModelBudget");
    render(<DrawerHarness />);
    await actor.click(screen.getByRole("button", { name: "Open Alice" }));
    await actor.click(await screen.findByRole("button", { name: "Add model budget" }));
    await actor.type(screen.getByLabelText("Model budget model ID"), "arn:aws:bedrock:us-east-1::foundation-model/x");
    await actor.click(screen.getByRole("button", { name: "Add model budget" }));
    expect(screen.getByRole("alert")).toHaveTextContent("not an ARN");
    await actor.clear(screen.getByLabelText("Model budget model ID"));
    await actor.type(screen.getByLabelText("Model budget model ID"), "opus");
    await actor.type(screen.getByLabelText("Model budget daily thresholds"), "100:block,150:warn");
    await actor.click(screen.getByRole("button", { name: "Add model budget" }));
    expect(screen.getByRole("alert")).toHaveTextContent("single trailing block");
    expect(setModelBudget).not.toHaveBeenCalled();
  });

  it("traps/restores focus, supports arrow-key tabs, and does not load audit before Changes", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: adminUser(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    vi.spyOn(api, "usageHistory").mockResolvedValue({ user_id: alice.user_id, period: "daily", start: "2026-08-04", end: "2026-09-02", usage: [], next_cursor: null });
    const audit = vi.spyOn(api, "listUserAuditPage").mockResolvedValue({ user_id: alice.user_id, events: [auditEvent], next_cursor: null });
    render(<DrawerHarness />);

    const opener = screen.getByRole("button", { name: "Open Alice" });
    await actor.click(opener);
    expect(await screen.findByRole("dialog", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close user details" })).toHaveFocus();
    await actor.tab({ shift: true });
    // The last focusable element in the overview is now the per-model
    // budgets "Add" button (it follows the calendar/rate sections).
    expect(screen.getByRole("button", { name: "Add model budget" })).toHaveFocus();
    await actor.tab();
    expect(screen.getByRole("button", { name: "Close user details" })).toHaveFocus();
    expect(audit).not.toHaveBeenCalled();

    const overview = screen.getByRole("tab", { name: "Overview" });
    overview.focus();
    await actor.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Usage" })).toHaveAttribute("aria-selected", "true");
    expect(audit).not.toHaveBeenCalled();
    await actor.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(audit).toHaveBeenCalledOnce());

    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Alice Example" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("shows usage pages newest-first, preserves data through errors, and applies an inclusive range", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: adminUser(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    const first: UsageHistoryResponse = {
      user_id: alice.user_id,
      period: "daily",
      start: "2026-08-27",
      end: "2026-09-02",
      usage: [{ user_id: alice.user_id, period: "daily", window: "2026-09-02", window_start: "2026-09-02T00:00:00+00:00", window_end: "2026-09-03T00:00:00+00:00", resets_at: "2026-09-03T00:00:00+00:00", cost_usd: 1.25, input_tokens: 100, output_tokens: 50, requests: 3 }],
      next_cursor: "usage-next",
    };
    const empty: UsageHistoryResponse = { ...first, usage: [], next_cursor: null };
    const usage = vi.spyOn(api, "usageHistory")
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(empty)
      .mockResolvedValueOnce(first)
      .mockRejectedValueOnce(new ApiError("Range outside retention", 400, "usage_range_outside_retention"));
    render(<DrawerHarness />);

    await actor.click(screen.getByRole("button", { name: "Open Alice" }));
    await actor.click(screen.getByRole("tab", { name: "Usage" }));
    expect(await screen.findByText("2026-09-02")).toBeInTheDocument();
    expect(usage.mock.calls[0][3]).toEqual({ limit: 25, cursor: null, period: "daily" });
    expect(screen.getByLabelText("Start date")).toHaveValue("2026-08-27");
    expect(screen.getByLabelText("End date")).toHaveValue("2026-09-02");
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("No usage was recorded in this date range.")).toBeInTheDocument();
    expect(usage.mock.calls[1][3]).toMatchObject({ start: "2026-08-27", end: "2026-09-02", cursor: "usage-next" });
    await actor.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("2026-09-02")).toBeInTheDocument();

    const start = screen.getByLabelText("Start date");
    const end = screen.getByLabelText("End date");
    await actor.clear(start);
    await actor.type(start, "2026-08-10");
    await actor.clear(end);
    await actor.type(end, "2026-09-01");
    await actor.click(screen.getByRole("button", { name: "Apply range" }));
    expect(await screen.findByText("Range outside retention")).toBeInTheDocument();
    expect(screen.getByText("2026-09-02")).toBeInTheDocument();
    expect(screen.getByText("Showing the previous usage page")).toBeInTheDocument();
    expect(usage.mock.calls[3][3]).toMatchObject({ start: "2026-08-10", end: "2026-09-01", cursor: null });
  });

  it("switches retained usage history between calendar periods", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: adminUser(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    const usage = vi.spyOn(api, "usageHistory")
      .mockResolvedValueOnce({ user_id: alice.user_id, period: "daily", start: "2026-08-04", end: "2026-09-02", usage: [], next_cursor: null })
      .mockResolvedValueOnce({ user_id: alice.user_id, period: "weekly", start: "2026-08-04", end: "2026-09-02", usage: [{ user_id: alice.user_id, ...currentUsage.weekly }], next_cursor: null });
    render(<DrawerHarness />);

    await actor.click(screen.getByRole("button", { name: "Open Alice" }));
    await actor.click(screen.getByRole("tab", { name: "Usage" }));
    await waitFor(() => expect(usage).toHaveBeenCalledTimes(1));
    await actor.selectOptions(screen.getByLabelText("Usage history period"), "weekly");
    await waitFor(() => expect(usage).toHaveBeenCalledTimes(2));

    expect(usage.mock.calls[1][3]).toMatchObject({ period: "weekly" });
    expect(await screen.findByLabelText("Weekly usage history")).toBeInTheDocument();
  });
});

describe("user detail drawer status hint", () => {
  it("explains how an automatic block lifts, and says nothing for admin blocks", async () => {
    const autoBlocked = adminUser({ status: "blocked", status_origin: "automatic", status_reason: "auto: daily USD quota exhausted in 2026-09-14" });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: autoBlocked, current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    const { unmount } = render(<UserDetailDrawer cfg={cfg} onCanonical={vi.fn()} onClose={vi.fn()} onEdit={vi.fn()} onStatus={vi.fn()} session={session} user={{ ...alice, ...autoBlocked }} />);
    expect(await screen.findByText("Lifts")).toBeInTheDocument();
    expect(screen.getByText(/nightly sweep/)).toBeInTheDocument();
    unmount();

    const adminBlocked = adminUser({ status: "blocked", status_origin: "admin", status_reason: "incident freeze" });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: adminBlocked, current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<UserDetailDrawer cfg={cfg} onCanonical={vi.fn()} onClose={vi.fn()} onEdit={vi.fn()} onStatus={vi.fn()} session={session} user={{ ...alice, ...adminBlocked }} />);
    expect(await screen.findByText("incident freeze")).toBeInTheDocument();
    expect(screen.queryByText("Lifts")).not.toBeInTheDocument();
    expect(screen.queryByText(/nightly sweep/)).not.toBeInTheDocument();
  });
});

describe("global audit", () => {
  it("lazy page component paginates and exposes explicit user targets with concise text", async () => {
    const actor = userEvent.setup();
    const second = { ...auditEvent, event_key: "2026-09-01T10:00:00Z#two", event_type: "user.created", before: null };
    const audit = vi.spyOn(api, "listAuditPage")
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: "audit-next" })
      .mockResolvedValueOnce({ events: [second], next_cursor: null });
    const onTargetUser = vi.fn();
    render(<GlobalAuditView cfg={cfg} onTargetUser={onTargetUser} session={session} />);

    expect(await screen.findByText(/USD:.*→/)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: alice.user_id }));
    expect(onTargetUser).toHaveBeenCalledWith(alice.user_id);
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText(/Created active/)).toBeInTheDocument();
    expect(audit.mock.calls[1][2]).toMatchObject({ cursor: "audit-next", limit: 25 });
  });

  it("refreshes explicitly from the first page and records the last successful client load", async () => {
    const actor = userEvent.setup();
    let now = Date.parse("2026-09-02T12:00:00Z");
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const refreshed = {
      ...auditEvent,
      event_key: "2026-09-02T12:05:00Z#refreshed",
      reason: "refreshed event",
    };
    const audit = vi.spyOn(api, "listAuditPage")
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: "old-next" })
      .mockResolvedValueOnce({ events: [refreshed], next_cursor: "new-next" });
    render(<GlobalAuditView cfg={cfg} onTargetUser={vi.fn()} session={session} />);

    expect(await screen.findByText(auditEvent.reason)).toBeInTheDocument();
    expect(screen.getByText(`Loaded ${new Date(now).toLocaleString()}`)).toBeInTheDocument();
    now = Date.parse("2026-09-02T12:05:00Z");
    await actor.click(screen.getByRole("button", { name: "Refresh audit log" }));

    expect(await screen.findByText("refreshed event")).toBeInTheDocument();
    expect(screen.queryByText(auditEvent.reason)).not.toBeInTheDocument();
    expect(screen.getByText(`Loaded ${new Date(now).toLocaleString()}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(audit).toHaveBeenNthCalledWith(2, cfg, session, { limit: 25, cursor: null });
  });

  it("offers Retry after an initial failure and repeats the failed first-page request", async () => {
    const actor = userEvent.setup();
    const audit = vi.spyOn(api, "listAuditPage")
      .mockRejectedValueOnce(new ApiError("Audit unavailable", 503, "service_unavailable"))
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: null });
    render(<GlobalAuditView cfg={cfg} onTargetUser={vi.fn()} session={session} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
    expect(screen.queryByText("No administrative changes were found.")).not.toBeInTheDocument();
    expect(screen.getByText("The audit log could not be loaded. Retry the request.")).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(auditEvent.reason)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/^Loaded /)).toBeInTheDocument();
    expect(audit).toHaveBeenNthCalledWith(2, cfg, session, { limit: 25, cursor: null });
  });

  it("preserves the last successful page and timestamp when refresh fails", async () => {
    const actor = userEvent.setup();
    const loadedAt = Date.parse("2026-09-02T13:00:00Z");
    vi.spyOn(Date, "now").mockReturnValue(loadedAt);
    vi.spyOn(api, "listAuditPage")
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: null })
      .mockRejectedValueOnce(new ApiError("Refresh failed", 503, "service_unavailable"));
    render(<GlobalAuditView cfg={cfg} onTargetUser={vi.fn()} session={session} />);

    expect(await screen.findByText(auditEvent.reason)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Refresh audit log" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
    expect(screen.getByText(auditEvent.reason)).toBeInTheDocument();
    expect(screen.getByText(`Showing cached audit data loaded ${new Date(loadedAt).toLocaleString()}.`)).toBeInTheDocument();
    expect(screen.getByText(`Loaded ${new Date(loadedAt).toLocaleString()}`)).toBeInTheDocument();
  });

  it("retries the exact failed pagination cursor and preserves cursor history", async () => {
    const actor = userEvent.setup();
    const loadedAt = Date.parse("2026-09-02T13:30:00Z");
    vi.spyOn(Date, "now").mockReturnValue(loadedAt);
    const second = { ...auditEvent, event_key: "second-page", reason: "second audit page" };
    const audit = vi.spyOn(api, "listAuditPage")
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: "opaque audit cursor" })
      .mockRejectedValueOnce(new ApiError("Next failed", 503, "service_unavailable"))
      .mockResolvedValueOnce({ events: [second], next_cursor: null })
      .mockResolvedValueOnce({ events: [auditEvent], next_cursor: "opaque audit cursor" });
    render(<GlobalAuditView cfg={cfg} onTargetUser={vi.fn()} session={session} />);

    expect(await screen.findByText(auditEvent.reason)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
    expect(screen.getByText(auditEvent.reason)).toBeInTheDocument();
    expect(screen.getByText(`Showing cached audit data loaded ${new Date(loadedAt).toLocaleString()}.`)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(second.reason)).toBeInTheDocument();
    expect(audit).toHaveBeenNthCalledWith(3, cfg, session, {
      limit: 25,
      cursor: "opaque audit cursor",
    });
    await actor.click(screen.getByRole("button", { name: "Previous" }));

    expect(await screen.findByText(auditEvent.reason)).toBeInTheDocument();
    expect(audit).toHaveBeenNthCalledWith(4, cfg, session, { limit: 25, cursor: null });
  });

  it("ignores a superseded audit response that completes after a refresh", async () => {
    const actor = userEvent.setup();
    type Page = { events: AuditEvent[]; next_cursor: string | null };
    let resolveInitial!: (page: Page) => void;
    let resolveLatest!: (page: Page) => void;
    const initial = new Promise<Page>((resolve) => { resolveInitial = resolve; });
    const latest = new Promise<Page>((resolve) => { resolveLatest = resolve; });
    const audit = vi.spyOn(api, "listAuditPage")
      .mockImplementationOnce(() => initial)
      .mockImplementationOnce(() => latest);
    render(<GlobalAuditView cfg={cfg} onTargetUser={vi.fn()} session={session} />);
    await waitFor(() => expect(audit).toHaveBeenCalledTimes(1));

    await actor.click(screen.getByRole("button", { name: "Refresh audit log" }));
    const latestEvent = { ...auditEvent, event_key: "latest", reason: "latest request" };
    await act(async () => {
      resolveLatest({ events: [latestEvent], next_cursor: null });
      await latest;
    });
    expect(await screen.findByText("latest request")).toBeInTheDocument();

    const oldEvent = { ...auditEvent, event_key: "old", reason: "older request" };
    await act(async () => {
      resolveInitial({ events: [oldEvent], next_cursor: null });
      await initial;
    });
    expect(screen.getByText("latest request")).toBeInTheDocument();
    expect(screen.queryByText("older request")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
