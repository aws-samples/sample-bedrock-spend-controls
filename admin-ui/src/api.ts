import type { AdminConfig } from "./config";
import type { Session } from "./auth";

export interface TransportResponse<T> {
  data: T;
  etag: string | null;
  requestId: string | null;
  status: number;
}

export class ApiError<TDetails = unknown> extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: TDetails | undefined;
  readonly requestId: string | null;

  constructor(
    message: string,
    status: number,
    code: string,
    details?: TDetails,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

type JsonObject = Record<string, unknown>;

interface TransportOptions<T> {
  body?: unknown;
  headers?: Record<string, string>;
  validate?: (data: unknown) => data is T;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function defaultErrorCode(status: number): string {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 503) return "service_unavailable";
  return "http_error";
}

function requestIdFrom(response: Response, data: unknown): string | null {
  const header = response.headers.get("X-Request-Id");
  if (header) return header;
  if (isObject(data) && typeof data.request_id === "string") return data.request_id;
  return null;
}

async function readResponseBody(response: Response, method: string, path: string): Promise<unknown> {
  let text: string;
  try {
    text = await response.text();
  } catch (caught) {
    throw new ApiError(
      caught instanceof Error ? caught.message : "The response body could not be read.",
      0,
      "network_error",
    );
  }
  if (!text) return undefined;

  try {
    return JSON.parse(text) as unknown;
  } catch {
    const contentType = response.headers.get("Content-Type") ?? "";
    if (contentType.toLowerCase().includes("json")) {
      throw new ApiError(
        `${method} ${path} returned malformed JSON (${response.status}).`,
        response.status,
        response.ok ? "invalid_response" : "invalid_error_response",
        undefined,
        response.headers.get("X-Request-Id"),
      );
    }
    return text;
  }
}

// Every admin call is (1) SigV4-signed by aws4fetch so the AWS_IAM Function URL
// accepts it, and (2) carries the OIDC ID token in X-Quota-User-Token so the
// broker's administrator authorization grants it. No admin secret is held by the browser.
export async function transport<T>(
  cfg: AdminConfig,
  session: Session,
  method: string,
  path: string,
  options: TransportOptions<T> = {},
): Promise<TransportResponse<T>> {
  let response: Response;
  try {
    const authorization = await session.authorization();
    const headers: Record<string, string> = {
      "X-Quota-User-Token": authorization.idToken,
      ...options.headers,
    };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    response = await authorization.signer.fetch(`${cfg.gatewayUrl}${path}`, {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch (caught) {
    throw new ApiError(
      caught instanceof Error ? caught.message : "The broker could not be reached.",
      0,
      "network_error",
    );
  }

  if (response.status === 401) {
    await session.reauthenticate();
  }
  const data = await readResponseBody(response, method, path);
  const requestId = requestIdFrom(response, data);
  if (!response.ok) {
    const error = isObject(data) && isObject(data.error) ? data.error : undefined;
    const message =
      (error && typeof error.message === "string" && error.message) ||
      (typeof data === "string" && data.trim()) ||
      `${method} ${path} failed (${response.status}).`;
    const code =
      (error && typeof error.code === "string" && error.code) ||
      (error && typeof error.type === "string" && error.type) ||
      defaultErrorCode(response.status);
    throw new ApiError(
      message,
      response.status,
      code,
      error?.details,
      requestId,
    );
  }

  if (options.validate && !options.validate(data)) {
    throw new ApiError(
      `${method} ${path} returned an invalid response (${response.status}).`,
      response.status,
      "invalid_response",
      undefined,
      requestId,
    );
  }

  return {
    data: data as T,
    etag: response.headers.get("ETag"),
    requestId,
    status: response.status,
  };
}

export type UserStatus = "active" | "blocked";

export type QuotaPeriod = "daily" | "weekly" | "monthly";

export type ThresholdAction = "warn" | "block";

/** One entry of a period's ordered thresholds list. `at` is a utilization
 *  ratio (1 = 100 %). At most one `block`, and only as the last entry; a
 *  list with no `block` is an alert-only period. */
export interface QuotaThreshold {
  at: number;
  action: ThresholdAction;
}

export interface PeriodLimits {
  usd: number;
  input_tokens: number;
  output_tokens: number;
  /** Omitted on write = keep the stored list (or the deployment default). */
  thresholds?: QuotaThreshold[];
}

export interface QuotaLimits {
  daily: PeriodLimits | null;
  weekly: PeriodLimits | null;
  monthly: PeriodLimits | null;
}

/** Subject-level per-minute limits; 0 disables a dimension. */
export interface RateLimits {
  rpm: number;
  tpm: number;
}

export const DEFAULT_THRESHOLDS: QuotaThreshold[] = [
  { at: 0.8, action: "warn" },
  { at: 1, action: "block" },
];

/** Client-side mirror of the server rules so the editor can explain a
 *  rejection before the request is sent. Returns null when valid. */
export function thresholdsError(thresholds: QuotaThreshold[]): string | null {
  if (thresholds.length === 0) return "Add at least one threshold.";
  let previous = 0;
  for (const [index, entry] of thresholds.entries()) {
    if (!Number.isFinite(entry.at) || entry.at <= 0 || entry.at > 10) {
      return `Threshold ${index + 1} must be greater than 0% and at most 1000%.`;
    }
    if (entry.at <= previous) return "Thresholds must be strictly increasing.";
    if (entry.action !== "warn" && entry.action !== "block") return "Choose warn or block for every threshold.";
    if (index < thresholds.length - 1 && entry.action === "block") return "A block threshold must be the last entry.";
    previous = entry.at;
  }
  return null;
}

export function isAlertOnly(thresholds: QuotaThreshold[] | undefined): boolean {
  return thresholds !== undefined && thresholds.length > 0 && thresholds.every((entry) => entry.action !== "block");
}

export interface UsageTotals {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  requests: number;
}

export interface PeriodUsage extends UsageTotals {
  period: QuotaPeriod;
  window: string;
  window_start: string;
  window_end: string;
  resets_at: string;
}

export type CurrentUsage = Record<QuotaPeriod, PeriodUsage>;

export interface LeaseState {
  active: boolean;
  expires_at: string;
  refresh_after: string | null;
  generation: number;
  granted_at: string;
  lease_seconds: number;
}

/** Identity of a workload-mode subject: an app on its own IAM principal,
 *  metered by application inference profile. `registered` is false for a
 *  metered row whose id is no longer in the deployed roster. */
export interface WorkloadIdentity {
  workload_id: string;
  name: string;
  model: string | null;
  profile_arn: string | null;
  role_arn: string | null;
  enforcement_ready: boolean;
  registered: boolean;
  tag: { key: string; value: string };
}

export interface AdminUser {
  user_id: string;
  name: string;
  status: UserStatus;
  status_reason: string;
  status_origin: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
  limits: QuotaLimits;
  /** null when neither rpm nor tpm is configured. */
  rate?: RateLimits | null;
  /** Optional second axis: budgets keyed by model ID. A breach on any
   *  model budget blocks the whole subject (enforcement is subject-wide). */
  model_budgets?: Record<string, QuotaLimits>;
  lease?: LeaseState | null;
  granularity: "user" | "workload";
  enforcement_ready?: boolean;
  /** Present when granularity is "workload". */
  workload?: WorkloadIdentity;
}

export function isWorkload(user: Pick<AdminUser, "granularity">): boolean {
  return user.granularity === "workload";
}

/** Copy for an automatic (quota-driven) block on a user, shown wherever the
 * operator could mistake it for a manual freeze. It names both lift paths so
 * "still blocked" is understood as "still over quota". */
export const AUTOMATIC_BLOCK_HINT =
  "Automatic block: it lifts by itself once the current windows are under quota, re-checked at the user's next credential request and by the nightly sweep. Unblocking now only skips that wait.";

/** Mirrors the broker's ownership rule: a block is automatic when its origin
 * is `automatic`; every other blocked row was placed by an administrator. */
export function isAutomaticBlock(user: Pick<AdminUser, "status" | "status_origin">): boolean {
  return user.status === "blocked" && user.status_origin === "automatic";
}

export interface ModelBudgetResponse {
  user_id: string;
  model_id: string;
  updated?: boolean;
  removed?: boolean;
  model_budgets: Record<string, QuotaLimits>;
  user: AdminUser;
}

export interface ModelUsageResponse {
  user_id: string;
  model_id: string;
  current_usage: CurrentUsage;
}

export interface ReconciliationComparison {
  estimated_usd: number;
  billed_usd: number;
  delta_usd: number;
  delta_percent: number | null;
}

export interface ReconciliationWorkload extends ReconciliationComparison {
  workload_id: string;
  name: string;
  tag_inactive: boolean;
}

export interface ReconciliationRun {
  day: string;
  run_at: string;
  region?: string;
  aggregate: ReconciliationComparison;
  workloads: ReconciliationWorkload[];
  tag_inactive_workloads: string[];
}

export interface ReconciliationResponse {
  enabled: boolean;
  lag_days?: number;
  message?: string;
  runs: ReconciliationRun[];
  latest?: ReconciliationRun | null;
}

function isReconciliationComparison(value: unknown): value is ReconciliationComparison {
  return isObject(value) && hasNumber(value, "estimated_usd") && hasNumber(value, "billed_usd") &&
    hasNumber(value, "delta_usd") && hasNullableNumber(value, "delta_percent");
}

function isReconciliationWorkload(value: unknown): value is ReconciliationWorkload {
  return isObject(value) && isReconciliationComparison(value) && hasString(value, "name") &&
    hasBoolean(value, "tag_inactive");
}

function isReconciliationRun(value: unknown): value is ReconciliationRun {
  return isObject(value) && hasString(value, "day") && hasString(value, "run_at") &&
    isReconciliationComparison(value.aggregate) &&
    Array.isArray(value.workloads) && value.workloads.every(isReconciliationWorkload) &&
    Array.isArray(value.tag_inactive_workloads) &&
    value.tag_inactive_workloads.every((name) => typeof name === "string");
}

export function isReconciliationResponse(value: unknown): value is ReconciliationResponse {
  return isObject(value) && hasBoolean(value, "enabled") &&
    Array.isArray(value.runs) && value.runs.every(isReconciliationRun);
}

export interface UserRow extends AdminUser {
  today: UsageTotals;
  current_usage: CurrentUsage;
}

/** One entry of GET /admin/workloads: the deployed roster joined with the
 *  metered row. `subject` is null for a workload configured at deploy time
 *  that has not invoked yet (its row appears on first metered call). */
export interface WorkloadEntry extends WorkloadIdentity {
  subject: UserRow | null;
}

export interface WorkloadListResponse {
  workloads: WorkloadEntry[];
  roster_source: string;
  tag_key: string;
}

export interface UserListResponse {
  users: UserRow[];
  next_cursor: string | null;
}

export interface AdminUserListResponse {
  users: AdminUser[];
  next_cursor: string | null;
}

export interface ListUsersOptions {
  limit?: number;
  cursor?: string | null;
  status?: UserStatus;
  query?: string;
  granularity?: "user" | "workload";
}

export interface CreateUserRequest {
  user_id: string;
  name: string;
  limits: QuotaLimits;
  rate?: RateLimits | null;
}

export interface CreateUserResponse {
  user_id: string;
  provisioned: boolean;
  limits: QuotaLimits;
  user: AdminUser;
}

export interface UserDetailResponse {
  user: AdminUser;
  current_usage: CurrentUsage;
}

export interface UsageHistoryRow extends PeriodUsage {
  user_id: string;
}

export interface UsageHistoryOptions {
  period?: QuotaPeriod;
  start?: string;
  end?: string;
  limit?: number;
  cursor?: string | null;
}

export interface UsageHistoryResponse {
  user_id: string;
  period: QuotaPeriod;
  start: string;
  end: string;
  usage: UsageHistoryRow[];
  next_cursor: string | null;
}

export interface AuditThreshold {
  at_bps: number;
  action: ThresholdAction;
}

export interface AuditPeriodLimits {
  usd_micro: number;
  input_tokens: number;
  output_tokens: number;
  thresholds?: AuditThreshold[];
}

export interface AuditSnapshotLimits {
  daily: AuditPeriodLimits | null;
  weekly: AuditPeriodLimits | null;
  monthly: AuditPeriodLimits | null;
}

export interface AuditUserSnapshot {
  user_id: string;
  name: string;
  status: UserStatus;
  status_reason: string;
  status_origin: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
  limits: AuditSnapshotLimits;
  rate?: RateLimits | null;
}

export interface AuditEvent {
  user_id: string;
  event_key: string;
  event_type: string;
  actor: string;
  auth_method: string;
  reason: string;
  request_id: string;
  created_at: string;
  before: AuditUserSnapshot | null;
  after: AuditUserSnapshot;
}

export interface AuditListOptions {
  user_id?: string;
  limit?: number;
  cursor?: string | null;
}

export interface AuditListResponse {
  events: AuditEvent[];
  next_cursor: string | null;
}

export interface UserAuditListResponse extends AuditListResponse {
  user_id: string;
}

export interface SubjectKindSummary {
  total: number;
  blocked: number;
  today: UsageTotals;
}

export interface WorkloadKindSummary extends SubjectKindSummary {
  /** Roster entries in the deployment config. */
  configured: number;
  /** Rows whose roster entry has no IAM role: metered and alerted, never hard-blocked. */
  metering_only: number;
  /** Rows with no roster entry (removed from config or created out of band). */
  unregistered: number;
  /** Roster entries with no row yet (no invocation since deploy). */
  awaiting_traffic: number;
}

export interface Summary {
  enforcement: {
    source: string;
    as_of: string;
    window: string;
    mode: string;
    credential_ttl_seconds: number;
    /** Effective runtime value (runtime dial or deployment default). */
    permission_lease_seconds: number;
    permission_lease_source: string;
    refresh_overlap_seconds: number;
    refresh_jitter_seconds: number;
    vend_rate_limit_per_minute: number;
    revocation_policy_shards: number;
    revocation_reconcile_minutes: number;
    /** All subjects (users + workloads). */
    total_users: number;
    blocked_users: number;
    blocked_user_ids: string[];
    today: UsageTotals;
    /** Same figures split by control path. */
    subjects: {
      users: SubjectKindSummary;
      workloads: WorkloadKindSummary;
    };
  };
  observability: {
    source: string;
    delivery: string;
    metrics_namespace: string;
    detection_lag_metric: string;
  };
}

export interface Operations {
  as_of: string;
  configuration: {
    mode: string;
    credential_ttl_seconds: number;
    /** Effective runtime value (runtime dial or deployment default). */
    permission_lease_seconds: number;
    permission_lease_source: string;
    permission_lease_default_seconds: number;
    refresh_overlap_seconds: number;
    refresh_jitter_seconds: number;
    vend_rate_limit_per_minute: number;
    revocation_policy_shards: number;
    revocation_policy_max_characters: number;
    revocation_reconcile_minutes: number;
  };
  emergency: {
    state: string;
    desired_active: boolean;
    generation: number;
    applied_generation: number;
    requested_at: string | null;
    applied_at: string | null;
    converged: boolean;
  };
  metrics: {
    namespace: string;
    detection_lag_metric: string;
    detection_lag_p95_ms: number | null;
    detection_lag_timestamp: string | null;
    telemetry_status: string;
    last_reconciliation_at: string | null;
    reconciliation_status: string;
    revoked_identities_desired: number | null;
    recent_sync_failure_count: number | null;
    recent_overflow_count: number | null;
    recent_emergency_failure_count: number | null;
    window_minutes: number;
  };
  alarms: Array<{ key: string; state: string; updated_at: string | null }>;
  cloudwatch: { status: string; error_code?: string };
  /** Nightly lift of automatic user blocks. */
  auto_block_sweep: AutoBlockSweep;
}

export interface AutoBlockSweepRun {
  ran_at: string;
  dry_run: boolean;
  evaluated: number;
  lifted: number;
  still_blocked: number;
  admin_blocked: number;
  raced: number;
  lifted_users: string[];
  failures: Array<{ user_id: string; error: string }>;
}

export interface AutoBlockSweep {
  schedule: string;
  status: "ok" | "failed" | "never_ran" | string;
  last_run: AutoBlockSweepRun | null;
}

export const USAGE_METRIC_KEYS = ["cost_usd", "requests", "input_tokens", "output_tokens"] as const;
export type UsageMetricKey = (typeof USAGE_METRIC_KEYS)[number];

export interface UsageModelMetrics {
  model: string;
  series: Record<UsageMetricKey, number[]>;
  totals: Record<UsageMetricKey, number>;
}

export interface UsageTopUser {
  user_id: string;
  name?: string;
  cost_usd: number;
  requests: number;
  granularity: "user" | "workload";
}

export interface UsageMetrics {
  status: "available" | "partial" | "unavailable";
  error_code?: string;
  as_of: string;
  start: string;
  end: string;
  period: "daily";
  days: string[];
  models: UsageModelMetrics[];
  totals: Record<UsageMetricKey, number>;
  top_users: UsageTopUser[];
}

export interface SetLimitsRequest {
  limits: QuotaLimits;
  /** Present = replace both rate limits (null disables). Absent = unchanged. */
  rate?: RateLimits | null;
  reason?: string;
}

export interface EnforcementConfig {
  permission_lease_seconds: number;
  source: string;
  generation: number;
  actor: string;
  reason: string;
  updated_at: string | null;
  valid_permission_lease_seconds?: number[];
  default_permission_lease_seconds?: number;
}

export type EmergencyAction = "activate" | "recover";

// The raw CONFIG#EMERGENCY_STOP state returned by POST /admin/emergency-stop.
// Convergence bookkeeping (applied_* / converged) is computed by the
// operations endpoint and is not part of this acknowledgement.
export interface EmergencyStopState {
  state: string;
  desired_active: boolean;
  generation: number;
  requested_at?: string | null;
  idempotent?: boolean;
  retry?: boolean;
}

export interface SetLimitsResponse {
  user_id: string;
  updated: boolean;
  limits: QuotaLimits;
  user: AdminUser;
}

export interface SetStatusRequest {
  status: UserStatus;
  reason: string;
}

export interface SetStatusResponse {
  user_id: string;
  status: UserStatus;
  reason: string;
  user: AdminUser;
}

export interface VersionConflictDetails {
  current_user?: AdminUser;
}

function hasString(value: JsonObject, key: string): boolean {
  return typeof value[key] === "string";
}

function hasNumber(value: JsonObject, key: string): boolean {
  return typeof value[key] === "number" && Number.isFinite(value[key]);
}

function hasBoolean(value: JsonObject, key: string): boolean {
  return typeof value[key] === "boolean";
}

function hasNullableString(value: JsonObject, key: string): boolean {
  return value[key] === null || typeof value[key] === "string";
}

function hasNullableNumber(value: JsonObject, key: string): boolean {
  return value[key] === null || hasNumber(value, key);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isNonNegativeNumber(value) && Number.isInteger(value);
}

function isUsageTotals(value: unknown): value is UsageTotals {
  return isObject(value) &&
    isNonNegativeNumber(value.cost_usd) &&
    isNonNegativeInteger(value.input_tokens) &&
    isNonNegativeInteger(value.output_tokens) &&
    isNonNegativeInteger(value.requests);
}

function isThreshold(value: unknown): value is QuotaThreshold {
  return isObject(value) &&
    typeof value.at === "number" && Number.isFinite(value.at) && value.at > 0 &&
    (value.action === "warn" || value.action === "block");
}

function isPeriodLimits(value: unknown): value is PeriodLimits {
  return isObject(value) &&
    isNonNegativeNumber(value.usd) &&
    isNonNegativeInteger(value.input_tokens) &&
    isNonNegativeInteger(value.output_tokens) &&
    (value.thresholds === undefined ||
      (Array.isArray(value.thresholds) && value.thresholds.every(isThreshold)));
}

function isRateLimits(value: unknown): value is RateLimits {
  return isObject(value) && isNonNegativeInteger(value.rpm) && isNonNegativeInteger(value.tpm);
}

function isNullableRateLimits(value: unknown): value is RateLimits | null | undefined {
  return value === null || value === undefined || isRateLimits(value);
}

function isQuotaLimits(value: unknown): value is QuotaLimits {
  return isObject(value) &&
    (value.daily === null || isPeriodLimits(value.daily)) &&
    (value.weekly === null || isPeriodLimits(value.weekly)) &&
    (value.monthly === null || isPeriodLimits(value.monthly));
}

function isPeriodUsage(value: unknown): value is PeriodUsage {
  return isObject(value) &&
    (value.period === "daily" || value.period === "weekly" || value.period === "monthly") &&
    hasString(value, "window") &&
    hasString(value, "window_start") &&
    hasString(value, "window_end") &&
    hasString(value, "resets_at") &&
    isUsageTotals(value);
}

function isCurrentUsage(value: unknown): value is CurrentUsage {
  return isObject(value) &&
    isPeriodUsage(value.daily) && value.daily.period === "daily" &&
    isPeriodUsage(value.weekly) && value.weekly.period === "weekly" &&
    isPeriodUsage(value.monthly) && value.monthly.period === "monthly";
}

function isModelBudgets(value: unknown): value is Record<string, QuotaLimits> {
  return value === undefined ||
    (isObject(value) && Object.values(value).every(isQuotaLimits));
}

function isWorkloadIdentity(value: unknown): value is WorkloadIdentity {
  return isObject(value) &&
    hasString(value, "workload_id") &&
    hasString(value, "name") &&
    hasNullableString(value, "model") &&
    hasNullableString(value, "profile_arn") &&
    hasNullableString(value, "role_arn") &&
    hasBoolean(value, "enforcement_ready") &&
    hasBoolean(value, "registered") &&
    isObject(value.tag) && hasString(value.tag, "key") && hasString(value.tag, "value");
}

export function isAdminUser(value: unknown): value is AdminUser {
  return isObject(value) &&
    hasString(value, "user_id") &&
    hasString(value, "name") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "status_reason") &&
    hasString(value, "status_origin") &&
    isNonNegativeInteger(value.version) &&
    hasNullableString(value, "created_at") &&
    hasNullableString(value, "updated_at") &&
    isQuotaLimits(value.limits) &&
    (value.granularity === "user" || value.granularity === "workload") &&
    isNullableRateLimits((value as { rate?: unknown }).rate) &&
    isModelBudgets((value as { model_budgets?: unknown }).model_budgets) &&
    isNullableLeaseState((value as { lease?: unknown }).lease) &&
    (value.workload === undefined || isWorkloadIdentity(value.workload));
}

function isModelBudgetResponse(value: unknown): value is ModelBudgetResponse {
  return isObject(value) && hasString(value, "user_id") && hasString(value, "model_id") &&
    isObject(value.model_budgets) && Object.values(value.model_budgets).every(isQuotaLimits) &&
    isAdminUser(value.user);
}

function isModelUsageResponse(value: unknown): value is ModelUsageResponse {
  return isObject(value) && hasString(value, "user_id") && hasString(value, "model_id") &&
    isCurrentUsage(value.current_usage);
}

function isNullableLeaseState(value: unknown): value is LeaseState | null | undefined {
  if (value === null || value === undefined) return true;
  return isObject(value) &&
    hasBoolean(value, "active") &&
    hasString(value, "expires_at") &&
    hasNullableString(value, "refresh_after") &&
    isNonNegativeInteger(value.generation) &&
    hasString(value, "granted_at") &&
    hasNumber(value, "lease_seconds");
}

function isUserRow(value: unknown): value is UserRow {
  return isAdminUser(value) && isObject(value) && isUsageTotals(value.today) &&
    isCurrentUsage(value.current_usage);
}

function isUserListResponse(value: unknown): value is UserListResponse {
  return isObject(value) &&
    Array.isArray(value.users) && value.users.every(isUserRow) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isWorkloadEntry(value: unknown): value is WorkloadEntry {
  return isWorkloadIdentity(value) && isObject(value) &&
    (value.subject === null || isUserRow(value.subject));
}

export function isWorkloadListResponse(value: unknown): value is WorkloadListResponse {
  return isObject(value) &&
    Array.isArray(value.workloads) && value.workloads.every(isWorkloadEntry) &&
    hasString(value, "roster_source") && hasString(value, "tag_key");
}

function isAdminUserListResponse(value: unknown): value is AdminUserListResponse {
  return isObject(value) &&
    Array.isArray(value.users) && value.users.every(isAdminUser) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isCreateUserResponse(value: unknown): value is CreateUserResponse {
  return isObject(value) && hasString(value, "user_id") &&
    value.provisioned === true && isQuotaLimits(value.limits) &&
    isAdminUser(value.user);
}

function isUserDetailResponse(value: unknown): value is UserDetailResponse {
  return isObject(value) && isAdminUser(value.user) &&
    isCurrentUsage(value.current_usage);
}

function isIsoDate(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function isUsageHistoryRow(value: unknown): value is UsageHistoryRow {
  return isObject(value) && hasString(value, "user_id") &&
    isIsoDate(value.window) && isPeriodUsage(value);
}

function isUsageHistoryResponse(value: unknown): value is UsageHistoryResponse {
  return isObject(value) && hasString(value, "user_id") &&
    (value.period === "daily" || value.period === "weekly" || value.period === "monthly") &&
    isIsoDate(value.start) && isIsoDate(value.end) &&
    Array.isArray(value.usage) && value.usage.every((row) =>
      isUsageHistoryRow(row) && row.period === value.period
    ) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isAuditPeriodLimits(value: unknown): value is AuditPeriodLimits {
  return isObject(value) &&
    isNonNegativeInteger(value.usd_micro) &&
    isNonNegativeInteger(value.input_tokens) &&
    isNonNegativeInteger(value.output_tokens) &&
    (value.thresholds === undefined ||
      (Array.isArray(value.thresholds) && value.thresholds.every((entry) =>
        isObject(entry) && isNonNegativeInteger(entry.at_bps) &&
        (entry.action === "warn" || entry.action === "block"))));
}

function isAuditSnapshotLimits(value: unknown): value is AuditSnapshotLimits {
  return isObject(value) &&
    (value.daily === null || isAuditPeriodLimits(value.daily)) &&
    (value.weekly === null || isAuditPeriodLimits(value.weekly)) &&
    (value.monthly === null || isAuditPeriodLimits(value.monthly));
}

function isAuditUserSnapshot(value: unknown): value is AuditUserSnapshot {
  return isObject(value) && hasString(value, "user_id") && hasString(value, "name") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "status_reason") && hasString(value, "status_origin") &&
    isNonNegativeInteger(value.version) && hasNullableString(value, "created_at") &&
    hasNullableString(value, "updated_at") && isAuditSnapshotLimits(value.limits) &&
    isNullableRateLimits((value as { rate?: unknown }).rate);
}

function isAuditEvent(value: unknown): value is AuditEvent {
  if (!isObject(value) || ![
    "user_id", "event_key", "event_type", "actor", "auth_method",
    "reason", "request_id", "created_at",
  ].every((key) => hasString(value, key)) ||
    !isAuditUserSnapshot(value.after) ||
    !(value.before === null || isAuditUserSnapshot(value.before))) return false;
  return value.after.user_id === value.user_id &&
    (value.before === null || value.before.user_id === value.user_id);
}

function isAuditListResponse(value: unknown): value is AuditListResponse {
  return isObject(value) && Array.isArray(value.events) &&
    value.events.every(isAuditEvent) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isUserAuditListResponse(value: unknown): value is UserAuditListResponse {
  return isAuditListResponse(value) && isObject(value) && hasString(value, "user_id");
}

function isSubjectKindSummary(value: unknown): value is SubjectKindSummary {
  return isObject(value) && hasNumber(value, "total") && hasNumber(value, "blocked") &&
    isUsageTotals(value.today);
}

function isWorkloadKindSummary(value: unknown): value is WorkloadKindSummary {
  return isSubjectKindSummary(value) && isObject(value) &&
    ["configured", "metering_only", "unregistered", "awaiting_traffic"].every((key) => hasNumber(value, key));
}

function isSummary(value: unknown): value is Summary {
  if (!isObject(value) || !isObject(value.enforcement) || !isObject(value.observability)) return false;
  const enforcement = value.enforcement;
  const observability = value.observability;
  const subjects = enforcement.subjects;
  return ["source", "as_of", "window", "mode", "permission_lease_source"].every((key) => hasString(enforcement, key)) &&
    [
      "credential_ttl_seconds",
      "permission_lease_seconds",
      "refresh_overlap_seconds",
      "refresh_jitter_seconds",
      "vend_rate_limit_per_minute",
      "revocation_policy_shards",
      "revocation_reconcile_minutes",
      "total_users",
      "blocked_users",
    ].every((key) => hasNumber(enforcement, key)) &&
    Array.isArray(enforcement.blocked_user_ids) && enforcement.blocked_user_ids.every((item) => typeof item === "string") &&
    isUsageTotals(enforcement.today) &&
    isObject(subjects) && isSubjectKindSummary(subjects.users) && isWorkloadKindSummary(subjects.workloads) &&
    ["source", "delivery", "metrics_namespace", "detection_lag_metric"].every((key) => hasString(observability, key));
}

function isAutoBlockSweepRun(value: unknown): value is AutoBlockSweepRun {
  return isObject(value) && hasString(value, "ran_at") && hasBoolean(value, "dry_run") &&
    ["evaluated", "lifted", "still_blocked", "admin_blocked", "raced"].every((key) => hasNumber(value, key)) &&
    Array.isArray(value.lifted_users) && value.lifted_users.every((item) => typeof item === "string") &&
    Array.isArray(value.failures) && value.failures.every((failure) =>
      isObject(failure) && hasString(failure, "user_id") && hasString(failure, "error"));
}

function isAutoBlockSweep(value: unknown): value is AutoBlockSweep {
  return isObject(value) && hasString(value, "schedule") && hasString(value, "status") &&
    (value.last_run === null || isAutoBlockSweepRun(value.last_run));
}

function isOperations(value: unknown): value is Operations {
  if (!isObject(value) || !hasString(value, "as_of") ||
      !isObject(value.configuration) || !isObject(value.emergency) ||
      !isObject(value.metrics) || !isObject(value.cloudwatch) ||
      !Array.isArray(value.alarms) || !isAutoBlockSweep(value.auto_block_sweep)) return false;
  const config = value.configuration;
  const emergency = value.emergency;
  const metrics = value.metrics;
  return hasString(config, "mode") && hasString(config, "permission_lease_source") &&
    [
      "credential_ttl_seconds", "permission_lease_seconds", "permission_lease_default_seconds",
      "refresh_overlap_seconds", "refresh_jitter_seconds", "vend_rate_limit_per_minute",
      "revocation_policy_shards", "revocation_policy_max_characters", "revocation_reconcile_minutes",
    ].every((key) => hasNumber(config, key)) &&
    hasString(emergency, "state") && hasBoolean(emergency, "desired_active") &&
    hasNumber(emergency, "generation") && hasNumber(emergency, "applied_generation") &&
    hasNullableString(emergency, "requested_at") && hasNullableString(emergency, "applied_at") &&
    hasBoolean(emergency, "converged") &&
    ["namespace", "detection_lag_metric", "telemetry_status", "reconciliation_status"].every((key) => hasString(metrics, key)) &&
    ["detection_lag_p95_ms", "recent_sync_failure_count", "recent_overflow_count", "recent_emergency_failure_count", "revoked_identities_desired"].every((key) => hasNullableNumber(metrics, key)) &&
    ["detection_lag_timestamp", "last_reconciliation_at"].every((key) => hasNullableString(metrics, key)) &&
    hasNumber(metrics, "window_minutes") &&
    value.alarms.every((alarm) => isObject(alarm) && hasString(alarm, "key") && hasString(alarm, "state") && hasNullableString(alarm, "updated_at")) &&
    hasString(value.cloudwatch, "status") &&
    (value.cloudwatch.error_code === undefined || typeof value.cloudwatch.error_code === "string");
}

function isUsageMetricRecord(value: unknown, arrays: boolean): boolean {
  return isObject(value) && USAGE_METRIC_KEYS.every((key) => (
    arrays
      ? Array.isArray(value[key]) && (value[key] as unknown[]).every((item) => typeof item === "number")
      : hasNumber(value, key)
  ));
}

function isUsageMetrics(value: unknown): value is UsageMetrics {
  return isObject(value) &&
    (value.status === "available" || value.status === "partial" || value.status === "unavailable") &&
    (value.error_code === undefined || typeof value.error_code === "string") &&
    hasString(value, "as_of") && hasString(value, "start") && hasString(value, "end") &&
    value.period === "daily" &&
    Array.isArray(value.days) && value.days.every((day) => typeof day === "string") &&
    isUsageMetricRecord(value.totals, false) &&
    Array.isArray(value.models) && value.models.every((entry) => (
      isObject(entry) && hasString(entry, "model") &&
      isUsageMetricRecord(entry.series, true) && isUsageMetricRecord(entry.totals, false)
    )) &&
    Array.isArray(value.top_users) && value.top_users.every((entry) => (
      isObject(entry) && hasString(entry, "user_id") &&
      (entry.name === undefined || typeof entry.name === "string") &&
      hasNumber(entry, "cost_usd") && hasNumber(entry, "requests") &&
      (entry.granularity === "user" || entry.granularity === "workload")
    ));
}

function isSetLimitsResponse(value: unknown): value is SetLimitsResponse {
  return isObject(value) && hasString(value, "user_id") && value.updated === true &&
    isQuotaLimits(value.limits) && isAdminUser(value.user);
}

function isSetStatusResponse(value: unknown): value is SetStatusResponse {
  return isObject(value) && hasString(value, "user_id") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "reason") && isAdminUser(value.user);
}

function isEnforcementConfig(value: unknown): value is EnforcementConfig {
  return isObject(value) &&
    isNonNegativeInteger(value.permission_lease_seconds) &&
    hasString(value, "source") &&
    isNonNegativeInteger(value.generation) &&
    hasString(value, "actor") &&
    hasString(value, "reason") &&
    hasNullableString(value, "updated_at") &&
    (value.valid_permission_lease_seconds === undefined ||
      (Array.isArray(value.valid_permission_lease_seconds) &&
        value.valid_permission_lease_seconds.every(isNonNegativeInteger))) &&
    (value.default_permission_lease_seconds === undefined ||
      isNonNegativeInteger(value.default_permission_lease_seconds));
}

function isEmergencyStopState(value: unknown): value is EmergencyStopState {
  return isObject(value) &&
    hasString(value, "state") &&
    hasBoolean(value, "desired_active") &&
    hasNumber(value, "generation");
}

export function normalizeUsd(value: number): number {
  let micro = Math.round(value * 1_000_000);
  if (value > 0 && micro === 0) micro = 1;
  return micro / 1_000_000;
}

function normalizeLimits(limits: QuotaLimits): QuotaLimits {
  return Object.fromEntries(
    (["daily", "weekly", "monthly"] as const).map((period) => [
      period,
      limits[period] === null
        ? null
        : { ...limits[period], usd: normalizeUsd(limits[period].usd) },
    ]),
  ) as unknown as QuotaLimits;
}

function sameThresholds(left: QuotaThreshold[] | undefined, right: QuotaThreshold[] | undefined): boolean {
  // The server always echoes a list; the client may have omitted one to
  // keep the stored list, in which case any echoed list is acceptable.
  if (left === undefined || right === undefined) return true;
  return left.length === right.length &&
    left.every((entry, index) => Math.abs(entry.at - right[index].at) < 1e-9 && entry.action === right[index].action);
}

function sameLimits(left: QuotaLimits, right: QuotaLimits): boolean {
  return (["daily", "weekly", "monthly"] as const).every((period) => {
    const a = left[period];
    const b = right[period];
    if (a === null || b === null) return a === b;
    return a.usd === b.usd &&
      a.input_tokens === b.input_tokens &&
      a.output_tokens === b.output_tokens &&
      sameThresholds(a.thresholds, b.thresholds);
  });
}

function sameRate(submitted: RateLimits | null | undefined, echoed: RateLimits | null | undefined): boolean {
  if (submitted === undefined) return true;  // not part of this mutation
  const a = submitted ?? { rpm: 0, tpm: 0 };
  const b = echoed ?? { rpm: 0, tpm: 0 };
  return a.rpm === b.rpm && a.tpm === b.tpm;
}

function mutationHeaders(user: AdminUser): Record<string, string> {
  return {
    "Idempotency-Key": globalThis.crypto.randomUUID(),
    "If-Match": `"${user.version}"`,
  };
}

export function apiErrorMessage(caught: unknown): string {
  if (!(caught instanceof ApiError)) {
    return caught instanceof Error ? caught.message : String(caught);
  }

  const suffix = caught.requestId ? ` Request ID: ${caught.requestId}.` : "";
  if (caught.status === 0) return `Unable to reach the broker. Check your connection and try again.${suffix}`;
  if (caught.status === 401) return `Your session expired. Sign in again.${suffix}`;
  if (caught.status === 403) return `You are not authorized to perform this action.${suffix}`;
  if (caught.status === 429) return `Too many admin requests. Wait and try again.${suffix}`;
  if (caught.status === 503) return `The broker is temporarily unavailable. Try again.${suffix}`;
  if (caught.status === 409 && caught.code === "version_conflict") {
    return `This user changed since you opened it. The latest state is shown; review it before retrying.${suffix}`;
  }
  return `${caught.message}${suffix}`;
}

export const api = {
  summary: async (cfg: AdminConfig, session: Session): Promise<Summary> =>
    (await transport<Summary>(cfg, session, "GET", "/admin/summary", { validate: isSummary })).data,

  operations: async (cfg: AdminConfig, session: Session): Promise<Operations> =>
    (await transport<Operations>(cfg, session, "GET", "/admin/operations", { validate: isOperations })).data,

  reconciliation: async (cfg: AdminConfig, session: Session, limit = 14): Promise<ReconciliationResponse> =>
    (await transport<ReconciliationResponse>(cfg, session, "GET", `/admin/reconciliation?limit=${limit}`, {
      validate: isReconciliationResponse,
    })).data,

  usageMetrics: async (cfg: AdminConfig, session: Session, days: number): Promise<UsageMetrics> =>
    (await transport<UsageMetrics>(cfg, session, "GET", `/admin/usage/metrics?days=${days}`, {
      validate: isUsageMetrics,
    })).data,

  getEnforcement: async (cfg: AdminConfig, session: Session): Promise<EnforcementConfig> =>
    (await transport<EnforcementConfig>(cfg, session, "GET", "/admin/enforcement", {
      validate: isEnforcementConfig,
    })).data,

  setEnforcement: async (
    cfg: AdminConfig,
    session: Session,
    permissionLeaseSeconds: number,
    reason?: string,
    expectedGeneration = 0,
  ): Promise<EnforcementConfig> => {
    const trimmedReason = reason?.trim();
    return (await transport<EnforcementConfig>(cfg, session, "PUT", "/admin/enforcement", {
      body: {
        permission_lease_seconds: permissionLeaseSeconds,
        ...(trimmedReason ? { reason: trimmedReason } : {}),
      },
      headers: {
        "Idempotency-Key": globalThis.crypto.randomUUID(),
        "If-Match": `"${expectedGeneration}"`,
      },
      validate: (value): value is EnforcementConfig =>
        isEnforcementConfig(value) &&
        value.permission_lease_seconds === permissionLeaseSeconds,
    })).data;
  },

  // The break-glass key is entered by the operator at action time and is only
  // held in memory for this single request; it is never persisted by the UI.
  setEmergencyStop: async (
    cfg: AdminConfig,
    session: Session,
    request: {
      action: EmergencyAction;
      confirmation: string;
      reason: string;
      emergencyKey: string;
    },
  ): Promise<EmergencyStopState> =>
    (await transport<EmergencyStopState>(cfg, session, "POST", "/admin/emergency-stop", {
      body: {
        action: request.action,
        confirmation: request.confirmation,
        reason: request.reason.trim(),
      },
      headers: { "X-Quota-Emergency-Key": request.emergencyKey },
      validate: isEmergencyStopState,
    })).data,

  listUsersPage: async (
    cfg: AdminConfig,
    session: Session,
    options: ListUsersOptions = {},
  ): Promise<UserListResponse> => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.status) params.set("status", options.status);
    if (options.query?.trim()) params.set("query", options.query.trim());
    if (options.granularity) params.set("granularity", options.granularity);
    return (await transport<UserListResponse>(
      cfg,
      session,
      "GET",
      `/admin/users?${params.toString()}`,
      { validate: isUserListResponse },
    )).data;
  },

  leaseSnapshot: async (cfg: AdminConfig, session: Session): Promise<AdminUserListResponse> =>
    (await transport<AdminUserListResponse>(
      cfg,
      session,
      "GET",
      "/admin/users?limit=50&include_usage=false",
      { validate: isAdminUserListResponse },
    )).data,

  listWorkloads: async (cfg: AdminConfig, session: Session): Promise<WorkloadListResponse> =>
    (await transport<WorkloadListResponse>(
      cfg,
      session,
      "GET",
      "/admin/workloads",
      { validate: isWorkloadListResponse },
    )).data,

  createUser: (
    cfg: AdminConfig,
    session: Session,
    request: CreateUserRequest,
  ): Promise<TransportResponse<CreateUserResponse>> => {
    const body: CreateUserRequest = {
      ...request,
      user_id: request.user_id.trim(),
      name: request.name.trim(),
      limits: normalizeLimits(request.limits),
    };
    return transport(cfg, session, "POST", "/admin/users", {
      body,
      headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
      validate: (value): value is CreateUserResponse =>
        isCreateUserResponse(value) &&
        value.user_id === body.user_id &&
        value.user.user_id === body.user_id &&
        value.user.name === body.name &&
        sameLimits(value.limits, value.user.limits) &&
        sameLimits(value.user.limits, body.limits) &&
        sameRate(body.rate, value.user.rate),
    });
  },

  getUser: (
    cfg: AdminConfig,
    session: Session,
    userId: string,
  ): Promise<TransportResponse<UserDetailResponse>> => {
    const params = new URLSearchParams({ user_id: userId });
    return transport(cfg, session, "GET", `/admin/user?${params.toString()}`, {
      validate: (value): value is UserDetailResponse =>
        isUserDetailResponse(value) && value.user.user_id === userId,
    });
  },

  usageHistory: async (
    cfg: AdminConfig,
    session: Session,
    userId: string,
    options: UsageHistoryOptions = {},
  ): Promise<UsageHistoryResponse> => {
    const params = new URLSearchParams({
      user_id: userId,
      limit: String(options.limit ?? 25),
      period: options.period ?? "daily",
    });
    if (options.start) params.set("start", options.start);
    if (options.end) params.set("end", options.end);
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<UsageHistoryResponse>(
      cfg,
      session,
      "GET",
      `/admin/user/usage-history?${params.toString()}`,
      {
        validate: (value): value is UsageHistoryResponse =>
          isUsageHistoryResponse(value) && value.user_id === userId &&
          value.period === (options.period ?? "daily") &&
          value.usage.every((row) => row.user_id === userId) &&
          (options.start === undefined || value.start === options.start) &&
          (options.end === undefined || value.end === options.end),
      },
    )).data;
  },

  listAuditPage: async (
    cfg: AdminConfig,
    session: Session,
    options: AuditListOptions = {},
  ): Promise<AuditListResponse> => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
    if (options.user_id) params.set("user_id", options.user_id);
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<AuditListResponse>(
      cfg,
      session,
      "GET",
      `/admin/audit?${params.toString()}`,
      {
        validate: (value): value is AuditListResponse =>
          isAuditListResponse(value) &&
          (options.user_id === undefined || value.events.every((event) => event.user_id === options.user_id)),
      },
    )).data;
  },

  listUserAuditPage: async (
    cfg: AdminConfig,
    session: Session,
    userId: string,
    options: Omit<AuditListOptions, "user_id"> = {},
  ): Promise<UserAuditListResponse> => {
    const params = new URLSearchParams({
      user_id: userId,
      limit: String(options.limit ?? 25),
    });
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<UserAuditListResponse>(
      cfg,
      session,
      "GET",
      `/admin/user/audit?${params.toString()}`,
      {
        validate: (value): value is UserAuditListResponse =>
          isUserAuditListResponse(value) && value.user_id === userId &&
          value.events.every((event) => event.user_id === userId),
      },
    )).data;
  },

  setLimits: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    limits: SetLimitsRequest,
  ): Promise<TransportResponse<SetLimitsResponse>> => {
    const { reason, limits: quotaLimits, rate } = limits;
    const trimmedReason = reason?.trim();
    const normalized: SetLimitsRequest = {
      limits: normalizeLimits(quotaLimits),
      ...(rate !== undefined ? { rate } : {}),
      ...(trimmedReason ? { reason: trimmedReason } : {}),
    };
    const params = new URLSearchParams({ user_id: user.user_id });
    return transport(cfg, session, "PUT", `/admin/user/limits?${params.toString()}`, {
      body: normalized,
      headers: mutationHeaders(user),
      validate: (value): value is SetLimitsResponse =>
        isSetLimitsResponse(value) &&
        value.user_id === user.user_id &&
        value.user.user_id === user.user_id &&
        value.user.version > user.version &&
        sameLimits(value.limits, value.user.limits) &&
        sameLimits(value.user.limits, normalized.limits) &&
        sameRate(normalized.rate, value.user.rate),
    });
  },

  setStatus: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    status: UserStatus,
    reason: string,
  ): Promise<TransportResponse<SetStatusResponse>> => {
    const params = new URLSearchParams({ user_id: user.user_id });
    return transport(cfg, session, "PUT", `/admin/user/status?${params.toString()}`, {
      body: { status, reason: reason.trim() } satisfies SetStatusRequest,
      headers: mutationHeaders(user),
      validate: (value): value is SetStatusResponse =>
        isSetStatusResponse(value) &&
        value.user_id === user.user_id &&
        value.user.user_id === user.user_id &&
        value.status === status &&
        value.user.status === status &&
        value.reason === reason.trim() &&
        value.user.status_reason === reason.trim() &&
        value.user.version > user.version,
    });
  },

  // Model-scoped budgets: PUT sets/replaces one model's budget, DELETE
  // removes it. Same If-Match/Idempotency-Key conventions as setLimits.
  setModelBudget: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    modelId: string,
    limits: QuotaLimits,
    reason?: string,
  ): Promise<TransportResponse<ModelBudgetResponse>> => {
    const params = new URLSearchParams({ user_id: user.user_id, model_id: modelId });
    const trimmedReason = reason?.trim();
    const normalized = normalizeLimits(limits);
    return transport(cfg, session, "PUT", `/admin/user/model-budget?${params.toString()}`, {
      body: { limits: normalized, ...(trimmedReason ? { reason: trimmedReason } : {}) },
      headers: mutationHeaders(user),
      validate: (value): value is ModelBudgetResponse =>
        isModelBudgetResponse(value) &&
        value.user_id === user.user_id &&
        value.model_id === modelId &&
        value.user.version > user.version &&
        modelId in value.model_budgets &&
        sameLimits(value.model_budgets[modelId], normalized),
    });
  },

  removeModelBudget: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    modelId: string,
    reason?: string,
  ): Promise<TransportResponse<ModelBudgetResponse>> => {
    const params = new URLSearchParams({ user_id: user.user_id, model_id: modelId });
    const trimmedReason = reason?.trim();
    return transport(cfg, session, "DELETE", `/admin/user/model-budget?${params.toString()}`, {
      body: trimmedReason ? { reason: trimmedReason } : {},
      headers: mutationHeaders(user),
      validate: (value): value is ModelBudgetResponse =>
        isModelBudgetResponse(value) &&
        value.user_id === user.user_id &&
        value.model_id === modelId &&
        value.user.version > user.version &&
        !(modelId in value.model_budgets),
    });
  },

  modelUsage: async (
    cfg: AdminConfig,
    session: Session,
    userId: string,
    modelId: string,
  ): Promise<ModelUsageResponse> => {
    const params = new URLSearchParams({ user_id: userId, model_id: modelId });
    return (await transport<ModelUsageResponse>(cfg, session, "GET", `/admin/user/model-usage?${params.toString()}`, {
      validate: (value): value is ModelUsageResponse =>
        isModelUsageResponse(value) && value.user_id === userId && value.model_id === modelId,
    })).data;
  },
};
