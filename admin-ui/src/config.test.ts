import { afterEach, describe, expect, it } from "vitest";
import { loadConfig } from "./config";

const valid = {
  gatewayUrl: "https://gateway.example.test/",
  region: "us-east-1",
  issuer: "https://issuer.example.test",
  clientId: "client",
  identityPoolId: "us-east-1:identity",
};

afterEach(() => {
  delete window.QUOTA_ADMIN_CONFIG;
});

describe("runtime configuration", () => {
  it("normalizes the issuer and applies scope defaults", () => {
    window.QUOTA_ADMIN_CONFIG = { ...valid, issuer: "https://issuer.example.test/" };

    expect(loadConfig()).toEqual({
      ...valid,
      gatewayUrl: "https://gateway.example.test",
      issuer: "https://issuer.example.test",
      scopes: "openid email profile",
    });
  });

  it("accepts issuers with a path, as corporate IdPs use", () => {
    window.QUOTA_ADMIN_CONFIG = {
      ...valid,
      issuer: "https://login.microsoftonline.com/tenant-id/v2.0",
      scopes: "openid email profile offline_access",
    };

    const config = loadConfig();
    expect(config.issuer).toBe("https://login.microsoftonline.com/tenant-id/v2.0");
    expect(config.scopes).toBe("openid email profile offline_access");
  });

  it("rejects a missing or non-HTTPS issuer", () => {
    window.QUOTA_ADMIN_CONFIG = { ...valid, issuer: "" };
    expect(() => loadConfig()).toThrow("issuer");

    window.QUOTA_ADMIN_CONFIG = { ...valid, issuer: "http://issuer.example.test" };
    expect(() => loadConfig()).toThrow("HTTPS");

    window.QUOTA_ADMIN_CONFIG = {
      ...valid,
      issuer: "https://issuer.example.test/?tenant=1",
    };
    expect(() => loadConfig()).toThrow("query");
  });

  it("names every missing key at once", () => {
    window.QUOTA_ADMIN_CONFIG = { region: "us-east-1" };

    expect(() => loadConfig()).toThrow(
      "missing: gatewayUrl, issuer, clientId, identityPoolId",
    );
  });
});
