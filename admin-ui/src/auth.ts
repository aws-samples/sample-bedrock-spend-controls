import { fromCognitoIdentityPool } from "@aws-sdk/credential-provider-cognito-identity";
import { AwsClient } from "aws4fetch";
import type { AdminConfig } from "./config";

const PKCE_STORAGE_KEY = "bedrockQuotaAdmin.pkce";
const PKCE_MAX_AGE_MS = 10 * 60 * 1000;
const RENEWAL_WINDOW_MS = 60 * 1000;

interface PendingAuthorization {
  verifier: string;
  state: string;
  nonce: string;
  redirectUri: string;
  createdAt: number;
}

interface TokenSet {
  idToken: string;
  refreshToken?: string;
  expiresAt: number;
}

interface TokenResponse {
  id_token?: unknown;
  refresh_token?: unknown;
}

export interface Signer {
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
}

export interface AuthorizationContext {
  idToken: string;
  signer: Signer;
}

export interface Session {
  readonly email: string;
  authorization(): Promise<AuthorizationContext>;
  reauthenticate(): Promise<void>;
  logout(): void;
}

interface IdentityCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
  expiration?: Date;
}

type CredentialsProvider = () => Promise<IdentityCredentials>;

export interface AuthDependencies {
  crypto: Crypto;
  fetch: typeof fetch;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  now: () => number;
  navigate: (url: string) => void;
  replaceUrl: (url: string) => void;
  createCredentialsProvider: (
    cfg: AdminConfig,
    idToken: string,
  ) => CredentialsProvider;
  createSigner: (cfg: AdminConfig, credentials: IdentityCredentials) => Signer;
  onReauthenticate: () => void;
  origin: string;
}

export type AuthDependencyOverrides = Partial<AuthDependencies>;

export interface AuthorizationRequest {
  url: string;
  pending: PendingAuthorization;
}

interface IdTokenClaims {
  aud?: string | string[];
  email?: string;
  exp?: number;
  iss?: string;
  nonce?: string;
  preferred_username?: string;
  sub?: string;
  token_use?: string;
  "cognito:username"?: string;
}

/** Endpoints resolved from the issuer's OIDC discovery document. */
export interface ProviderMetadata {
  authorizationEndpoint: string;
  tokenEndpoint: string;
  endSessionEndpoint?: string;
}

const callbackExchanges = new Map<string, Promise<Session>>();
const providerMetadataCache = new Map<string, Promise<ProviderMetadata>>();

/** Test hook: discovery is cached per issuer for the lifetime of the page. */
export function resetProviderMetadataCache(): void {
  providerMetadataCache.clear();
}

function normalizedIssuer(value: string): string {
  return value.replace(/\/+$/, "");
}

function httpsEndpoint(value: unknown, name: string): string {
  if (typeof value !== "string" || !value) {
    throw new Error(`The discovery document does not contain a valid ${name}.`);
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`The discovery document does not contain a valid ${name}.`);
  }
  if (url.protocol !== "https:" || url.username || url.password) {
    throw new Error(`The discovery document does not contain a valid ${name}.`);
  }
  return url.toString();
}

async function fetchProviderMetadata(
  cfg: AdminConfig,
  deps: AuthDependencies,
): Promise<ProviderMetadata> {
  const url = `${normalizedIssuer(cfg.issuer)}/.well-known/openid-configuration`;
  const response = await deps.fetch(url);
  if (!response.ok) {
    throw new Error(`OIDC discovery failed (${response.status}).`);
  }
  let document: unknown;
  try {
    document = await response.json();
  } catch {
    throw new Error("The identity provider returned an invalid discovery document.");
  }
  const record = (document ?? {}) as Record<string, unknown>;
  if (
    typeof record.issuer !== "string" ||
    normalizedIssuer(record.issuer) !== normalizedIssuer(cfg.issuer)
  ) {
    throw new Error("The discovery document issuer does not match this deployment.");
  }
  const metadata: ProviderMetadata = {
    authorizationEndpoint: httpsEndpoint(
      record.authorization_endpoint,
      "authorization_endpoint",
    ),
    tokenEndpoint: httpsEndpoint(record.token_endpoint, "token_endpoint"),
  };
  if (record.end_session_endpoint !== undefined) {
    metadata.endSessionEndpoint = httpsEndpoint(
      record.end_session_endpoint,
      "end_session_endpoint",
    );
  }
  return metadata;
}

