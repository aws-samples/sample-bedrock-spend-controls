import { describe, expect, it, vi } from "vitest";
import type { Session } from "./auth";
import type { AdminConfig } from "./config";
import {
  ApiError,
  LEASE_SNAPSHOT_MAX_PAGES,
  api,
  apiErrorMessage,
  queryString,
  transport,
  type CurrentUsage,
  type QuotaLimits,
  type UserRow,
} from "./api";

const cfg: AdminConfig = {
  gatewayUrl: "https://gateway.example.test",
  region: "us-east-1",
  issuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
  clientId: "client",
  identityPoolId: "us-east-1:identity",
  scopes: "openid email profile",
};

function sessionWith(fetch: ReturnType<typeof vi.fn>): Session {
  return {
    email: "admin@example.test",
    authorization: vi.fn().mockResolvedValue({
      idToken: "jwt-token",
      signer: { fetch },
    }),
    reauthenticate: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn(),
  };
}

function response(body: BodyInit | null, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers });
}

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return response(JSON.stringify(body), status, { "Content-Type": "application/json", ...headers });
}

const limits: QuotaLimits = {
  daily: { usd: 10, input_tokens: 100, output_tokens: 50 },
  weekly: null,
  monthly: null,
};

const currentUsage: CurrentUsage = {
  daily: { period: "daily", window: "2026-09-02", window_start: "2026-09-02T00:00:00+00:00", window_end: "2026-09-03T00:00:00+00:00", resets_at: "2026-09-03T00:00:00+00:00", cost_usd: 2, input_tokens: 20, output_tokens: 10, requests: 3 },
  weekly: { period: "weekly", window: "2026-08-31", window_start: "2026-08-31T00:00:00+00:00", window_end: "2026-09-07T00:00:00+00:00", resets_at: "2026-09-07T00:00:00+00:00", cost_usd: 2, input_tokens: 20, output_tokens: 10, requests: 3 },
  monthly: { period: "monthly", window: "2026-09-01", window_start: "2026-09-01T00:00:00+00:00", window_end: "2026-10-01T00:00:00+00:00", resets_at: "2026-10-01T00:00:00+00:00", cost_usd: 2, input_tokens: 20, output_tokens: 10, requests: 3 },
};

const user: UserRow = {
  user_id: "tenant/alice",
  name: "Alice",
  status: "active",
  status_reason: "created",
  status_origin: "admin",
  granularity: "user",
  version: 7,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
  limits,
  today: { cost_usd: 2, input_tokens: 20, output_tokens: 10, requests: 3 },
  current_usage: currentUsage,
};

describe("transport", () => {
  it("returns empty responses and response metadata without parsing failures", async () => {
    const fetch = vi.fn().mockResolvedValue(response(null, 204, {
      ETag: '"8"',
      "X-Request-Id": "request-8",
    }));

    const result = await transport<undefined>(cfg, sessionWith(fetch), "DELETE", "/empty");

    expect(result).toEqual({ data: undefined, etag: '"8"', requestId: "request-8", status: 204 });
  });

  it("accepts plain text and rejects declared malformed JSON safely", async () => {
    const plainFetch = vi.fn().mockResolvedValue(response("accepted", 200, { "Content-Type": "text/plain" }));
    await expect(transport<string>(cfg, sessionWith(plainFetch), "GET", "/plain"))
      .resolves.toMatchObject({ data: "accepted" });

    const malformedFetch = vi.fn().mockResolvedValue(response("{broken", 200, { "Content-Type": "application/json" }));
    await expect(transport(cfg, sessionWith(malformedFetch), "GET", "/broken"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
  });

  it("preserves structured error status, code, details, and request ID", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      error: {
        type: "version_conflict",
        message: "Changed",
        details: { current_user: { ...user, version: 8 } },
      },
    }, 409, { "X-Request-Id": "conflict-request" }));

    const caught = await transport(cfg, sessionWith(fetch), "PUT", "/conflict").catch((error) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({
      message: "Changed",
      status: 409,
      code: "version_conflict",
      requestId: "conflict-request",
      details: { current_user: { version: 8 } },
    });
  });

  it("uses useful fallbacks for plain-text HTTP and network failures", async () => {
    const deniedFetch = vi.fn().mockResolvedValue(response("Access denied", 403, { "Content-Type": "text/plain" }));
    await expect(transport(cfg, sessionWith(deniedFetch), "GET", "/denied"))
      .rejects.toMatchObject({ status: 403, code: "forbidden", message: "Access denied" });

    const networkFetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(transport(cfg, sessionWith(networkFetch), "GET", "/offline"))
      .rejects.toMatchObject({ status: 0, code: "network_error", message: "Failed to fetch" });
  });
  it("reports sign-in, Identity Pool, and token-refresh failures as authentication errors", async () => {
    const fetch = vi.fn();
    const session = sessionWith(fetch);
    (session.authorization as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("NotAuthorizedException: Token is not from a supported provider of this identity pool."),
    );

    const caught = await transport(cfg, session, "GET", "/admin/summary").catch((error) => error);

    expect(caught).toMatchObject({ status: 0, code: "auth_error" });
    expect(fetch).not.toHaveBeenCalled();
    const message = apiErrorMessage(caught);
    expect(message).toMatch(/Sign-in could not be completed/);
    expect(message).toMatch(/NotAuthorizedException/);
    expect(message).not.toMatch(/Unable to reach the broker/);

    // A genuine network failure keeps the reachability wording.
    const offline = await transport(cfg, sessionWith(vi.fn().mockRejectedValue(new TypeError("Failed to fetch"))), "GET", "/x").catch((error) => error);
    expect(apiErrorMessage(offline)).toMatch(/Unable to reach the broker/);
  });

  it("triggers managed reauthentication on a 401 response", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      error: { type: "unauthorized", message: "Expired" },
    }, 401));
    const session = sessionWith(fetch);

    await expect(transport(cfg, session, "GET", "/expired"))
      .rejects.toMatchObject({ status: 401, code: "unauthorized" });

    expect(session.reauthenticate).toHaveBeenCalledOnce();
    expect(session.authorization).toHaveBeenCalledOnce();
  });
});

