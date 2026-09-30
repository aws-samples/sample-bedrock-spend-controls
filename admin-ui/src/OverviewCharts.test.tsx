import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OverviewCharts, formatMetricValue } from "./OverviewCharts";
import { ApiError, api, type UsageMetrics } from "./api";
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

const users = [
  { user_id: "user-a", name: "Ada Lovelace" },
  { user_id: "user-b", name: "Grace Hopper" },
];

const usage: UsageMetrics = {
  status: "available",
  as_of: "2026-09-09T10:00:00Z",
  start: "2026-09-07",
  end: "2026-09-09",
  period: "daily",
  days: ["2026-09-07", "2026-09-08", "2026-09-09"],
  models: [
    {
      model: "us.amazon.nova-micro-v1:0",
      series: {
        cost_usd: [0.1, 0, 0.05],
        requests: [4, 0, 2],
        input_tokens: [400, 0, 200],
        output_tokens: [80, 0, 40],
      },
      totals: { cost_usd: 0.15, requests: 6, input_tokens: 600, output_tokens: 120 },
    },
    {
      model: "anthropic.claude-haiku",
      series: {
        cost_usd: [0, 0.2, 0],
        requests: [0, 1, 0],
        input_tokens: [0, 900, 0],
        output_tokens: [0, 120, 0],
      },
      totals: { cost_usd: 0.2, requests: 1, input_tokens: 900, output_tokens: 120 },
    },
  ],
  totals: { cost_usd: 0.35, requests: 7, input_tokens: 1500, output_tokens: 240 },
  top_users: [
    { user_id: "user-b", name: "Grace Hopper (server)", cost_usd: 0.2, requests: 1, granularity: "user" },
    { user_id: "user-a", cost_usd: 0.15, requests: 6, granularity: "user" },
    { user_id: "user-c", cost_usd: 0.01, requests: 1, granularity: "user" },
    { user_id: "workload:payments", name: "payments", cost_usd: 0.005, requests: 1, granularity: "workload" },
  ],
};

describe("metric formatting", () => {
  it("formats spend as $1,234.56 with two decimals and counts as grouped integers", () => {
    expect(formatMetricValue("cost_usd", 0.1234)).toBe("$0.12");
    expect(formatMetricValue("cost_usd", 12.3)).toBe("$12.30");
    expect(formatMetricValue("cost_usd", 1234.567)).toBe("$1,234.57");
    expect(formatMetricValue("requests", 1234)).toBe("1,234");
    expect(formatMetricValue("input_tokens", 1_234_567.4)).toBe("1,234,567");
  });
});

describe("overview usage charts", () => {
  it("renders the stacked per-model chart, legend, totals, and top users", async () => {
    vi.spyOn(api, "usageMetrics").mockResolvedValue(usage);
    render(<OverviewCharts cfg={cfg} session={session} users={users} />);

    expect(await screen.findByRole("img", { name: "Daily Spend (USD) by model" })).toBeInTheDocument();
    const legend = screen.getByLabelText("Model legend");
    expect(within(legend).getByText("us.amazon.nova-micro-v1:0")).toBeInTheDocument();
    expect(within(legend).getByText("$0.15")).toBeInTheDocument();
    expect(screen.getByText(/Range totals: \$0\.35 · 7 requests/)).toBeInTheDocument();

    const topUsers = screen.getByLabelText("Top spenders");
    // Server-resolved names win; known users map from the local page; and
    // unknown ones fall back to the raw id.
    expect(within(topUsers).getByText("Grace Hopper (server)")).toBeInTheDocument();
    expect(within(topUsers).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(topUsers).getByText("user-c")).toBeInTheDocument();
    // Workloads are labelled so a top spender is never mistaken for a person.
    expect(within(topUsers).getAllByText("workload")).toHaveLength(1);
    expect(within(topUsers).getByText("payments")).toBeInTheDocument();
  });

  it("switches the charted metric and refetches when the range changes", async () => {
    const actor = userEvent.setup();
    const spy = vi.spyOn(api, "usageMetrics").mockResolvedValue(usage);
    render(<OverviewCharts cfg={cfg} session={session} users={users} />);
    await screen.findByRole("img", { name: "Daily Spend (USD) by model" });
    expect(spy).toHaveBeenLastCalledWith(cfg, session, 14);

    await actor.selectOptions(screen.getByLabelText("Usage metric"), "requests");
    expect(screen.getByRole("img", { name: "Daily Requests by model" })).toBeInTheDocument();

    await actor.selectOptions(screen.getByLabelText("Usage range"), "7");
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(cfg, session, 7));
  });

  it("explains unavailable CloudWatch telemetry instead of charting", async () => {
    vi.spyOn(api, "usageMetrics").mockResolvedValue({
      ...usage,
      status: "unavailable",
      error_code: "AccessDenied",
      models: [],
      top_users: [],
    });
    render(<OverviewCharts cfg={cfg} session={session} users={users} />);

    expect(await screen.findByText(/CloudWatch metrics are unavailable \(AccessDenied\)/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Model legend")).not.toBeInTheDocument();
  });

  it("shows an empty message when the range has no usage and an alert on errors", async () => {
    const spy = vi.spyOn(api, "usageMetrics").mockResolvedValue({
      ...usage,
      models: [],
      totals: { cost_usd: 0, requests: 0, input_tokens: 0, output_tokens: 0 },
      top_users: [],
    });
    const { unmount } = render(<OverviewCharts cfg={cfg} session={session} users={users} />);
    expect(await screen.findByText("No Bedrock usage in the selected range.")).toBeInTheDocument();
    unmount();

    spy.mockRejectedValue(new ApiError("Gateway unavailable", 503, "service_unavailable"));
    render(<OverviewCharts cfg={cfg} session={session} users={users} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/);
  });
});
