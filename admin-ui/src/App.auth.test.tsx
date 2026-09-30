import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const authMocks = vi.hoisted(() => ({
  beginSignIn: vi.fn(),
  handleAuthCallback: vi.fn(),
}));

vi.mock("./auth", () => ({
  beginSignIn: authMocks.beginSignIn,
  handleAuthCallback: authMocks.handleAuthCallback,
}));

import { App } from "./App";

afterEach(() => {
  authMocks.beginSignIn.mockReset();
  authMocks.handleAuthCallback.mockReset();
  delete window.QUOTA_ADMIN_CONFIG;
  window.history.replaceState({}, "", "/");
});

describe("authentication bootstrap", () => {
  it("replays the original callback URL under StrictMode after the URL is scrubbed", async () => {
    window.history.replaceState({}, "", "/auth/callback?code=one-use&state=expected");
    const callbackUrl = window.location.href;
    window.QUOTA_ADMIN_CONFIG = {
      gatewayUrl: "https://gateway.example.test",
      region: "us-east-1",
      issuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
      clientId: "client",
      identityPoolId: "us-east-1:identity",
      scopes: "openid email profile",
    };
    authMocks.handleAuthCallback.mockImplementation(async () => {
      window.history.replaceState({}, "", "/");
      return null;
    });

    render(<StrictMode><App /></StrictMode>);

    await screen.findByRole("button", { name: "Continue to sign in" });
    await waitFor(() => expect(authMocks.handleAuthCallback.mock.calls.length).toBeGreaterThan(1));
    expect(authMocks.handleAuthCallback.mock.calls.every(
      (call) => call[2] === callbackUrl,
    )).toBe(true);
  });
});