describe("endpoint response validation", () => {
  it("turns empty or structurally invalid successful JSON into typed API errors", async () => {
    const emptyFetch = vi.fn().mockResolvedValue(response(null, 200));
    await expect(api.summary(cfg, sessionWith(emptyFetch)))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });

    const invalidMutationFetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: user.user_id,
      status: "blocked",
      reason: "policy request",
    }, 200, { "X-Request-Id": "invalid-body" }));
    await expect(api.setStatus(cfg, sessionWith(invalidMutationFetch), user, "blocked", "policy request"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response", requestId: "invalid-body" });

    const wrongIdentity = { ...user, user_id: "tenant/bob", status: "blocked" as const, status_reason: "policy request", version: 8 };
    const mismatchedFetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: "tenant/bob",
      status: "blocked",
      reason: "policy request",
      user: wrongIdentity,
    }, 200));
    await expect(api.setStatus(cfg, sessionWith(mismatchedFetch), user, "blocked", "policy request"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
  });
});

describe("mutation requests", () => {
  it("sends a generated idempotency key, If-Match version, and trimmed status reason", async () => {
    const randomUuid = vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000001",
    );
    const canonical = { ...user, version: 8, status: "blocked" as const, status_reason: "policy request" };
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: user.user_id,
      status: "blocked",
      reason: "policy request",
      user: canonical,
    }, 200));

    await api.setStatus(cfg, sessionWith(fetch), user, "blocked", "  policy request  ");

    expect(randomUuid).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://gateway.example.test/admin/user/status?user_id=tenant%2Falice");
    expect(init.headers).toMatchObject({
      "Idempotency-Key": "00000000-0000-4000-8000-000000000001",
      "If-Match": '"7"',
      "X-Quota-User-Token": "jwt-token",
    });
    expect(JSON.parse(String(init.body))).toEqual({ status: "blocked", reason: "policy request" });
  });

  it("uses canonical query routes for slash and route-suffix user IDs", async () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000099",
    );
    const routeUser = { ...user, user_id: "tenant/alice/usage-history/audit/limits/status" };
    const { today: _today, current_usage: _currentUsage, ...canonical } = routeUser;
    const updatedLimits: QuotaLimits = {
      daily: { usd: 12, input_tokens: 120, output_tokens: 60 },
      weekly: null,
      monthly: null,
    };
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ user: canonical, current_usage: currentUsage }, 200, { ETag: '"7"' }))
      .mockResolvedValueOnce(jsonResponse({
        user_id: routeUser.user_id,
        period: "daily",
        start: "2026-09-01",
        end: "2026-09-02",
        usage: [],
        next_cursor: null,
      }, 200))
      .mockResolvedValueOnce(jsonResponse({
        user_id: routeUser.user_id,
        events: [],
        next_cursor: null,
      }, 200))
      .mockResolvedValueOnce(jsonResponse({
        user_id: routeUser.user_id,
        updated: true,
        limits: updatedLimits,
        user: { ...canonical, limits: updatedLimits, version: 8 },
      }, 200, { ETag: '"8"' }))
      .mockResolvedValueOnce(jsonResponse({
        user_id: routeUser.user_id,
        status: "blocked",
        reason: "policy request",
        user: {
          ...canonical,
          status: "blocked",
          status_reason: "policy request",
          version: 8,
        },
      }, 200, { ETag: '"8"' }));
    const clientSession = sessionWith(fetch);

    await api.getUser(cfg, clientSession, routeUser.user_id);
    await api.usageHistory(cfg, clientSession, routeUser.user_id, {
      start: "2026-09-01",
      end: "2026-09-02",
    });
    await api.listUserAuditPage(cfg, clientSession, routeUser.user_id);
    await api.setLimits(cfg, clientSession, routeUser, { limits: updatedLimits });
    await api.setStatus(
      cfg,
      clientSession,
      routeUser,
      "blocked",
      "policy request",
    );

    const urls = fetch.mock.calls.map(([value]) => new URL(String(value)));
    expect(urls.map((url) => url.pathname)).toEqual([
      "/admin/user",
      "/admin/user/usage-history",
      "/admin/user/audit",
      "/admin/user/limits",
      "/admin/user/status",
    ]);
    for (const url of urls) {
      expect(url.searchParams.get("user_id")).toBe(routeUser.user_id);
      expect(url.pathname).not.toMatch(/^\/admin\/users\//);
    }
    expect(urls[1].searchParams.get("limit")).toBe("25");
    expect(urls[1].searchParams.get("start")).toBe("2026-09-01");
    expect(urls[1].searchParams.get("end")).toBe("2026-09-02");
    expect(urls[2].searchParams.get("limit")).toBe("25");

    for (const callIndex of [3, 4]) {
      const init = fetch.mock.calls[callIndex][1] as RequestInit;
      expect(init.headers).toMatchObject({
        "Idempotency-Key": "00000000-0000-4000-8000-000000000099",
        "If-Match": '"7"',
      });
    }
  });

  it("changes the runtime lease dial without redeploying", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      permission_lease_seconds: 60,
      source: "runtime",
      generation: 3,
      actor: "admin@example.test",
      reason: "incident response",
      updated_at: "2026-09-09T10:00:00Z",
    }, 200));

    await api.setEnforcement(
      cfg,
      sessionWith(fetch),
      60,
      "  incident response  ",
      2,
    );

    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://gateway.example.test/admin/enforcement");
    expect(init.method).toBe("PUT");
    expect(init.headers).toMatchObject({
      "If-Match": '"2"',
      "Idempotency-Key": expect.any(String),
    });
    expect(JSON.parse(String(init.body))).toEqual({
      permission_lease_seconds: 60,
      reason: "incident response",
    });
  });

  it("sends the break-glass key only in the emergency request header", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      state: "activating",
      desired_active: true,
      generation: 1,
      requested_at: "2026-09-09T10:00:00Z",
      idempotent: false,
      retry: false,
    }, 202));

    await api.setEmergencyStop(cfg, sessionWith(fetch), {
      action: "activate",
      confirmation: "STOP_ALL_BEDROCK_SESSIONS",
      reason: "  incident response  ",
      emergencyKey: "break-glass-secret",
    });

    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://gateway.example.test/admin/emergency-stop");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      "X-Quota-Emergency-Key": "break-glass-secret",
      "X-Quota-User-Token": "jwt-token",
    });
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({
      action: "activate",
      confirmation: "STOP_ALL_BEDROCK_SESSIONS",
      reason: "incident response",
    });
    expect(JSON.stringify(body)).not.toContain("break-glass-secret");
  });
});


