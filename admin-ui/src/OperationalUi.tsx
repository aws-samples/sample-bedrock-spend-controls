import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CalendarDays,
  ChevronRight,
  Pencil,
  RefreshCw,
  ShieldAlert,
  Unlock,
  Lock,
  UserPlus,
  X,
} from "lucide-react";
import type { AdminConfig } from "./config";
import type { Session } from "./auth";
import {
  ApiError,
  AUTOMATIC_BLOCK_HINT,
  api,
  apiErrorMessage,
  isAutomaticBlock,
  normalizeUsd,
  thresholdsError,
  type AdminUser,
  type AuditEvent,
  type CreateUserRequest,
  type CurrentUsage,
  type QuotaLimits,
  type QuotaPeriod,
  type QuotaThreshold,
  type Operations,
  type UsageHistoryResponse,
  type UserAuditListResponse,
  type UserRow,
  type WorkloadIdentity,
} from "./api";
import { formatNumber, formatRatioPercent, formatUsd } from "./format";
import { useModalLifecycle } from "./modal";

const PAGE_SIZE = 25;
// "workload:" is reserved too: workload rows come from the deployed roster.
const RESERVED_PREFIXES = ["SESSION#", "VEND#", "REVOCATION#", "CONFIG#", "EMERGENCY_AUDIT#", "RATE#", "RECONCILE#", "workload:"];
const QUOTA_PERIODS: QuotaPeriod[] = ["daily", "weekly", "monthly"];

type DrawerTab = "overview" | "usage" | "changes";

/** Enforcement posture of a workload, for badges and detail rows. */
export function workloadEnforcementLabel(workload: WorkloadIdentity): { label: string; tone: "green" | "amber" | "gray"; detail: string } {
  if (!workload.registered) {
    return { label: "Unregistered", tone: "gray", detail: "This row is not in the deployed roster (removed from workloads.json or created out of band). It keeps its metering history but nothing enforces it." };
  }
  if (!workload.enforcement_ready) {
    return { label: "Metering only", tone: "amber", detail: "No IAM role configured: usage is metered and alerted but a block is recorded, not enforced. Add role_arn to workloads.json and redeploy." };
  }
  return { label: "Enforced", tone: "green", detail: "A block attaches an inline IAM Deny to the workload role and unblocking removes it; the deny takes effect within seconds and is re-applied automatically every five minutes." };
}

export function WorkloadIdentitySection({ workload }: { workload: WorkloadIdentity }) {
  const enforcement = workloadEnforcementLabel(workload);
  return <section aria-label="Workload identity"><h3>Workload identity</h3><dl className="detail-list detail-list-wide">
    <div><dt>Model</dt><dd>{workload.model ? <code>{workload.model}</code> : "Unknown (not in roster)"}</dd></div>
    <div><dt>Inference profile</dt><dd>{workload.profile_arn ? <code className="break-all">{workload.profile_arn}</code> : "Unknown (not in roster)"}</dd></div>
    <div><dt>IAM role</dt><dd>{workload.role_arn ? <code className="break-all">{workload.role_arn}</code> : "None configured"}</dd></div>
    <div><dt>Enforcement</dt><dd><span className={`ops-status ops-status-plain ops-status-${enforcement.tone}`}><span aria-hidden="true" />{enforcement.label}</span><p className="field-help">{enforcement.detail}</p></dd></div>
    <div><dt>Cost-allocation tag</dt><dd><code>{workload.tag.key}={workload.tag.value}</code></dd></div>
  </dl></section>;
}

function ErrorMessage({ message }: { message: string }) {
  return <div className="message message-error" role="alert"><AlertCircle aria-hidden="true" size={18} /><span>{message}</span></div>;
}

function BusyLabel({ children }: { children: string }) {
  return <span className="inline-busy"><RefreshCw className="spin" aria-hidden="true" size={16} />{children}</span>;
}


function formatTimestamp(value: string | null): string {
  if (!value) return "Not available";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Invalid timestamp" : timestamp.toLocaleString();
}

function formatLimit(value: number, formatter: (item: number) => string): string {
  return value === 0 ? "Unlimited" : formatter(value);
}

type WizardPeriodDraft = { enabled: boolean; usd: string; input: string; output: string };
type WizardLimitsDraft = Record<QuotaPeriod, WizardPeriodDraft>;

function initialWizardLimits(): WizardLimitsDraft {
  return {
    daily: { enabled: true, usd: "1", input: "1000000", output: "200000" },
    weekly: { enabled: false, usd: "0", input: "0", output: "0" },
    monthly: { enabled: false, usd: "0", input: "0", output: "0" },
  };
}

function parseLimits(draft: WizardLimitsDraft): QuotaLimits | null {
  const result: Partial<QuotaLimits> = {};
  for (const period of QUOTA_PERIODS) {
    const value = draft[period];
    if (!value.enabled) {
      result[period] = null;
      continue;
    }
    const usd = value.usd.trim() === "" ? Number.NaN : Number(value.usd);
    const input = value.input.trim() === "" ? Number.NaN : Number(value.input);
    const output = value.output.trim() === "" ? Number.NaN : Number(value.output);
    if (!Number.isFinite(usd) || usd < 0 || !Number.isInteger(input) || input < 0 || !Number.isInteger(output) || output < 0) return null;
    result[period] = { usd: normalizeUsd(usd), input_tokens: input, output_tokens: output };
  }
  return Object.values(result).some((value) => value !== null)
    ? result as QuotaLimits
    : null;
}

function periodLabel(period: QuotaPeriod): string {
  return period[0].toUpperCase() + period.slice(1);
}

