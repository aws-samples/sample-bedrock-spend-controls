import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { LiveLeases } from "./OperationalUi";
import { api, type UserRow } from "./api";
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

function bruno(leased: boolean): UserRow {
  const inOneMinute = new Date(Date.now() + 60_000).toISOString();
  const justNow = new Date(Date.now() - 1_000).toISOString();
  return {
    user_id: "bruno-sub",
    name: "Bruno Vega",
    status: "active",
    status_reason: "",
    status_origin: "admin",
    granularity: "user",
    version: 1,
    created_at: justNow,
    updated_at: justNow,
    limits: { daily: { usd: 0.35, input_tokens: 0, output_tokens: 0 }, weekly: null, monthly: null },
    lease: leased ? {
      active: true,
      expires_at: inOneMinute,
      refresh_after: justNow,
      generation: 3,
      granted_at: justNow,
      lease_seconds: 60,
    } : null,
    today: { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
    current_usage: {
      daily: { period: "daily", window: justNow.slice(0, 10), window_start: justNow, window_end: inOneMinute, resets_at: inOneMinute, cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
      weekly: { period: "weekly", window: justNow.slice(0, 10), window_start: justNow, window_end: inOneMinute, resets_at: inOneMinute, cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
      monthly: { period: "monthly", window: justNow.slice(0, 10), window_start: justNow, window_end: inOneMinute, resets_at: inOneMinute, cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
    },
  };
}

describe("live leases", () => {
  it("shows a vended user's lease with grant time, countdown, and renewal count", async () => {
    const snapshot = vi.spyOn(api, "leaseSnapshot").mockResolvedValue({ users: [bruno(true)], next_cursor: null });
    render(<LiveLeases cfg={cfg} configuration={{
      mode: "layered", credential_ttl_seconds: 900, permission_lease_seconds: 60,
      permission_lease_source: "runtime", permission_lease_default_seconds: 300,
      refresh_overlap_seconds: 20, refresh_jitter_seconds: 5, vend_rate_limit_per_minute: 6,
      revocation_policy_shards: 0, revocation_policy_max_characters: 0, revocation_reconcile_minutes: 0,
    }} session={session} />);
    await waitFor(() => expect(screen.getByText(/Bruno Vega/)).toBeInTheDocument());
    expect(screen.getByText(/renewal #3/)).toBeInTheDocument();
    expect(screen.getByText(/lease \d+s remaining of 60s/)).toBeInTheDocument();
    expect(screen.getByText(/deadline .* credentials valid until ≈/)).toBeInTheDocument();
    snapshot.mockRestore();
  });

  it("explains itself when no credentials are vended", async () => {
    const snapshot = vi.spyOn(api, "leaseSnapshot").mockResolvedValue({ users: [bruno(false)], next_cursor: null });
    render(<LiveLeases cfg={cfg} configuration={{
      mode: "layered", credential_ttl_seconds: 900, permission_lease_seconds: 60,
      permission_lease_source: "runtime", permission_lease_default_seconds: 300,
      refresh_overlap_seconds: 20, refresh_jitter_seconds: 5, vend_rate_limit_per_minute: 6,
      revocation_policy_shards: 0, revocation_policy_max_characters: 0, revocation_reconcile_minutes: 0,
    }} session={session} />);
    await waitFor(() => expect(screen.getByText(/No vended credentials right now/)).toBeInTheDocument());
    snapshot.mockRestore();
  });
});