function providerMetadata(
  cfg: AdminConfig,
  deps: AuthDependencies,
): Promise<ProviderMetadata> {
  const key = normalizedIssuer(cfg.issuer);
  const cached = providerMetadataCache.get(key);
  if (cached) return cached;
  const pending = fetchProviderMetadata(cfg, deps).catch((error) => {
    providerMetadataCache.delete(key);
    throw error;
  });
  providerMetadataCache.set(key, pending);
  return pending;
}

function dependencies(overrides: AuthDependencyOverrides = {}): AuthDependencies {
  return {
    crypto: globalThis.crypto,
    fetch: globalThis.fetch.bind(globalThis),
    storage: globalThis.sessionStorage,
    now: () => Date.now(),
    navigate: (url) => window.location.assign(url),
    replaceUrl: (url) => window.history.replaceState({}, document.title, url),
    createCredentialsProvider: (cfg, idToken) => {
      // Identity Pool login key: the provider URL without the scheme. This
      // matches both Cognito User Pools and IAM OIDC providers.
      const loginKey = cfg.issuer.replace(/^https:\/\//, "");
      return fromCognitoIdentityPool({
        clientConfig: { region: cfg.region },
        identityPoolId: cfg.identityPoolId,
        logins: { [loginKey]: idToken },
      });
    },
    createSigner: (cfg, credentials) => new AwsClient({
      accessKeyId: credentials.accessKeyId,
      secretAccessKey: credentials.secretAccessKey,
      sessionToken: credentials.sessionToken,
      service: "lambda",
      region: cfg.region,
    }),
    onReauthenticate: () => undefined,
    origin: window.location.origin,
    ...overrides,
  };
}

function base64Url(bytes: Uint8Array): string {
  let value = "";
  for (const byte of bytes) value += String.fromCharCode(byte);
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomValue(cryptoProvider: Crypto): string {
  const bytes = new Uint8Array(32);
  cryptoProvider.getRandomValues(bytes);
  return base64Url(bytes);
}

function callbackUri(origin: string): string {
  return new URL("/auth/callback", `${origin}/`).toString();
}

function logoutUri(origin: string): string {
  return new URL("/", `${origin}/`).toString();
}

export async function createAuthorizationRequest(
  cfg: AdminConfig,
  overrides: AuthDependencyOverrides = {},
): Promise<AuthorizationRequest> {
  const deps = dependencies(overrides);
  const metadata = await providerMetadata(cfg, deps);
  const verifier = randomValue(deps.crypto);
  const state = randomValue(deps.crypto);
  const nonce = randomValue(deps.crypto);
  const digest = await deps.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  const redirectUri = callbackUri(deps.origin);
  // Append to the discovered endpoint: some providers carry fixed query
  // parameters on their authorization_endpoint.
  const url = new URL(metadata.authorizationEndpoint);
  const parameters = new URLSearchParams({
    client_id: cfg.clientId,
    response_type: "code",
    scope: cfg.scopes,
    redirect_uri: redirectUri,
    code_challenge_method: "S256",
    code_challenge: base64Url(new Uint8Array(digest)),
    state,
    nonce,
  });
  for (const [key, value] of parameters) url.searchParams.set(key, value);
  return {
    url: url.toString(),
    pending: {
      verifier,
      state,
      nonce,
      redirectUri,
      createdAt: deps.now(),
    },
  };
}

export async function beginSignIn(
  cfg: AdminConfig,
  overrides: AuthDependencyOverrides = {},
): Promise<void> {
  const deps = dependencies(overrides);
  const request = await createAuthorizationRequest(cfg, deps);
  deps.storage.setItem(PKCE_STORAGE_KEY, JSON.stringify(request.pending));
  deps.navigate(request.url);
}

function cleanCallbackUrl(deps: AuthDependencies): void {
  deps.replaceUrl(logoutUri(deps.origin));
}

function readPendingAuthorization(deps: AuthDependencies): PendingAuthorization {
  const serialized = deps.storage.getItem(PKCE_STORAGE_KEY);
  if (!serialized) throw new Error("The sign-in request is missing or has already been used.");
  let pending: PendingAuthorization;
  try {
    pending = JSON.parse(serialized) as PendingAuthorization;
  } catch {
    throw new Error("The saved sign-in request is invalid.");
  }
  if (
    typeof pending.verifier !== "string" ||
    typeof pending.state !== "string" ||
    typeof pending.nonce !== "string" ||
    typeof pending.redirectUri !== "string" ||
    typeof pending.createdAt !== "number"
  ) {
    throw new Error("The saved sign-in request is invalid.");
  }
  if (deps.now() - pending.createdAt > PKCE_MAX_AGE_MS) {
    throw new Error("The sign-in request expired. Start sign-in again.");
  }
  return pending;
}

function validatePendingCallback(
  returnedStates: string[],
  actualCallbackUri: string,
  deps: AuthDependencies,
): PendingAuthorization {
  const pending = readPendingAuthorization(deps);
  if (returnedStates.length === 0 || !returnedStates[0]) {
    throw new Error("The OAuth callback did not include state.");
  }
  if (returnedStates.length !== 1 || pending.state !== returnedStates[0]) {
    throw new Error("The OAuth state does not match the sign-in request.");
  }
  if (
    pending.redirectUri !== callbackUri(deps.origin) ||
    pending.redirectUri !== actualCallbackUri
  ) {
    throw new Error("The OAuth callback URI does not match the sign-in request.");
  }
  return pending;
}

function sanitizeOAuthErrorText(value: string | null, maxLength = 512): string {
  return (value ?? "")
    .slice(0, maxLength * 2)
    .replace(/[\x00-\x1F\x7F]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maxLength);
}

function decodeIdToken(idToken: string): IdTokenClaims {
  const parts = idToken.split(".");
  if (parts.length !== 3) {
    throw new Error("The identity provider returned an invalid ID token.");
  }
  try {
    const encoded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, "=");
    return JSON.parse(atob(padded)) as IdTokenClaims;
  } catch {
    throw new Error("The identity provider returned an invalid ID token.");
  }
}

/**
 * Checks callback-bound claims before the token is used by the UI. Signature
 * verification remains at the Identity Pool and the broker's token verifier.
 */
export function validateIdToken(
  idToken: string,
  cfg: AdminConfig,
  now: number,
  expectedNonce?: string,
): IdTokenClaims {
  const claims = decodeIdToken(idToken);
  if (
    typeof claims.iss !== "string" ||
    normalizedIssuer(claims.iss) !== normalizedIssuer(cfg.issuer)
  ) {
    throw new Error("The ID token issuer does not match this deployment.");
  }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!audiences.includes(cfg.clientId)) {
    throw new Error("The ID token audience does not match this application.");
  }
  if (typeof claims.exp !== "number" || claims.exp * 1000 <= now) {
    throw new Error("The ID token is expired.");
  }
  if (expectedNonce !== undefined && claims.nonce !== expectedNonce) {
    throw new Error("The ID token nonce does not match the sign-in request.");
  }
  // Cognito-specific claim; other providers omit it.
  if (claims.token_use !== undefined && claims.token_use !== "id") {
    throw new Error("The identity provider did not return an ID token.");
  }
  return claims;
}