export function CreateUserWizard({
  cfg,
  session,
  onClose,
  onCreated,
}: {
  cfg: AdminConfig;
  session: Session;
  onClose: () => void;
  onCreated: (user: AdminUser, openDetails: boolean) => void;
}) {
  const [step, setStep] = useState(0);
  const [userId, setUserId] = useState("");
  const [name, setName] = useState("");
  const [limitsDraft, setLimitsDraft] = useState<WizardLimitsDraft>(initialWizardLimits);
  const [unlimitedConfirmed, setUnlimitedConfirmed] = useState(false);
  const [openDetails, setOpenDetails] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  const [requestError, setRequestError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const userIdRef = useRef<HTMLInputElement>(null);
  const stepHeadingRef = useRef<HTMLHeadingElement>(null);
  useModalLifecycle(busy, onClose, dialogRef, userIdRef);

  const limits = parseLimits(limitsDraft);
  const unlimited = limits !== null && QUOTA_PERIODS.some((period) => {
    const value = limits[period];
    return value !== null && Object.values(value).some((item) => item === 0);
  });
  const steps = ["Identity", "Limits", "Review"];

  useEffect(() => {
    if (step > 0) stepHeadingRef.current?.focus();
  }, [step]);

  function identityErrors(): string[] {
    const next: string[] = [];
    const identity = userId.trim();
    if (!identity) next.push("Enter the immutable user identity claim value.");
    if (RESERVED_PREFIXES.some((prefix) => identity.startsWith(prefix))) {
      next.push("The user identity starts with a reserved prefix (for example `workload:`).");
    }
    if (!name.trim()) next.push("Enter a display name.");
    return next;
  }

  function limitErrors(): string[] {
    if (!limits) return [Object.values(limitsDraft).some((value) => value.enabled)
      ? "Enter a non-negative USD amount and whole token values. Fields cannot be blank."
      : "Enable at least one calendar quota period."];
    if (unlimited && !unlimitedConfirmed) return ["Confirm that every zero limit should be Unlimited."];
    return [];
  }

  function next() {
    const nextErrors = step === 0 ? identityErrors() : limitErrors();
    setErrors(nextErrors);
    setRequestError("");
    if (nextErrors.length === 0) setStep((current) => Math.min(current + 1, 2));
  }

  async function create() {
    const nextErrors = [...identityErrors(), ...limitErrors()];
    setErrors(nextErrors);
    setRequestError("");
    if (nextErrors.length > 0 || !limits) return;
    const request: CreateUserRequest = {
      user_id: userId.trim(),
      name: name.trim(),
      limits,
    };
    setBusy(true);
    try {
      const result = await api.createUser(cfg, session, request);
      onCreated(result.data.user, openDetails);
      onClose();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "user_already_exists") {
        setRequestError(`A user with identity "${request.user_id}" already exists. Close this wizard and search for the existing user instead.`);
      } else {
        setRequestError(apiErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-labelledby="create-user-title"
        aria-modal="true"
        className="dialog create-wizard"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div><p className="eyebrow">Provision access</p><h2 id="create-user-title">Create user</h2></div>
          <button aria-label="Close create user wizard" className="icon-button" disabled={busy} onClick={onClose} type="button"><X aria-hidden="true" size={19} /></button>
        </div>
        <ol aria-label="Create user progress" className="wizard-steps">
          {steps.map((label, index) => <li aria-current={step === index ? "step" : undefined} className={step >= index ? "wizard-step-active" : ""} key={label}><span>{index + 1}</span>{label}</li>)}
        </ol>
        <div className="wizard-body">
          <h3 ref={stepHeadingRef} tabIndex={-1}>{steps[step]}</h3>
          {errors.length > 0 && (
            <div aria-label="Create user errors" className="error-summary" role="alert">
              <strong>Fix the following before continuing:</strong>
              <ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
            </div>
          )}
          {requestError && <ErrorMessage message={requestError} />}
          {step === 0 && (
            <div className="wizard-fields">
              <label><span>User identity claim value</span><input autoComplete="off" disabled={busy} onChange={(event) => { setUserId(event.target.value); setErrors([]); }} ref={userIdRef} value={userId} /></label>
              <p className="field-help">Use the exact value of the configured identity claim. This becomes the immutable <code>user_id</code>.</p>
              <label><span>Display name</span><input autoComplete="off" disabled={busy} onChange={(event) => { setName(event.target.value); setErrors([]); }} value={name} /></label>
            </div>
          )}
          {step === 1 && (
            <div className="wizard-fields">
              <p className="field-help">Calendar windows use UTC. Daily is enabled by default; weekly and monthly are optional. Enter 0 only when that dimension must be Unlimited.</p>
              <div className="quota-limit-matrix">
                {QUOTA_PERIODS.map((period) => (
                  <fieldset className="quota-period-card" key={period}>
                    <legend><label className="quota-period-toggle"><input checked={limitsDraft[period].enabled} disabled={busy} onChange={(event) => { setLimitsDraft((current) => ({ ...current, [period]: { ...current[period], enabled: event.target.checked } })); setUnlimitedConfirmed(false); setErrors([]); }} type="checkbox" /><span>{periodLabel(period)}</span></label></legend>
                    <div className="field-grid">
                      <label><span>{periodLabel(period)} USD limit</span><input aria-label={`Create ${periodLabel(period)} USD limit`} disabled={busy || !limitsDraft[period].enabled} min="0" onChange={(event) => { setLimitsDraft((current) => ({ ...current, [period]: { ...current[period], usd: event.target.value } })); setUnlimitedConfirmed(false); setErrors([]); }} step="0.000001" type="number" value={limitsDraft[period].usd} /></label>
                      <label><span>{periodLabel(period)} input token limit</span><input aria-label={`Create ${periodLabel(period)} input token limit`} disabled={busy || !limitsDraft[period].enabled} min="0" onChange={(event) => { setLimitsDraft((current) => ({ ...current, [period]: { ...current[period], input: event.target.value } })); setUnlimitedConfirmed(false); setErrors([]); }} step="1" type="number" value={limitsDraft[period].input} /></label>
                      <label><span>{periodLabel(period)} output token limit</span><input aria-label={`Create ${periodLabel(period)} output token limit`} disabled={busy || !limitsDraft[period].enabled} min="0" onChange={(event) => { setLimitsDraft((current) => ({ ...current, [period]: { ...current[period], output: event.target.value } })); setUnlimitedConfirmed(false); setErrors([]); }} step="1" type="number" value={limitsDraft[period].output} /></label>
                    </div>
                  </fieldset>
                ))}
              </div>
              {unlimited && (
                <label className="unlimited-confirm"><input checked={unlimitedConfirmed} disabled={busy} onChange={(event) => setUnlimitedConfirmed(event.target.checked)} type="checkbox" /><span>I confirm that each 0 value above means Unlimited.</span></label>
              )}
            </div>
          )}
          {step === 2 && limits && (
            <div className="wizard-review">
              <dl>
                <div><dt>User identity</dt><dd>{userId.trim()}</dd></div>
                <div><dt>Display name</dt><dd>{name.trim()}</dd></div>
                {QUOTA_PERIODS.map((period) => {
                  const value = limits[period];
                  return <div key={period}><dt>{periodLabel(period)}</dt><dd>{value ? `${formatLimit(value.usd, (item) => formatUsd(item))} · ${formatLimit(value.input_tokens, (item) => formatNumber(item))} input · ${formatLimit(value.output_tokens, (item) => formatNumber(item))} output` : "Disabled"}</dd></div>;
                })}
              </dl>
              <div className="safety-warning"><ShieldAlert aria-hidden="true" size={18} /><span>Creating this user grants quota-managed access for the immutable identity shown above. No request is sent until you select Create user.</span></div>
              <label className="review-option"><input checked={openDetails} disabled={busy} onChange={(event) => setOpenDetails(event.target.checked)} type="checkbox" />Open details if the new user is added to this page</label>
            </div>
          )}
        </div>
        <div className="dialog-actions wizard-actions">
          <button className="button button-secondary" disabled={busy} onClick={step === 0 ? onClose : () => { setStep((current) => current - 1); setErrors([]); setRequestError(""); }} type="button">{step === 0 ? "Cancel" : "Back"}</button>
          {step < 2 ? (
            <button className="button button-primary" disabled={busy} onClick={next} type="button">Next<ChevronRight aria-hidden="true" size={16} /></button>
          ) : (
            <button className="button button-primary" disabled={busy} onClick={() => void create()} type="button">{busy ? <BusyLabel>Creating</BusyLabel> : <><UserPlus aria-hidden="true" size={16} />Create user</>}</button>
          )}
        </div>
      </div>
    </div>
  );
}

function utcDate(offsetDays: number): string {
  const date = new Date();
  date.setUTCHours(0, 0, 0, 0);
  date.setUTCDate(date.getUTCDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function periodStart(period: QuotaPeriod, value: string): string {
  const date = new Date(`${value}T00:00:00Z`);
  if (period === "weekly") {
    date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7));
  } else if (period === "monthly") {
    date.setUTCDate(1);
  }
  return date.toISOString().slice(0, 10);
}

function Pagination({
  busy,
  hasNext,
  hasPrevious,
  label,
  onNext,
  onPrevious,
}: {
  busy: boolean;
  hasNext: boolean;
  hasPrevious: boolean;
  label: string;
  onNext: () => void;
  onPrevious: () => void;
}) {
  return <div className="pagination"><span>{label}</span><div><button className="button button-secondary" disabled={busy || !hasPrevious} onClick={onPrevious} type="button"><ArrowLeft aria-hidden="true" size={15} />Previous</button><button className="button button-secondary" disabled={busy || !hasNext} onClick={onNext} type="button">Next<ArrowRight aria-hidden="true" size={15} /></button></div></div>;
}

function UsageTab({ active, cfg, session, userId }: { active: boolean; cfg: AdminConfig; session: Session; userId: string }) {
  const [period, setPeriod] = useState<QuotaPeriod>("daily");
  const [start, setStart] = useState(() => utcDate(-29));
  const [end, setEnd] = useState(() => utcDate(0));
  const [applied, setApplied] = useState(() => ({ start: utcDate(-29), end: utcDate(0) }));
  const [page, setPage] = useState<UsageHistoryResponse | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [rangeError, setRangeError] = useState("");
  const loadedPeriod = useRef<QuotaPeriod | null>(null);
  const request = useRef(0);

  async function load(cursor: string | null, range: { start: string; end: string } | undefined, targetIndex: number, reset = false) {
    const currentRequest = ++request.current;
    setLoading(true);
    setError("");
    try {
      const next = await api.usageHistory(cfg, session, userId, { ...range, period, limit: PAGE_SIZE, cursor });
      if (request.current !== currentRequest) return;
      const resolvedRange = { start: next.start, end: next.end };
      setPage(next);
      setApplied(resolvedRange);
      if (range === undefined) {
        setStart(next.start);
        setEnd(next.end);
      }
      if (reset) {
        setCursors([null]);
        setPageIndex(0);
      } else {
        setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
        setPageIndex(targetIndex);
      }
      loadedPeriod.current = period;
    } catch (caught) {
      if (request.current === currentRequest) setError(apiErrorMessage(caught));
    } finally {
      if (request.current === currentRequest) setLoading(false);
    }
  }

  useEffect(() => {
    if (active && loadedPeriod.current !== period) void load(null, undefined, 0, true);
  }, [active, period]);

  function applyRange(event: FormEvent) {
    event.preventDefault();
    if (!start || !end || start > end) {
      setRangeError("Choose an inclusive start date that is not after the end date.");
      return;
    }
    if (period !== "daily" && (periodStart(period, start) !== start || periodStart(period, end) !== end)) {
      setRangeError(`Choose ${period} period-start dates (${period === "weekly" ? "Mondays" : "the first day of each month"}).`);
      return;
    }
    setRangeError("");
    void load(null, { start, end }, 0, true);
  }

  return (
    <div aria-busy={loading} className="drawer-tab-content">
      <form className="range-form" onSubmit={applyRange}>
        <label><span>Quota period</span><select aria-label="Usage history period" onChange={(event) => { const next = event.target.value as QuotaPeriod; request.current += 1; loadedPeriod.current = null; setPeriod(next); setStart(periodStart(next, utcDate(-29))); setEnd(periodStart(next, utcDate(0))); setPage(null); setCursors([null]); setPageIndex(0); }} value={period}><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option></select></label>
        <label><span>Start date</span><input max={utcDate(0)} onChange={(event) => setStart(event.target.value)} type="date" value={start} /></label>
        <label><span>End date</span><input max={utcDate(0)} onChange={(event) => setEnd(event.target.value)} type="date" value={end} /></label>
        <button className="button button-secondary" disabled={loading} type="submit"><CalendarDays aria-hidden="true" size={16} />Apply range</button>
      </form>
      <p className="field-help">{period === "daily" ? "Dates are inclusive." : `${periodLabel(period)} history selects an inclusive range of period start dates.`} The broker enforces the configured usage retention window.</p>
      {rangeError && <ErrorMessage message={rangeError} />}
      {error && <ErrorMessage message={error} />}
      {error && page && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing the previous usage page</span>}
      {loading && !page ? <div className="drawer-loading"><BusyLabel>Loading usage</BusyLabel></div> : page && (
        <>
          <div aria-label={`${periodLabel(period)} usage history`} className="drawer-table-scroll" role="region" tabIndex={0}>
            <table className="compact-table"><thead><tr><th>Window start</th><th>Resets</th><th>USD</th><th>Input tokens</th><th>Output tokens</th><th>Requests</th></tr></thead><tbody>
              {page.usage.map((row) => <tr key={row.window}><td>{row.window}</td><td>{formatTimestamp(row.resets_at)}</td><td>{formatUsd(row.cost_usd)}</td><td>{formatNumber(row.input_tokens)}</td><td>{formatNumber(row.output_tokens)}</td><td>{formatNumber(row.requests)}</td></tr>)}
            </tbody></table>
            {page.usage.length === 0 && <div className="compact-empty">No usage was recorded in this date range.</div>}
          </div>
          <Pagination busy={loading} hasNext={Boolean(page.next_cursor)} hasPrevious={pageIndex > 0} label={`${page.usage.length} ${period} records on this page · ${page.start} to ${page.end}`} onNext={() => page.next_cursor && void load(page.next_cursor, applied, pageIndex + 1)} onPrevious={() => void load(cursors[pageIndex - 1], applied, pageIndex - 1)} />
        </>
      )}
    </div>
  );
}

function formatThresholds(thresholds: Array<{ at_bps: number; action: string }> | undefined): string {
  if (!thresholds || thresholds.length === 0) return "default";
  const list = thresholds.map((entry) => `${entry.at_bps / 100}% ${entry.action}`).join(", ");
  return thresholds.some((entry) => entry.action === "block") ? list : `${list} (alert-only)`;
}

function sameAuditThresholds(a: Array<{ at_bps: number; action: string }> | undefined, b: Array<{ at_bps: number; action: string }> | undefined): boolean {
  if (!a || !b) return a === b;
  return a.length === b.length && a.every((entry, index) => entry.at_bps === b[index].at_bps && entry.action === b[index].action);
}

export function summarizeAuditEvent(event: AuditEvent): string {
  if (!event.before) {
    const periods = QUOTA_PERIODS.map((period) => {
      const limits = event.after.limits[period];
      return limits ? `${periodLabel(period)}: ${formatLimit(limits.usd_micro / 1_000_000, (value) => formatUsd(value))}, ${formatLimit(limits.input_tokens, (value) => formatNumber(value))} input, ${formatLimit(limits.output_tokens, (value) => formatNumber(value))} output` : `${periodLabel(period)}: disabled`;
    });
    const rate = event.after.rate;
    if (rate && (rate.rpm > 0 || rate.tpm > 0)) periods.push(`Rate: ${rate.rpm || "∞"} rpm, ${rate.tpm || "∞"} tpm`);
    return `Created ${event.after.status}; ${periods.join("; ")}.`;
  }
  const changes: string[] = [];
  if (event.before.name !== event.after.name) changes.push(`name: ${event.before.name} → ${event.after.name}`);
  if (event.before.status !== event.after.status) changes.push(`status: ${event.before.status} → ${event.after.status}`);
  for (const period of QUOTA_PERIODS) {
    const before = event.before.limits[period];
    const after = event.after.limits[period];
    if (before === null || after === null) {
      if (before !== after) {
        const afterDetail = after
          ? `enabled (${formatLimit(after.usd_micro / 1_000_000, (value) => formatUsd(value))}, ${formatLimit(after.input_tokens, (value) => formatNumber(value))} input, ${formatLimit(after.output_tokens, (value) => formatNumber(value))} output)`
          : "disabled";
        changes.push(`${periodLabel(period)}: ${before ? "enabled" : "disabled"} → ${afterDetail}`);
      }
      continue;
    }
    if (before.usd_micro !== after.usd_micro) changes.push(`${periodLabel(period)} USD: ${formatLimit(before.usd_micro / 1_000_000, (value) => formatUsd(value))} → ${formatLimit(after.usd_micro / 1_000_000, (value) => formatUsd(value))}`);
    if (before.input_tokens !== after.input_tokens) changes.push(`${periodLabel(period)} input: ${formatLimit(before.input_tokens, (value) => formatNumber(value))} → ${formatLimit(after.input_tokens, (value) => formatNumber(value))}`);
    if (before.output_tokens !== after.output_tokens) changes.push(`${periodLabel(period)} output: ${formatLimit(before.output_tokens, (value) => formatNumber(value))} → ${formatLimit(after.output_tokens, (value) => formatNumber(value))}`);
    if (!sameAuditThresholds(before.thresholds, after.thresholds)) changes.push(`${periodLabel(period)} thresholds: ${formatThresholds(before.thresholds)} → ${formatThresholds(after.thresholds)}`);
  }
  const rateBefore = event.before.rate ?? { rpm: 0, tpm: 0 };
  const rateAfter = event.after.rate ?? { rpm: 0, tpm: 0 };
  if (rateBefore.rpm !== rateAfter.rpm) changes.push(`rpm: ${formatLimit(rateBefore.rpm, String)} → ${formatLimit(rateAfter.rpm, String)}`);
  if (rateBefore.tpm !== rateAfter.tpm) changes.push(`tpm: ${formatLimit(rateBefore.tpm, String)} → ${formatLimit(rateAfter.tpm, String)}`);
  return changes.length > 0 ? changes.join("; ") : `Configuration version ${event.before.version} → ${event.after.version}.`;
}

function AuditTable({ events, label, onTarget }: { events: AuditEvent[]; label: string; onTarget?: (userId: string) => void }) {
  return <div aria-label={label} className="drawer-table-scroll" role="region" tabIndex={0}>
    <table className="compact-table audit-table"><thead><tr><th>Timestamp</th><th>Actor / auth</th><th>Event</th><th>User</th><th>Reason</th><th>Request ID</th><th>Change summary</th></tr></thead><tbody>
      {events.map((event) => <tr key={event.event_key}><td>{formatTimestamp(event.created_at)}</td><td>{event.actor}<small>{event.auth_method}</small></td><td>{event.event_type}</td><td>{onTarget ? <button className="link-button" onClick={() => onTarget(event.user_id)} type="button">{event.user_id}</button> : event.user_id}</td><td>{event.reason || "Not provided"}</td><td><code>{event.request_id || "Not provided"}</code></td><td>{summarizeAuditEvent(event)}</td></tr>)}
    </tbody></table>
    {events.length === 0 && <div className="compact-empty">No administrative changes were found.</div>}
  </div>;
}

function ChangesTab({ active, cfg, session, userId }: { active: boolean; cfg: AdminConfig; session: Session; userId: string }) {
  const [page, setPage] = useState<UserAuditListResponse | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const loaded = useRef(false);

  async function load(cursor: string | null, targetIndex: number) {
    setLoading(true);
    setError("");
    try {
      const next = await api.listUserAuditPage(cfg, session, userId, { limit: PAGE_SIZE, cursor });
      setPage(next);
      setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
      setPageIndex(targetIndex);
      loaded.current = true;
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (active && !loaded.current) void load(null, 0);
  }, [active]);

  return <div aria-busy={loading} className="drawer-tab-content">
    {error && <ErrorMessage message={error} />}
    {error && page && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing the previous changes page</span>}
    {loading && !page ? <div className="drawer-loading"><BusyLabel>Loading changes</BusyLabel></div> : page && <><AuditTable events={page.events} label={`Administrative changes for ${userId}`} /><Pagination busy={loading} hasNext={Boolean(page.next_cursor)} hasPrevious={pageIndex > 0} label={`${page.events.length} changes on this page`} onNext={() => page.next_cursor && void load(page.next_cursor, pageIndex + 1)} onPrevious={() => void load(cursors[pageIndex - 1], pageIndex - 1)} /></>}
  </div>;
}

export function UserDetailDrawer({
  cfg,
  session,
  user,
  onCanonical,
  onClose,
  onEdit,
  onStatus,
  statusActionAvailable = true,
  suspended = false,
}: {
  cfg: AdminConfig;
  session: Session;
  user: UserRow;
  onCanonical: (user: AdminUser) => void;
  onClose: () => void;
  onEdit: () => void;
  onStatus: () => void;
  statusActionAvailable?: boolean;
  suspended?: boolean;
}) {
  const [tab, setTab] = useState<DrawerTab>("overview");
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(true);
  // Freshly fetched usage for this drawer; until it lands, render the
  // list row's snapshot so prop updates keep flowing through.
  const [fetchedUsage, setFetchedUsage] = useState<CurrentUsage | null>(null);
  const detailUsage = fetchedUsage ?? user.current_usage;
  const drawerRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useModalLifecycle(false, onClose, drawerRef, closeRef, !suspended);
  const tabs: Array<{ id: DrawerTab; label: string }> = [
    { id: "overview", label: "Overview" },
    { id: "usage", label: "Usage" },
    { id: "changes", label: "Changes" },
  ];

  useEffect(() => {
    let active = true;
    setDetailLoading(true);
    setDetailError("");
    void api.getUser(cfg, session, user.user_id).then((result) => {
      if (active) {
        onCanonical(result.data.user);
        setFetchedUsage(result.data.current_usage);
      }
    }).catch((caught) => {
      if (active) setDetailError(apiErrorMessage(caught));
    }).finally(() => {
      if (active) setDetailLoading(false);
    });
    return () => { active = false; };
  }, [user.user_id]);

  function selectTab(nextIndex: number) {
    const normalized = (nextIndex + tabs.length) % tabs.length;
    setTab(tabs[normalized].id);
    tabRefs.current[normalized]?.focus();
  }

  function tabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "ArrowRight") { event.preventDefault(); selectTab(index + 1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); selectTab(index - 1); }
    else if (event.key === "Home") { event.preventDefault(); selectTab(0); }
    else if (event.key === "End") { event.preventDefault(); selectTab(tabs.length - 1); }
  }

  const workload = user.workload;
  const subjectLabel = workload ? "workload" : "user";
  const blockLabel = user.status === "active" ? `Block ${subjectLabel}` : `Unblock ${subjectLabel}`;

  return <div className="drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <aside aria-hidden={suspended || undefined} aria-labelledby="user-detail-title" aria-modal={suspended ? undefined : true} className="detail-drawer" ref={drawerRef} role="dialog" tabIndex={-1}>
      <div className="drawer-header"><div><p className="eyebrow">{workload ? "Workload details" : "User details"}</p><h2 id="user-detail-title">{user.name || "Unnamed identity"}</h2><code>{user.user_id}</code></div><button aria-label={`Close ${subjectLabel} details`} className="icon-button" onClick={onClose} ref={closeRef} type="button"><X aria-hidden="true" size={20} /></button></div>
      {detailError && <div className="drawer-message"><ErrorMessage message={detailError} /><span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing configuration from the current page</span></div>}
      {detailLoading && <span className="detail-refresh"><BusyLabel>Refreshing detail</BusyLabel></span>}
      <div aria-label={`${workload ? "Workload" : "User"} detail sections`} className="drawer-tabs" role="tablist">
        {tabs.map((item, index) => <button aria-controls={`user-${item.id}-panel`} aria-selected={tab === item.id} id={`user-${item.id}-tab`} key={item.id} onClick={() => setTab(item.id)} onKeyDown={(event) => tabKeyDown(event, index)} ref={(element) => { tabRefs.current[index] = element; }} role="tab" tabIndex={tab === item.id ? 0 : -1} type="button">{item.label}</button>)}
      </div>
      <section aria-labelledby="user-overview-tab" hidden={tab !== "overview"} id="user-overview-panel" role="tabpanel" tabIndex={0}>
        <div className="drawer-overview">
          <div className="drawer-actions">
            <button className="button button-secondary" onClick={onEdit} type="button"><Pencil aria-hidden="true" size={16} />Edit limits</button>
            <button
              aria-label={statusActionAvailable ? blockLabel : "Status change unavailable until a fresh enforcement summary loads"}
              className={`button ${user.status === "active" ? "button-danger" : "button-primary"}`}
              disabled={!statusActionAvailable}
              onClick={onStatus}
              type="button"
            >
              {user.status === "active" ? <Lock aria-hidden="true" size={16} /> : <Unlock aria-hidden="true" size={16} />}
              {blockLabel}
            </button>
          </div>
          {workload && <WorkloadIdentitySection workload={workload} />}
          <section><h3>Identity and status</h3><dl className="detail-list"><div><dt>Status</dt><dd><span className={`status-badge status-${user.status}`}><span aria-hidden="true" />{user.status}</span></dd></div><div><dt>Status origin</dt><dd>{user.status_origin || "Not provided"}</dd></div><div><dt>Status reason</dt><dd>{user.status_reason || "Not provided"}</dd></div>{isAutomaticBlock(user) && <div><dt>Lifts</dt><dd>{AUTOMATIC_BLOCK_HINT}</dd></div>}<div><dt>Created</dt><dd>{formatTimestamp(user.created_at)}</dd></div><div><dt>Updated</dt><dd>{formatTimestamp(user.updated_at)}</dd></div><div><dt>Version</dt><dd>{user.version}</dd></div></dl></section>
          <section><h3>Calendar quota windows</h3><div className="detail-periods">{QUOTA_PERIODS.map((period) => { const limits = user.limits[period]; const usage = detailUsage[period]; const thresholds = limits?.thresholds ?? []; const alertOnly = thresholds.length > 0 && thresholds.every((entry) => entry.action !== "block"); return <article className="detail-period" key={period}><div><h4>{periodLabel(period)}</h4><span>Resets {formatTimestamp(usage.resets_at)}</span></div>{limits ? <dl className="detail-list"><div><dt>USD</dt><dd>{formatUsd(usage.cost_usd)} of {formatLimit(limits.usd, (value) => formatUsd(value))}</dd></div><div><dt>Input tokens</dt><dd>{formatNumber(usage.input_tokens)} of {formatLimit(limits.input_tokens, (value) => formatNumber(value))}</dd></div><div><dt>Output tokens</dt><dd>{formatNumber(usage.output_tokens)} of {formatLimit(limits.output_tokens, (value) => formatNumber(value))}</dd></div><div><dt>Requests</dt><dd>{formatNumber(usage.requests)}</dd></div><div><dt>Thresholds</dt><dd>{thresholds.length > 0 ? thresholds.map((entry) => `${formatRatioPercent(entry.at)} ${entry.action}`).join(", ") : "default"}{alertOnly && <span className="ops-status ops-status-amber"> alert-only</span>}</dd></div></dl> : <p className="operations-muted">Disabled</p>}</article>; })}</div></section>
          <section><h3>Rate limits</h3><dl className="detail-list"><div><dt>Requests per minute</dt><dd>{user.rate?.rpm ? formatNumber(user.rate.rpm) : "Unlimited"}</dd></div><div><dt>Tokens per minute</dt><dd>{user.rate?.tpm ? formatNumber(user.rate.tpm) : "Unlimited"}</dd></div></dl></section>
          <ModelBudgetsSection cfg={cfg} onCanonical={onCanonical} session={session} user={user} />
        </div>
      </section>
      <section aria-labelledby="user-usage-tab" hidden={tab !== "usage"} id="user-usage-panel" role="tabpanel" tabIndex={0}><UsageTab active={tab === "usage"} cfg={cfg} session={session} userId={user.user_id} /></section>
      <section aria-labelledby="user-changes-tab" hidden={tab !== "changes"} id="user-changes-panel" role="tabpanel" tabIndex={0}><ChangesTab active={tab === "changes"} cfg={cfg} session={session} userId={user.user_id} /></section>
    </aside>
  </div>;
}

type AuditLoadRequest = Readonly<{
  cursor: string | null;
  targetIndex: number;
  reset: boolean;
}>;

// ---------------------------------------------------------------------------
// Per-model budgets (optional second axis)
// ---------------------------------------------------------------------------

/** "50:warn,80:warn,100:block" -> thresholds; null on a parse error. Same
 *  grammar as the sigv4 CLI so operators can copy between the two. */
export function parseThresholdSpec(spec: string): QuotaThreshold[] | null | undefined {
  const trimmed = spec.trim();
  if (trimmed === "") return undefined;  // omitted: server applies the default
  const entries: QuotaThreshold[] = [];
  for (const token of trimmed.split(",")) {
    const [percent, action] = token.trim().split(":");
    const at = Number(percent) / 100;
    if (!Number.isFinite(at) || (action !== "warn" && action !== "block")) return null;
    entries.push({ at: Math.round(at * 10_000) / 10_000, action });
  }
  return thresholdsError(entries) === null ? entries : null;
}

export function formatThresholdSpec(thresholds: QuotaThreshold[] | undefined): string {
  return (thresholds ?? []).map((entry) => `${Math.round(entry.at * 10_000) / 100}:${entry.action}`).join(",");
}

type ModelBudgetPeriodDraft = { enabled: boolean; usd: string; input: string; output: string; thresholds: string };
type ModelBudgetDraft = { modelId: string; periods: Record<QuotaPeriod, ModelBudgetPeriodDraft>; reason: string };

function emptyModelBudgetDraft(modelId = "", existing?: QuotaLimits): ModelBudgetDraft {
  const period = (value: QuotaLimits[QuotaPeriod] | undefined, enabled: boolean): ModelBudgetPeriodDraft => ({
    enabled: value ? true : enabled,
    usd: String(value?.usd ?? 0),
    input: String(value?.input_tokens ?? 0),
    output: String(value?.output_tokens ?? 0),
    thresholds: formatThresholdSpec(value?.thresholds),
  });
  return {
    modelId,
    periods: {
      daily: period(existing?.daily, !existing),
      weekly: period(existing?.weekly, false),
      monthly: period(existing?.monthly, false),
    },
    reason: "",
  };
}

export function parseModelBudgetDraft(draft: ModelBudgetDraft): { limits: QuotaLimits } | { error: string } {
  const result: Partial<QuotaLimits> = {};
  let enabledCount = 0;
  for (const period of QUOTA_PERIODS) {
    const value = draft.periods[period];
    if (!value.enabled) { result[period] = null; continue; }
    enabledCount += 1;
    const usd = value.usd.trim() === "" ? Number.NaN : Number(value.usd);
    const input = value.input.trim() === "" ? Number.NaN : Number(value.input);
    const output = value.output.trim() === "" ? Number.NaN : Number(value.output);
    if (!Number.isFinite(usd) || usd < 0 || !Number.isInteger(input) || input < 0 || !Number.isInteger(output) || output < 0) {
      return { error: `Enter a non-negative USD amount and whole token values for the ${period} period.` };
    }
    const thresholds = parseThresholdSpec(value.thresholds);
    if (thresholds === null) return { error: `${periodLabel(period)} thresholds must look like "50:warn,80:warn,100:block" with a single trailing block.` };
    result[period] = { usd: normalizeUsd(usd), input_tokens: input, output_tokens: output, ...(thresholds ? { thresholds } : {}) };
  }
  if (enabledCount === 0) return { error: "Enable at least one period for the model budget." };
  return { limits: result as QuotaLimits };
}

export function ModelBudgetsSection({
  cfg,
  session,
  user,
  onCanonical,
}: {
  cfg: AdminConfig;
  session: Session;
  user: AdminUser;
  onCanonical: (user: AdminUser) => void;
}) {
  const [draft, setDraft] = useState<ModelBudgetDraft | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [usage, setUsage] = useState<Record<string, CurrentUsage>>({});
  const budgets = user.model_budgets ?? {};
  const modelIds = Object.keys(budgets).sort();

  useEffect(() => {
    let active = true;
    setUsage({});
    for (const modelId of modelIds) {
      void api.modelUsage(cfg, session, user.user_id, modelId).then((result) => {
        if (active) setUsage((current) => ({ ...current, [modelId]: result.current_usage }));
      }).catch(() => { /* usage is informational; the budget list still renders */ });
    }
    return () => { active = false; };
  }, [user.user_id, user.version, modelIds.join("|")]);

  async function save() {
    if (!draft) return;
    const modelId = draft.modelId.trim();
    if (!modelId) { setError("Enter the model or inference-profile ID as it appears in the invocation log."); return; }
    if (modelId.startsWith("arn:")) { setError("Use the model ID, not an ARN."); return; }
    const parsed = parseModelBudgetDraft(draft);
    if ("error" in parsed) { setError(parsed.error); return; }
    setBusy(true);
    setError("");
    try {
      const result = await api.setModelBudget(cfg, session, user, modelId, parsed.limits, draft.reason);
      onCanonical(result.data.user);
      setDraft(null);
      setEditing(null);
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  async function remove(modelId: string) {
    const reason = window.prompt(`Reason for removing the ${modelId} budget (stored in the audit trail):`, "");
    if (reason === null) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.removeModelBudget(cfg, session, user, modelId, reason);
      onCanonical(result.data.user);
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function setPeriod(period: QuotaPeriod, patch: Partial<ModelBudgetPeriodDraft>) {
    setDraft((current) => current ? { ...current, periods: { ...current.periods, [period]: { ...current.periods[period], ...patch } } } : current);
    setError("");
  }

  return (
    <section aria-labelledby="model-budgets-title" className="model-budgets">
      <div className="panel-heading-inline">
        <h3 id="model-budgets-title">Per-model budgets</h3>
        {!draft && <button className="button button-secondary button-small" disabled={busy} onClick={() => { setDraft(emptyModelBudgetDraft()); setEditing(null); }} type="button">Add model budget</button>}
      </div>
      <p className="field-help">A second axis on top of the subject limits, evaluated against this subject's per-model ledger. <strong>A breach on any model budget blocks the whole subject</strong> — enforcement is on the identity, not the model — and model budgets are not checked at credential vend time.</p>
      {error && <ErrorMessage message={error} />}
      {modelIds.length === 0 && !draft && <p className="operations-muted">No model budgets configured.</p>}
      {modelIds.length > 0 && (
        <div className="drawer-table-scroll" role="region" aria-label="Model budgets" tabIndex={0}>
          <table className="compact-table"><thead><tr><th>Model</th><th>Period</th><th>USD</th><th>Input tokens</th><th>Output tokens</th><th>Thresholds</th><th></th></tr></thead><tbody>
            {modelIds.flatMap((modelId) => QUOTA_PERIODS.filter((period) => budgets[modelId][period] !== null).map((period, index, enabled) => {
              const limits = budgets[modelId][period]!;
              const current = usage[modelId]?.[period];
              const alertOnly = (limits.thresholds ?? []).length > 0 && (limits.thresholds ?? []).every((entry) => entry.action !== "block");
              return (
                <tr key={`${modelId}-${period}`}>
                  {index === 0 && <td rowSpan={enabled.length}><code>{modelId}</code></td>}
                  <td>{periodLabel(period)}</td>
                  <td>{current ? `${formatUsd(current.cost_usd)} of ` : ""}{formatLimit(limits.usd, (value) => formatUsd(value))}</td>
                  <td>{current ? `${formatNumber(current.input_tokens)} of ` : ""}{formatLimit(limits.input_tokens, (value) => formatNumber(value))}</td>
                  <td>{current ? `${formatNumber(current.output_tokens)} of ` : ""}{formatLimit(limits.output_tokens, (value) => formatNumber(value))}</td>
                  <td>{formatThresholdSpec(limits.thresholds) || "default"}{alertOnly && <span className="ops-status ops-status-amber"> alert-only</span>}</td>
                  {index === 0 && (
                    <td rowSpan={enabled.length} className="compact-actions">
                      <button aria-label={`Edit ${modelId} budget`} className="icon-button" disabled={busy} onClick={() => { setDraft(emptyModelBudgetDraft(modelId, budgets[modelId])); setEditing(modelId); }} type="button"><Pencil aria-hidden="true" size={14} /></button>
                      <button aria-label={`Remove ${modelId} budget`} className="icon-button" disabled={busy} onClick={() => void remove(modelId)} type="button"><X aria-hidden="true" size={14} /></button>
                    </td>
                  )}
                </tr>
              );
            }))}
          </tbody></table>
        </div>
      )}
      {draft && (
        <form aria-label={editing ? `Edit ${editing} budget` : "New model budget"} className="model-budget-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label><span>Model or inference-profile ID</span><input aria-label="Model budget model ID" autoComplete="off" disabled={busy || editing !== null} onChange={(event) => { setDraft((current) => current ? { ...current, modelId: event.target.value } : current); setError(""); }} placeholder="us.anthropic.claude-opus-4-7" value={draft.modelId} /></label>
          <div className="quota-limit-matrix">
            {QUOTA_PERIODS.map((period) => (
              <fieldset className="quota-period-card" key={period}>
                <legend><label className="quota-period-toggle"><input aria-label={`Model budget ${period} enabled`} checked={draft.periods[period].enabled} disabled={busy} onChange={(event) => setPeriod(period, { enabled: event.target.checked })} type="checkbox" /><span>{periodLabel(period)}</span></label></legend>
                <div className="field-grid">
                  <label><span>USD</span><input aria-label={`Model budget ${period} USD limit`} disabled={busy || !draft.periods[period].enabled} min="0" onChange={(event) => setPeriod(period, { usd: event.target.value })} step="0.000001" type="number" value={draft.periods[period].usd} /></label>
                  <label><span>Input tokens</span><input aria-label={`Model budget ${period} input token limit`} disabled={busy || !draft.periods[period].enabled} min="0" onChange={(event) => setPeriod(period, { input: event.target.value })} step="1" type="number" value={draft.periods[period].input} /></label>
                  <label><span>Output tokens</span><input aria-label={`Model budget ${period} output token limit`} disabled={busy || !draft.periods[period].enabled} min="0" onChange={(event) => setPeriod(period, { output: event.target.value })} step="1" type="number" value={draft.periods[period].output} /></label>
                  <label><span>Thresholds</span><input aria-label={`Model budget ${period} thresholds`} disabled={busy || !draft.periods[period].enabled} onChange={(event) => setPeriod(period, { thresholds: event.target.value })} placeholder="80:warn,100:block (blank = default)" value={draft.periods[period].thresholds} /></label>
                </div>
              </fieldset>
            ))}
          </div>
          <label className="reason-field"><span>Reason</span><textarea aria-label="Model budget reason" disabled={busy} onChange={(event) => setDraft((current) => current ? { ...current, reason: event.target.value } : current)} rows={2} value={draft.reason} /></label>
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={() => { setDraft(null); setEditing(null); setError(""); }} type="button">Cancel</button>
            <button className="button button-primary" disabled={busy} type="submit">{busy ? <BusyLabel>Saving</BusyLabel> : editing ? "Save model budget" : "Add model budget"}</button>
          </div>
        </form>
      )}
    </section>
  );
}

export function GlobalAuditView({ cfg, session, onTargetUser }: { cfg: AdminConfig; session: Session; onTargetUser: (userId: string) => void }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [failedLoad, setFailedLoad] = useState<AuditLoadRequest | null>(null);
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const request = useRef(0);

  async function load(loadRequest: AuditLoadRequest) {
    const { cursor, targetIndex, reset } = loadRequest;
    const currentRequest = ++request.current;
    setLoading(true);
    setError("");
    setFailedLoad(null);
    try {
      const page = await api.listAuditPage(cfg, session, { limit: PAGE_SIZE, cursor });
      if (request.current !== currentRequest) return;
      setEvents(page.events);
      setNextCursor(page.next_cursor);
      if (reset) {
        setCursors([null]);
        setPageIndex(0);
      } else {
        setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
        setPageIndex(targetIndex);
      }
      setLastSuccessfulAt(Date.now());
    } catch (caught) {
      if (request.current === currentRequest) {
        setError(apiErrorMessage(caught));
        setFailedLoad(loadRequest);
      }
    } finally {
      if (request.current === currentRequest) setLoading(false);
    }
  }

  useEffect(() => {
    void load({ cursor: null, targetIndex: 0, reset: true });
    return () => { request.current += 1; };
  }, []);

  const loadedAt = lastSuccessfulAt === null
    ? null
    : new Date(lastSuccessfulAt).toLocaleString();

  return <section aria-busy={loading} aria-labelledby="audit-log-title" className="audit-panel">
    <div className="panel-heading">
      <div><h2 id="audit-log-title">Audit log</h2><p>Newest-first create, limit, and status changes retained by the broker.</p></div>
      <div className="audit-heading-actions">
        {loadedAt !== null && <p className="updated-at" role="status">Loaded {loadedAt}</p>}
        <button className="button button-secondary" onClick={() => void load({ cursor: null, targetIndex: 0, reset: true })} type="button"><RefreshCw className={loading ? "spin" : ""} aria-hidden="true" size={16} />Refresh audit log</button>
      </div>
    </div>
    {error && <ErrorMessage message={error} />}
    {failedLoad && <button className="button button-secondary audit-stale" onClick={() => void load(failedLoad)} type="button">Retry</button>}
    {error && loadedAt !== null && <span className="ops-status ops-status-amber audit-stale" role="status"><span aria-hidden="true" />Showing cached audit data loaded {loadedAt}.</span>}
    {loading && lastSuccessfulAt === null ? <div className="audit-loading"><BusyLabel>Loading audit log</BusyLabel></div> : lastSuccessfulAt !== null ? <><AuditTable events={events} label="Global administrative audit log" onTarget={onTargetUser} /><Pagination busy={loading} hasNext={Boolean(nextCursor)} hasPrevious={pageIndex > 0} label={`${events.length} events on this page`} onNext={() => nextCursor && void load({ cursor: nextCursor, targetIndex: pageIndex + 1, reset: false })} onPrevious={() => void load({ cursor: cursors[pageIndex - 1], targetIndex: pageIndex - 1, reset: false })} /></> : <div className="compact-empty">The audit log could not be loaded. Retry the request.</div>}
  </section>;
}

export function LiveLeases({ cfg, configuration, session }: {
  cfg: AdminConfig;
  configuration: Operations["configuration"];
  session: Session;
}) {
  const [rows, setRows] = useState<AdminUser[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const generations = useRef<Record<string, number>>({});
  const renewedAt = useRef<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const page = await api.leaseSnapshot(cfg, session);
        if (cancelled) return;
        for (const user of page.users) {
          const generation = user.lease?.generation;
          if (generation !== undefined && generations.current[user.user_id] !== undefined
              && generation > generations.current[user.user_id]) {
            renewedAt.current[user.user_id] = Date.now();
          }
          if (generation !== undefined) generations.current[user.user_id] = generation;
        }
        setRows(page.users);
        setLoaded(true);
      } catch {
        // Keep the previous rows; the next poll retries.
      }
    }
    void poll();
    const interval = window.setInterval(poll, 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [cfg, session]);

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, []);

  const leaseSeconds = configuration.permission_lease_seconds;
  const stsSeconds = configuration.credential_ttl_seconds;
  const active = rows.filter((user) => user.lease !== null && user.lease !== undefined);
  const clock = (iso: string) => new Date(iso).toISOString().slice(11, 19);

  return (
    <div aria-label="Live leases" className="credential-timeline live-leases">
      <div className="timeline-title">
        <RefreshCw aria-hidden="true" size={16} />
        <strong>Live leases</strong>
        <span>Currently vended credentials, updated every 5 seconds: grant time, time left on the lease, and renewals as they happen.</span>
      </div>
      {!loaded ? (
        <p className="operations-muted">Loading lease state…</p>
      ) : active.length === 0 ? (
        <p className="operations-muted">No vended credentials right now. Leases appear here the moment a user vends.</p>
      ) : active.map((user) => {
        const lease = user.lease!;
        const expires = new Date(lease.expires_at).getTime();
        const granted = new Date(lease.granted_at).getTime();
        const remaining = Math.max(0, Math.round((expires - now) / 1000));
        const pct = Math.min(Math.max(((now - granted) / (expires - granted)) * 100, 0), 100);
        const alive = lease.active && remaining > 0;
        const stsExpiry = granted + stsSeconds * 1000;
        const justRenewed = renewedAt.current[user.user_id] !== undefined
          && now - renewedAt.current[user.user_id] < 4000;
        return (
          <div className="timeline-row" key={user.user_id}>
            <span className="timeline-label">
              {user.name || user.user_id} · granted {clock(lease.granted_at)} UTC ·
              renewal #{lease.generation}
              {justRenewed && <span className="lease-renewed-flash"> · renewed</span>}
            </span>
            <div className="timeline-bar">
              <div
                className={alive ? "timeline-authority" : "timeline-dead"}
                style={{ width: "100%" }}
                title={`Lease of ${leaseSeconds}s · expires ${clock(lease.expires_at)} UTC · credentials remain valid until ~${new Date(stsExpiry).toISOString().slice(11, 19)} UTC`}
              >
                <div className="lease-progress" style={{ width: `${pct}%` }} aria-hidden="true" />
                <span className="lease-caption">
                  {alive
                    ? `lease ${remaining}s remaining of ${leaseSeconds}s`
                    : "lease expired: access is denied until the next renewal re-checks the quota"}
                </span>
              </div>
            </div>
            <div className="timeline-ticks">
              <span>granted {clock(lease.granted_at)}</span>
              <span>{lease.refresh_after ? `renewable from ${clock(lease.refresh_after)}` : ""}</span>
              <span>deadline {clock(lease.expires_at)} · credentials valid until ≈ {new Date(stsExpiry).toISOString().slice(11, 19)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