describe("query string encoding", () => {
  it("encodes spaces as %20 so the signed URL matches the URL sent", () => {
    // URLSearchParams would write "Jane+Doe"; aws4fetch signs "Jane%20Doe".
    expect(queryString({ query: "Jane Doe" })).toBe("?query=Jane%20Doe");
    expect(queryString({ query: "Jane Doe" })).not.toContain("+");
    // RFC 3986 reserved characters that encodeURIComponent leaves alone.
    expect(queryString({ q: "a*b!c'(d)" })).toBe("?q=a%2Ab%21c%27%28d%29");
    expect(queryString({ user_id: "tenant/alice", limit: 25 })).toBe("?user_id=tenant%2Falice&limit=25");
    // Undefined, null, and empty values are omitted; no params gives no "?".
    expect(queryString({ a: undefined, b: null, c: "" })).toBe("");
    expect(queryString({ limit: 0 })).toBe("?limit=0");
  });

  it("signs and sends the same URL for a user search containing a space", async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ users: [], next_cursor: null }, 200));
    await api.listUsersPage(cfg, sessionWith(fetch), { query: "Jane Doe", granularity: "user" });
    const url = String(fetch.mock.calls[0][0]);
    expect(url).toBe("https://gateway.example.test/admin/users?limit=25&query=Jane%20Doe&granularity=user");
    expect(url).not.toContain("+");
    // The server still decodes the intended value.
    expect(new URL(url).searchParams.get("query")).toBe("Jane Doe");
  });
});