async function tokenRequest(
  cfg: AdminConfig,
  body: URLSearchParams,
  deps: AuthDependencies,
): Promise<TokenResponse> {
  const metadata = await providerMetadata(cfg, deps);
  const response = await deps.fetch(metadata.tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok) {
    throw new Error(`The token exchange failed (${response.status}).`);
  }
  try {
    return await response.json() as TokenResponse;
  } catch {
    throw new Error("The identity provider returned an invalid token response.");
  }
}

function requiredIdToken(response: TokenResponse): string {
  if (typeof response.id_token !== "string" || !response.id_token) {
    throw new Error("The identity provider did not return an ID token.");
  }
  return response.id_token;
}

function isIdentityAuthorizationError(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const record = error as { name?: unknown; code?: unknown };
  return record.name === "NotAuthorizedException"
    || record.code === "NotAuthorizedException";
}

async function exchangeAuthorizationCode(
  cfg: AdminConfig,
  code: string,
  returnedStates: string[],
  actualCallbackUri: string,
  deps: AuthDependencies,
): Promise<Session> {
  try {
    const pending = validatePendingCallback(returnedStates, actualCallbackUri, deps);
    const response = await tokenRequest(
      cfg,
      new URLSearchParams({
        grant_type: "authorization_code",
        client_id: cfg.clientId,
        code,
        redirect_uri: pending.redirectUri,
        code_verifier: pending.verifier,
      }),
      deps,
    );
    const idToken = requiredIdToken(response);
    const claims = validateIdToken(idToken, cfg, deps.now(), pending.nonce);
    return new ManagedSession(
      cfg,
      {
        idToken,
        refreshToken: typeof response.refresh_token === "string"
          ? response.refresh_token
          : undefined,
        expiresAt: claims.exp! * 1000,
      },
      claims,
      deps,
    );
  } finally {
    deps.storage.removeItem(PKCE_STORAGE_KEY);
  }
}

export async function handleAuthCallback(
  cfg: AdminConfig,
  overrides: AuthDependencyOverrides = {},
  href = window.location.href,
): Promise<Session | null> {
  const deps = dependencies(overrides);
  const url = new URL(href);
  const code = url.searchParams.get("code");
  const oauthError = url.searchParams.get("error");
  if (!code && !oauthError) return null;

  cleanCallbackUrl(deps);
  const returnedStates = url.searchParams.getAll("state");
  const returnedState = returnedStates[0] ?? null;
  const actualCallbackUri = new URL(url.pathname, url.origin).toString();
  if (oauthError) {
    validatePendingCallback(returnedStates, actualCallbackUri, deps);
    deps.storage.removeItem(PKCE_STORAGE_KEY);
    const description = sanitizeOAuthErrorText(url.searchParams.get("error_description"));
    const errorCode = sanitizeOAuthErrorText(oauthError, 128);
    throw new Error(description || (errorCode
      ? `Sign-in failed (${errorCode}).`
      : "Sign-in failed."));
  }
  if (!returnedState) {
    deps.storage.removeItem(PKCE_STORAGE_KEY);
    throw new Error("The OAuth callback did not include state.");
  }

  const exchangeKey = `${returnedState}:${code}`;
  const existing = callbackExchanges.get(exchangeKey);
  if (existing) return existing;
  const exchange = exchangeAuthorizationCode(
    cfg,
    code!,
    returnedStates,
    actualCallbackUri,
    deps,
  );
  callbackExchanges.set(exchangeKey, exchange);
  return exchange;
}

export function createSessionFromIdToken(
  cfg: AdminConfig,
  idToken: string,
  refreshToken: string | undefined,
  overrides: AuthDependencyOverrides = {},
): Session {
  const deps = dependencies(overrides);
  const claims = validateIdToken(idToken, cfg, deps.now());
  return new ManagedSession(
    cfg,
    { idToken, refreshToken, expiresAt: claims.exp! * 1000 },
    claims,
    deps,
  );
}

class ManagedSession implements Session {
  readonly email: string;
  private tokens: TokenSet | null;
  private credentialsProvider: CredentialsProvider | null = null;
  private signer: Signer | null = null;
  private credentialsExpireAt = 0;
  private tokenRenewal: Promise<string> | null = null;
  private credentialRenewal: Promise<Signer> | null = null;
  private reauthentication: Promise<void> | null = null;

  constructor(
    private readonly cfg: AdminConfig,
    tokens: TokenSet,
    claims: IdTokenClaims,
    private readonly deps: AuthDependencies,
  ) {
    this.tokens = tokens;
    this.email = claims.email
      || claims.preferred_username
      || claims["cognito:username"]
      || claims.sub
      || "Administrator";
  }

  async authorization(): Promise<AuthorizationContext> {
    const idToken = await this.currentIdToken();
    const signer = await this.currentSigner(idToken);
    return { idToken, signer };
  }