describe("paginated operational endpoints", () => {
  it("follows next_cursor for the lease snapshot up to the page bound", async () => {
    const { today: _today, current_usage: _currentUsage, ...canonical } = user;
    const second = { ...canonical, user_id: "tenant/bob" };
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ users: [canonical], next_cursor: "page two" }, 200))
      .mockResolvedValueOnce(jsonResponse({ users: [second], next_cursor: null }, 200));

    const snapshot = await api.leaseSnapshot(cfg, sessionWith(fetch));

    expect(snapshot.users.map((entry) => entry.user_id)).toEqual(["tenant/alice", "tenant/bob"]);
    expect(snapshot.next_cursor).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(String(fetch.mock.calls[0][0])).toBe("https://gateway.example.test/admin/users?limit=50&include_usage=false");
    expect(String(fetch.mock.calls[1][0])).toBe("https://gateway.example.test/admin/users?limit=50&include_usage=false&cursor=page%20two");

    // An endless cursor stops at the bound and reports that more remains.
    const endless = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({ users: [canonical], next_cursor: "more" }, 200)));
    const bounded = await api.leaseSnapshot(cfg, sessionWith(endless));
    expect(endless).toHaveBeenCalledTimes(LEASE_SNAPSHOT_MAX_PAGES);
    expect(bounded.users).toHaveLength(LEASE_SNAPSHOT_MAX_PAGES);
    expect(bounded.next_cursor).toBe("more");
  });

  it("reads the raw emergency-stop state and rejects a malformed one", async () => {
    const okFetch = vi.fn().mockResolvedValue(jsonResponse({ state: "activating", desired_active: true, generation: 2, requested_at: "2026-09-09T10:00:00Z" }, 200));
    await expect(api.getEmergencyStop(cfg, sessionWith(okFetch))).resolves.toMatchObject({ state: "activating", desired_active: true });
    expect(String(okFetch.mock.calls[0][0])).toBe("https://gateway.example.test/admin/emergency-stop");
    expect((okFetch.mock.calls[0][1] as RequestInit).method).toBe("GET");

    const badFetch = vi.fn().mockResolvedValue(jsonResponse({ state: "active" }, 200));
    await expect(api.getEmergencyStop(cfg, sessionWith(badFetch))).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("accepts and type-checks the optional unpriced_requests usage field", async () => {
    const { today: _today, current_usage: _currentUsage, ...canonical } = user;
    const withUnpriced = Object.fromEntries(Object.entries(currentUsage).map(([period, row]) => [period, { ...row, unpriced_requests: 2 }]));
    const okFetch = vi.fn().mockResolvedValue(jsonResponse({ user: canonical, current_usage: withUnpriced }, 200));
    const detail = await api.getUser(cfg, sessionWith(okFetch), user.user_id);
    expect(detail.data.current_usage.daily.unpriced_requests).toBe(2);

    const badFetch = vi.fn().mockResolvedValue(jsonResponse({ user: canonical, current_usage: { ...currentUsage, daily: { ...currentUsage.daily, unpriced_requests: -1 } } }, 200));
    await expect(api.getUser(cfg, sessionWith(badFetch), user.user_id)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("loads exactly one 25-user page and passes opaque cursor, status, and query server-side", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ users: [user], next_cursor: "opaque {cursor}" }, 200))
      .mockResolvedValueOnce(jsonResponse({ users: [], next_cursor: null }, 200));
    const session = sessionWith(fetch);

    const first = await api.listUsersPage(cfg, session);
    expect(first.next_cursor).toBe("opaque {cursor}");
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toBe("https://gateway.example.test/admin/users?limit=25");

    await api.listUsersPage(cfg, session, {
      limit: 25,
      cursor: first.next_cursor,
      status: "blocked",
      query: " Alice / team ",
    });
    const secondUrl = new URL(String(fetch.mock.calls[1][0]));
    expect(secondUrl.searchParams.get("cursor")).toBe("opaque {cursor}");
    expect(secondUrl.searchParams.get("status")).toBe("blocked");
    expect(secondUrl.searchParams.get("query")).toBe("Alice / team");
  });

  it("creates with a UUID idempotency key and rejects a mismatched canonical identity", async () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000002");
    const { today: _today, current_usage: _currentUsage, ...canonical } = user;
    const fetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: user.user_id,
      provisioned: true,
      limits: canonical.limits,
      user: { ...canonical, version: 1 },
    }, 200, { ETag: '"1"' }));

    const result = await api.createUser(cfg, sessionWith(fetch), {
      user_id: `  ${user.user_id}  `,
      name: "  Alice  ",
      limits: canonical.limits,
    });

    expect(result.etag).toBe('"1"');
    const [, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ "Idempotency-Key": "00000000-0000-4000-8000-000000000002" });
    expect(JSON.parse(String(init.body))).toMatchObject({ user_id: user.user_id, name: "Alice" });

    const mismatched = vi.fn().mockResolvedValue(jsonResponse({
      user_id: user.user_id,
      provisioned: true,
      limits: canonical.limits,
      user: { ...canonical, user_id: "tenant/bob", version: 1 },
    }, 200));
    await expect(api.createUser(cfg, sessionWith(mismatched), {
      user_id: user.user_id,
      name: "Alice",
      limits: canonical.limits,
    })).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates detail and usage response identity consistency", async () => {
    const { today: _today, current_usage: _currentUsage, ...canonical } = user;
    const detailFetch = vi.fn().mockResolvedValue(jsonResponse({ user: canonical, current_usage: currentUsage }, 200));
    await expect(api.getUser(cfg, sessionWith(detailFetch), "tenant/bob"))
      .rejects.toMatchObject({ code: "invalid_response" });

    const usageFetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: user.user_id,
      period: "daily",
      start: "2026-08-04",
      end: "2026-09-02",
      usage: [{ user_id: "tenant/bob", period: "daily", window: "2026-09-02", window_start: "2026-09-02T00:00:00+00:00", window_end: "2026-09-03T00:00:00+00:00", resets_at: "2026-09-03T00:00:00+00:00", cost_usd: 1, input_tokens: 2, output_tokens: 3, requests: 4 }],
      next_cursor: null,
    }, 200));
    await expect(api.usageHistory(cfg, sessionWith(usageFetch), user.user_id, {
      start: "2026-08-04",
      end: "2026-09-02",
    })).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("reads reconciliation runs with a bounded limit and rejects malformed comparisons", async () => {
    const run = {
      day: "2026-09-12",
      run_at: "2026-09-14T06:00:00Z",
      aggregate: { estimated_usd: 5, billed_usd: 5.5, delta_usd: 0.5, delta_percent: 9.1 },
      workloads: [{ workload_id: "workload:p", name: "p", estimated_usd: 2, billed_usd: 0, delta_usd: -2, delta_percent: null, tag_inactive: true }],
      tag_inactive_workloads: ["p"],
    };
    const okFetch = vi.fn().mockResolvedValue(jsonResponse({ enabled: true, lag_days: 2, runs: [run], latest: run }, 200));
    const response = await api.reconciliation(cfg, sessionWith(okFetch), 7);
    expect(new URL(okFetch.mock.calls[0][0] as string).search).toBe("?limit=7");
    expect(response.latest?.workloads[0].tag_inactive).toBe(true);

    const disabledFetch = vi.fn().mockResolvedValue(jsonResponse({ enabled: false, runs: [], message: "off" }, 200));
    expect((await api.reconciliation(cfg, sessionWith(disabledFetch))).enabled).toBe(false);

    const badFetch = vi.fn().mockResolvedValue(jsonResponse({
      enabled: true,
      runs: [{ ...run, aggregate: { estimated_usd: "5", billed_usd: 5.5, delta_usd: 0.5, delta_percent: 9.1 } }],
    }, 200));
    await expect(api.reconciliation(cfg, sessionWith(badFetch))).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates global and per-user audit snapshots without accepting cross-user events", async () => {
    const snapshot = {
      user_id: user.user_id,
      name: user.name,
      status: user.status,
      status_reason: user.status_reason,
      status_origin: user.status_origin,
      version: user.version,
      created_at: user.created_at,
      updated_at: user.updated_at,
      limits: { daily: { usd_micro: 10_000_000, input_tokens: 100, output_tokens: 50 }, weekly: null, monthly: null },
    };
    const event = {
      user_id: user.user_id,
      event_key: "2026-09-02T10:00:00Z#event",
      event_type: "user.created",
      actor: "admin",
      auth_method: "jwt",
      reason: "admin user creation",
      request_id: "request-1",
      created_at: "2026-09-02T10:00:00Z",
      before: null,
      after: snapshot,
    };
    const globalFetch = vi.fn().mockResolvedValue(jsonResponse({ events: [event], next_cursor: "next" }, 200));
    await expect(api.listAuditPage(cfg, sessionWith(globalFetch))).resolves.toEqual({ events: [event], next_cursor: "next" });

    const wrongUserFetch = vi.fn().mockResolvedValue(jsonResponse({ user_id: "tenant/bob", events: [event], next_cursor: null }, 200));
    await expect(api.listUserAuditPage(cfg, sessionWith(wrongUserFetch), user.user_id))
      .rejects.toMatchObject({ code: "invalid_response" });
  });
});


describe("USD normalization", () => {
  it("normalizes create and limit writes to the backend micro-dollar precision before validation", async () => {
    const { today: _today, current_usage: _currentUsage, ...base } = user;
    const normalizedLimits: QuotaLimits = {
      daily: { ...base.limits.daily!, usd: 0.123457 },
      weekly: { usd: 0.234568, input_tokens: 200, output_tokens: 100 },
      monthly: { usd: 0.345679, input_tokens: 300, output_tokens: 150 },
    };
    const created = { ...base, version: 1, limits: normalizedLimits };
    const createFetch = vi.fn().mockResolvedValue(jsonResponse({
      user_id: base.user_id,
      provisioned: true,
      limits: normalizedLimits,
      user: created,
    }, 200));

    await expect(api.createUser(cfg, sessionWith(createFetch), {
      user_id: base.user_id,
      name: base.name,
      limits: {
        daily: { ...base.limits.daily!, usd: 0.1234567 },
        weekly: { usd: 0.2345678, input_tokens: 200, output_tokens: 100 },
        monthly: { usd: 0.3456789, input_tokens: 300, output_tokens: 150 },
      },
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    const createBody = JSON.parse(String((createFetch.mock.calls[0][1] as RequestInit).body));
    expect(createBody.limits.daily.usd).toBe(0.123457);
    expect(createBody.limits.weekly.usd).toBe(0.234568);
    expect(createBody.limits.monthly.usd).toBe(0.345679);

    const updated = { ...base, version: base.version + 1, limits: normalizedLimits };
    const limitsFetch = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({
      user_id: base.user_id,
      updated: true,
      limits: normalizedLimits,
      user: updated,
    }, 200)));
    await expect(api.setLimits(cfg, sessionWith(limitsFetch), base, {
      limits: {
        daily: { ...base.limits.daily!, usd: 0.1234567 },
        weekly: { usd: 0.2345678, input_tokens: 200, output_tokens: 100 },
        monthly: { usd: 0.3456789, input_tokens: 300, output_tokens: 150 },
      },
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    const bodyWithoutReason = JSON.parse(String((limitsFetch.mock.calls[0][1] as RequestInit).body));
    expect(bodyWithoutReason.limits.daily.usd).toBe(0.123457);
    expect(bodyWithoutReason).not.toHaveProperty("reason");

    await expect(api.setLimits(cfg, sessionWith(limitsFetch), base, {
      limits: {
        daily: { ...base.limits.daily!, usd: 0.1234567 },
        weekly: { usd: 0.2345678, input_tokens: 200, output_tokens: 100 },
        monthly: { usd: 0.3456789, input_tokens: 300, output_tokens: 150 },
      },
      reason: "  Annual allocation  ",
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    expect(JSON.parse(String((limitsFetch.mock.calls[1][1] as RequestInit).body))).toEqual({
      limits: normalizedLimits,
      reason: "Annual allocation",
    });
  });
});