  private async currentIdToken(): Promise<string> {
    if (this.tokens && this.tokens.expiresAt > this.deps.now() + RENEWAL_WINDOW_MS) {
      return this.tokens.idToken;
    }
    if (!this.tokenRenewal) {
      this.tokenRenewal = this.refreshIdToken().finally(() => {
        this.tokenRenewal = null;
      });
    }
    return this.tokenRenewal;
  }

  private async refreshIdToken(): Promise<string> {
    const refreshToken = this.tokens?.refreshToken;
    if (!refreshToken) {
      await this.reauthenticate();
      throw new Error("Reauthentication started because the session expired.");
    }
    try {
      const response = await tokenRequest(
        this.cfg,
        new URLSearchParams({
          grant_type: "refresh_token",
          client_id: this.cfg.clientId,
          refresh_token: refreshToken,
        }),
        this.deps,
      );
      const idToken = requiredIdToken(response);
      const claims = validateIdToken(idToken, this.cfg, this.deps.now());
      this.tokens = {
        idToken,
        refreshToken: typeof response.refresh_token === "string"
          ? response.refresh_token
          : refreshToken,
        expiresAt: claims.exp! * 1000,
      };
      // The Identity Pool provider captures the ID token in its login map.
      this.credentialsProvider = null;
      this.signer = null;
      this.credentialsExpireAt = 0;
      return idToken;
    } catch (error) {
      await this.reauthenticate();
      throw error;
    }
  }

  private async currentSigner(idToken: string): Promise<Signer> {
    if (this.signer && this.credentialsExpireAt > this.deps.now() + RENEWAL_WINDOW_MS) {
      return this.signer;
    }
    if (!this.credentialRenewal) {
      this.credentialRenewal = this.refreshSigner(idToken).finally(() => {
        this.credentialRenewal = null;
      });
    }
    return this.credentialRenewal;
  }

  private async refreshSigner(idToken: string): Promise<Signer> {
    if (!this.credentialsProvider) {
      this.credentialsProvider = this.deps.createCredentialsProvider(this.cfg, idToken);
    }
    let credentials: IdentityCredentials;
    try {
      credentials = await this.credentialsProvider();
    } catch (error) {
      if (isIdentityAuthorizationError(error)) await this.reauthenticate();
      throw error;
    }
    this.signer = this.deps.createSigner(this.cfg, credentials);
    this.credentialsExpireAt = credentials.expiration?.getTime()
      ?? this.deps.now() + 5 * 60 * 1000;
    return this.signer;
  }

  async reauthenticate(): Promise<void> {
    if (!this.reauthentication) {
      this.clearLocalState();
      this.deps.onReauthenticate();
      this.reauthentication = beginSignIn(this.cfg, this.deps);
    }
    return this.reauthentication;
  }

  logout(): void {
    const idToken = this.tokens?.idToken;
    this.clearLocalState();
    this.deps.storage.removeItem(PKCE_STORAGE_KEY);
    void this.redirectThroughProviderLogout(idToken);
  }

  private async redirectThroughProviderLogout(
    idToken: string | undefined,
  ): Promise<void> {
    const localUri = logoutUri(this.deps.origin);
    try {
      const metadata = await providerMetadata(this.cfg, this.deps);
      if (!metadata.endSessionEndpoint) {
        // No RP-initiated logout: the local session is already cleared.
        this.deps.navigate(localUri);
        return;
      }
      const url = new URL(metadata.endSessionEndpoint);
      // Send the OIDC-standard and the Cognito parameter names together;
      // each provider honors its own and ignores the other (verified
      // against Cognito's /logout, which requires logout_uri and tolerates
      // the standard names).
      url.searchParams.set("client_id", this.cfg.clientId);
      url.searchParams.set("logout_uri", localUri);
      url.searchParams.set("post_logout_redirect_uri", localUri);
      if (idToken) url.searchParams.set("id_token_hint", idToken);
      this.deps.navigate(url.toString());
    } catch {
      this.deps.navigate(localUri);
    }
  }

  private clearLocalState(): void {
    this.tokens = null;
    this.credentialsProvider = null;
    this.signer = null;
    this.credentialsExpireAt = 0;
  }
}
