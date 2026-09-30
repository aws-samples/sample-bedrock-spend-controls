import { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  BellRing,
  Check,
  Database,
  MoonStar,
  OctagonX,
  RefreshCw,
  Scale,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import type { AdminConfig } from "./config";
import type { Session } from "./auth";
import {
  ApiError,
  api,
  apiErrorMessage,
  type EmergencyAction,
  type EnforcementConfig,
  type Operations,
  type ReconciliationResponse,
} from "./api";
import { formatDecimal, formatNumber, formatSignedPercent, formatUsd } from "./format";
import { useModalLifecycle } from "./modal";
import { LiveLeases } from "./OperationalUi";

const EMERGENCY_CONFIRMATIONS: Record<EmergencyAction, string> = {
  activate: "STOP_ALL_BEDROCK_SESSIONS",
  recover: "RESTORE_ALL_BEDROCK_SESSIONS",
};

export function leaseWindowLabel(seconds: number): string {
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return minutes === 1 ? "1 minute" : `${minutes} minutes`;
  }
  return `${seconds} seconds`;
}

function formatOperationalLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${seconds / 60} min`;
}

function formatMilliseconds(value: number | null): string {
  if (value === null) return "No data";
  return value >= 1000 ? `${formatDecimal(value / 1000)}s` : `${Math.round(value)}ms`;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "No data";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Invalid timestamp" : timestamp.toLocaleString();
}

function alarmTone(state: string): string {
  if (state === "OK") return "green";
  if (state === "ALARM") return "red";
  return "gray";
}

function ErrorMessage({ message, dismiss }: { message: string; dismiss?: () => void }) {
  return (
    <div className="message message-error" role="alert">
      <AlertCircle aria-hidden="true" size={18} />
      <span>{message}</span>
      {dismiss && (
        <button aria-label="Dismiss error" onClick={dismiss} title="Dismiss" type="button">
          <X aria-hidden="true" size={16} />
        </button>
      )}
    </div>
  );
}

function SuccessMessage({ message, dismiss }: { message: string; dismiss: () => void }) {
  return (
    <div className="message message-success" role="status">
      <Check aria-hidden="true" size={18} />
      <span>{message}</span>
      <button aria-label="Dismiss notification" onClick={dismiss} title="Dismiss" type="button">
        <X aria-hidden="true" size={16} />
      </button>
    </div>
  );
}

function OperationsCard({
  children,
  className,
  icon,
  status,
  title,
  tone,
}: {
  children: React.ReactNode;
  className?: string;
  icon: React.ReactNode;
  status: string;
  title: string;
  tone: string;
}) {
  return (
    <article className={className ? `operations-card ${className}` : "operations-card"}>
      <div className="operations-card-heading">
        <div className="operations-card-title">{icon}<strong>{title}</strong></div>
        <span className={`ops-status ops-status-${tone}`}><span aria-hidden="true" />{status}</span>
      </div>
      <dl>{children}</dl>
    </article>
  );
}

function OperationsRow({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd title={value}>{value}</dd></div>;
}


export function reconciliationTone(
  response: ReconciliationResponse | null,
  alarmPercent: number | null,
): string {
  if (!response || !response.enabled) return "gray";
  const latest = response.latest ?? response.runs[0] ?? null;
  if (!latest) return "gray";
  if (latest.tag_inactive_workloads.length > 0) return "amber";
  const percent = latest.aggregate.delta_percent;
  if (percent === null) return "gray";
  if (alarmPercent !== null && Math.abs(percent) > alarmPercent) return "red";
  return "green";
}

// Nightly lift of automatic user blocks. The broker only lifts a block when
// the user next asks for credentials; this card shows whether the sweep that
// covers users who never come back actually ran, and what it did.
export function AutoBlockSweepCard({
  sweep,
  alarmState,
}: {
  sweep: Operations["auto_block_sweep"];
  alarmState: string | null;
}) {
  const icon = <MoonStar aria-hidden="true" size={19} />;
  const title = "Auto-block sweep";
  const run = sweep.last_run;
  if (!run) {
    return (
      <OperationsCard icon={icon} status="Never ran" title={title} tone="gray">
        <OperationsRow label="Schedule" value={sweep.schedule} />
        <OperationsRow label="Last run" value="No pass recorded yet" />
        <OperationsRow label="Lifts" value="Automatic blocks whose windows are under quota" />
      </OperationsCard>
    );
  }
  // The state row is the truth about the LAST pass; the alarm has a one-day
  // period, so it keeps ringing after a failed pass has been repaired by a
  // manual run. Show both without letting the alarm call a good pass failed.
  const lastFailed = sweep.status === "failed" || run.failures.length > 0;
  const alarmActive = alarmState === "ALARM";
  const status = lastFailed ? "Failed" : alarmActive ? "Alarm clearing" : "Ran";
  const tone = lastFailed ? "red" : alarmActive ? "amber" : "green";
  return (
    <OperationsCard icon={icon} status={status} title={title} tone={tone}>
      <OperationsRow label="Last run" value={formatTimestamp(run.ran_at)} />
      <OperationsRow label="Schedule" value={sweep.schedule} />
      <OperationsRow
        label="Lifted / still blocked"
        value={`${formatNumber(run.lifted)} / ${formatNumber(run.still_blocked)}`}
      />
      <OperationsRow label="Evaluated" value={`${formatNumber(run.evaluated)} blocked`} />
      <OperationsRow
        label="Admin blocks kept"
        value={`${formatNumber(run.admin_blocked)}${run.raced ? ` · ${formatNumber(run.raced)} raced` : ""}`}
      />
      {lastFailed && (
        <OperationsRow
          label="Failures"
          value={run.failures.length
            ? run.failures.map((failure) => `${failure.user_id}: ${failure.error}`).join(" · ")
            : "See the auto-block sweeper logs in CloudWatch"}
        />
      )}
      {alarmActive && !lastFailed && (
        <OperationsRow label="Alarm" value="An earlier pass failed today; the alarm clears within 24 hours of it" />
      )}
    </OperationsCard>
  );
}

// Daily ledger-vs-Cost-Explorer comparison. Reads the stored RECONCILE# rows
// through GET /admin/reconciliation; the broker never calls Cost Explorer on
// a page load. Disabled deployments get an explicit "off" card rather than a
// zero delta that could be mistaken for a clean bill.
export function SpendReconciliationCard({
  cfg,
  session,
  alarmState,
  refreshKey,
}: {
  cfg: AdminConfig;
  session: Session;
  alarmState: string | null;
  refreshKey?: string;
}) {
  const [response, setResponse] = useState<ReconciliationResponse | null>(null);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    api.reconciliation(cfg, session, 14)
      .then((next) => { if (!cancelled) setResponse(next); })
      .catch((caught) => { if (!cancelled) setLoadError(apiErrorMessage(caught)); });
    return () => { cancelled = true; };
  }, [cfg, session, refreshKey]);

  const icon = <Scale aria-hidden="true" size={19} />;
  if (loadError) {
    return (
      <OperationsCard className="operations-card-wide" icon={icon} status="Unavailable" title="Spend reconciliation" tone="gray">
        <OperationsRow label="Error" value={loadError} />
      </OperationsCard>
    );
  }
  if (!response) {
    return (
      <OperationsCard className="operations-card-wide" icon={icon} status="Loading" title="Spend reconciliation" tone="gray">
        <OperationsRow label="Ledger vs bill" value="Loading…" />
      </OperationsCard>
    );
  }
  if (!response.enabled) {
    return (
      <OperationsCard className="operations-card-wide" icon={icon} status="Disabled" title="Spend reconciliation" tone="gray">
        <OperationsRow label="Ledger vs bill" value="Not compared" />
        <OperationsRow label="Enable" value="reconciliation_enabled=true in the deployment config" />
      </OperationsCard>
    );
  }
  const latest = response.latest ?? response.runs[0] ?? null;
  const tone = alarmState === "ALARM" ? "red" : reconciliationTone(response, null);
  if (!latest) {
    return (
      <OperationsCard className="operations-card-wide" icon={icon} status="No runs yet" title="Spend reconciliation" tone="gray">
        <OperationsRow label="Schedule" value={`Daily · compares day D-${response.lag_days ?? "?"}`} />
        <OperationsRow label="Ledger vs bill" value="No run stored yet" />
      </OperationsCard>
    );
  }
  const status = latest.tag_inactive_workloads.length > 0
    ? "Tag inactive"
    : alarmState === "ALARM"
      ? "Drift"
      : latest.aggregate.delta_percent === null
        ? "No activity"
        : "Compared";
  return (
    <OperationsCard className="operations-card-wide" icon={icon} status={status} title="Spend reconciliation" tone={tone}>
      <OperationsRow label="Reconciled day" value={`${latest.day} (D-${response.lag_days ?? "?"})`} />
      <OperationsRow label="Ledger estimate" value={formatUsd(latest.aggregate.estimated_usd)} />
      <OperationsRow label="Cost Explorer" value={formatUsd(latest.aggregate.billed_usd)} />
      <OperationsRow
        label="Delta (bill − ledger)"
        value={`${formatUsd(latest.aggregate.delta_usd)} · ${formatSignedPercent(latest.aggregate.delta_percent)}`}
      />
      <OperationsRow
        label="Workloads"
        value={latest.workloads.length === 0
          ? "None configured"
          : latest.tag_inactive_workloads.length > 0
            ? `Cost-allocation tag inactive: ${latest.tag_inactive_workloads.join(", ")}`
            : latest.workloads.map((workload) => `${workload.name} ${formatSignedPercent(workload.delta_percent)}`).join(" · ")}
      />
      <OperationsRow label="Runs stored" value={`${response.runs.length} · last ${formatTimestamp(latest.run_at)}`} />
    </OperationsCard>
  );
}

// Runtime enforcement dial: reads GET /admin/enforcement (allowed values come
// from the broker, never hardcoded) and applies PUT /admin/enforcement.
// A dial change applies to new credentials immediately; outstanding leases
// keep their issued deadline.
export function EnforcementDialCard({
  cfg,
  session,
  onApplied,
  refreshKey,
}: {
  cfg: AdminConfig;
  session: Session;
  onApplied: () => void;
  refreshKey?: string;
}) {
  const [config, setConfig] = useState<EnforcementConfig | null>(null);
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [notice, setNotice] = useState("");
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);

  async function load() {
    setLoadError("");
    try {
      const next = await api.getEnforcement(cfg, session);
      setConfig(next);
      setSelected((current) => current ?? next.permission_lease_seconds);
    } catch (caught) {
      setLoadError(apiErrorMessage(caught));
    }
  }

  useEffect(() => { void load(); }, [refreshKey]);

  const options = config?.valid_permission_lease_seconds ?? [];
  const dirty = config !== null && selected !== null && selected !== config.permission_lease_seconds;

  function selectOption(index: number) {
    if (options.length === 0) return;
    const normalized = (index + options.length) % options.length;
    setSelected(options[normalized]);
    setApplyError("");
    setNotice("");
    optionRefs.current[normalized]?.focus();
  }

  function optionKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      selectOption(index + 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      selectOption(index - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      selectOption(0);
    } else if (event.key === "End") {
      event.preventDefault();
      selectOption(options.length - 1);
    }
  }

  async function apply(event: React.FormEvent) {
    event.preventDefault();
    if (!config || selected === null || !dirty || !reason.trim()) return;
    setBusy(true);
    setApplyError("");
    setNotice("");
    try {
      const next = await api.setEnforcement(
        cfg, session, selected, reason, config.generation
      );
      setConfig((current) => ({
        ...next,
        valid_permission_lease_seconds: current?.valid_permission_lease_seconds,
        default_permission_lease_seconds: current?.default_permission_lease_seconds,
      }));
      setSelected(next.permission_lease_seconds);
      setReason("");
      setNotice(`Permission lease is now ${leaseWindowLabel(next.permission_lease_seconds)} for newly vended credentials.`);
      onApplied();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "version_conflict") {
        const details = caught.details as { current_enforcement?: Partial<EnforcementConfig> } | undefined;
        const latest = details?.current_enforcement;
        if (latest && typeof latest.permission_lease_seconds === "number" && typeof latest.generation === "number") {
          setConfig((current) => ({
            ...current!,
            ...latest,
            permission_lease_seconds: latest.permission_lease_seconds!,
            generation: latest.generation!,
          }));
          setSelected(latest.permission_lease_seconds);
          setApplyError("The enforcement dial changed in another session. The latest value is shown; review it before applying another change.");
        } else {
          setApplyError(apiErrorMessage(caught));
        }
      } else {
        setApplyError(apiErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <article aria-labelledby="dial-title" className="control-card">
      <div className="control-card-heading">
        <div className="operations-card-title">
          <SlidersHorizontal aria-hidden="true" size={19} />
          <strong id="dial-title">Permission lease dial</strong>
        </div>
        {config && (
          <span className="ops-status ops-status-blue">
            <span aria-hidden="true" />
            {leaseWindowLabel(config.permission_lease_seconds)} · {config.source === "runtime" ? "runtime dial" : "deployment default"}
          </span>
        )}
      </div>
      <p className="control-card-help">
        How long each vended credential stays authorized before IAM re-checks the budget.
        Changes apply to new credentials immediately — no redeploy. Outstanding leases keep
        their issued deadline, and blocked identities are still cut by the revocation layer.
      </p>
      {loadError && (
        <>
          <ErrorMessage message={loadError} />
          <button className="button button-secondary" onClick={() => void load()} type="button">Retry</button>
        </>
      )}
      {!config && !loadError && <p className="operations-muted">Loading enforcement dial…</p>}
      {config && (
        <form onSubmit={(event) => void apply(event)}>
          <div aria-label="Permission lease window" className="segmented" role="radiogroup">
            {options.map((seconds, index) => (
              <button
                aria-checked={selected === seconds}
                className={selected === seconds ? "segment segment-active" : "segment"}
                disabled={busy}
                key={seconds}
                onClick={() => { setSelected(seconds); setApplyError(""); setNotice(""); }}
                onKeyDown={(event) => optionKeyDown(event, index)}
                ref={(element) => { optionRefs.current[index] = element; }}
                role="radio"
                tabIndex={selected === seconds ? 0 : -1}
                type="button"
              >
                {leaseWindowLabel(seconds)}
                {seconds === config.default_permission_lease_seconds && <small>deployment default</small>}
              </button>
            ))}
          </div>
          <dl className="control-meta">
            <div><dt>Last changed</dt><dd>{formatTimestamp(config.updated_at)}</dd></div>
            <div><dt>Changed by</dt><dd>{config.actor || "Not recorded"}</dd></div>
            <div><dt>Reason</dt><dd>{config.reason || "Not recorded"}</dd></div>
          </dl>
          {dirty && (
            <>
              <label className="reason-field">
                <span>Reason <strong aria-hidden="true">*</strong></span>
                <textarea
                  aria-label="Reason for lease window change"
                  aria-required="true"
                  disabled={busy}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Why the enforcement window is changing (stored in the audit trail)"
                  rows={2}
                  required
                  value={reason}
                />
              </label>
              <div className="control-actions">
                <button
                  className="button button-secondary"
                  disabled={busy}
                  onClick={() => { setSelected(config.permission_lease_seconds); setReason(""); setApplyError(""); }}
                  type="button"
                >
                  Cancel
                </button>
                <button className="button button-primary" disabled={busy || !reason.trim()} type="submit">
                  {busy ? <RefreshCw className="spin" aria-hidden="true" size={16} /> : <Check aria-hidden="true" size={16} />}
                  {busy ? "Applying" : `Apply ${selected === null ? "" : leaseWindowLabel(selected)} lease`}
                </button>
              </div>
            </>
          )}
          {applyError && <ErrorMessage message={applyError} dismiss={() => setApplyError("")} />}
          {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}
        </form>
      )}
    </article>
  );
}

export function EmergencyStopCard({
  cfg,
  session,
  emergency,
  onApplied,
}: {
  cfg: AdminConfig;
  session: Session;
  emergency: Operations["emergency"] | null;
  onApplied: () => void;
}) {
  const [dialogAction, setDialogAction] = useState<EmergencyAction | null>(null);
  const [notice, setNotice] = useState("");
  const active = emergency?.desired_active ?? false;
  const tone = emergency === null ? "gray" : active ? "red" : emergency.converged ? "green" : "amber";

  return (
    <article aria-labelledby="emergency-title" className="control-card control-card-emergency">
      <div className="control-card-heading">
        <div className="operations-card-title">
          <OctagonX aria-hidden="true" size={19} />
          <strong id="emergency-title">Emergency stop</strong>
        </div>
        <span className={`ops-status ops-status-${tone}`}>
          <span aria-hidden="true" />
          {emergency ? formatOperationalLabel(emergency.state) : "unknown"}
        </span>
      </div>
      <p className="control-card-help">
        Break-glass control: denies every Bedrock credential and active session for all
        identities at once. Requires the separate emergency key — the console never stores it.
      </p>
      {emergency && (
        <dl className="control-meta">
          <div><dt>Converged</dt><dd>{emergency.converged ? "Yes" : "No"}</dd></div>
          <div><dt>Generation</dt><dd>{emergency.applied_generation} / {emergency.generation}</dd></div>
          <div><dt>Requested</dt><dd>{formatTimestamp(emergency.requested_at)}</dd></div>
          <div><dt>Applied</dt><dd>{formatTimestamp(emergency.applied_at)}</dd></div>
        </dl>
      )}
      <div className="control-actions">
        {active ? (
          <button className="button button-primary" disabled={!emergency} onClick={() => setDialogAction("recover")} type="button">
            Recover from emergency stop
          </button>
        ) : (
          <button className="button button-danger" disabled={!emergency} onClick={() => setDialogAction("activate")} type="button">
            Activate emergency stop
          </button>
        )}
      </div>
      {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}
      {dialogAction && (
        <EmergencyDialog
          action={dialogAction}
          cfg={cfg}
          onClose={() => setDialogAction(null)}
          onRequested={(state) => {
            setDialogAction(null);
            setNotice(state.idempotent
              ? "The emergency state already matched this request; nothing changed."
              : `Emergency ${dialogAction === "activate" ? "stop" : "recovery"} requested. Convergence is reconciled automatically; watch the status above.`);
            onApplied();
          }}
          session={session}
        />
      )}
    </article>
  );
}

function EmergencyDialog({
  action,
  cfg,
  onClose,
  onRequested,
  session,
}: {
  action: EmergencyAction;
  cfg: AdminConfig;
  onClose: () => void;
  onRequested: (state: { idempotent?: boolean }) => void;
  session: Session;
}) {
  const [emergencyKey, setEmergencyKey] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const keyRef = useRef<HTMLInputElement>(null);
  useModalLifecycle(busy, onClose, dialogRef, keyRef);

  const activating = action === "activate";
  const expectedConfirmation = EMERGENCY_CONFIRMATIONS[action];
  const ready = emergencyKey.trim() !== "" && confirmation === expectedConfirmation && reason.trim() !== "";

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError("");
    try {
      const state = await api.setEmergencyStop(cfg, session, {
        action,
        confirmation,
        reason,
        emergencyKey: emergencyKey.trim(),
      });
      onRequested(state);
    } catch (caught) {
      setError(apiErrorMessage(caught));
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-describedby="emergency-dialog-help"
        aria-labelledby="emergency-dialog-title"
        aria-modal="true"
        className="dialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Break-glass control</p>
            <h2 id="emergency-dialog-title">{activating ? "Activate emergency stop" : "Recover from emergency stop"}</h2>
          </div>
          <button aria-label="Close dialog" className="icon-button" disabled={busy} onClick={onClose} type="button">
            <X aria-hidden="true" size={19} />
          </button>
        </div>
        <div className={`enforcement-warning${activating ? " enforcement-warning-destructive" : ""}`} id="emergency-dialog-help">
          <ShieldAlert aria-hidden="true" size={18} />
          <div>
            <strong>{activating ? "Every identity loses Bedrock access" : "Bedrock access is restored"}</strong>
            <p>
              {activating
                ? "All credential vending stops and every active session is denied once the change converges. Per-user quotas are not consulted while the stop is active."
                : "Vending and active sessions resume for identities that are not individually blocked. Configured calendar quotas apply again immediately."}
            </p>
          </div>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <label className="reason-field">
            <span>Emergency key <strong aria-hidden="true">*</strong></span>
            <input
              aria-label="Emergency key"
              autoComplete="off"
              disabled={busy}
              onChange={(event) => { setEmergencyKey(event.target.value); setError(""); }}
              ref={keyRef}
              type="password"
              value={emergencyKey}
            />
          </label>
          <p className="field-help">The separate break-glass secret. It is sent only with this request and never stored by the console.</p>
          <label className="reason-field">
            <span>Type <code>{expectedConfirmation}</code> to confirm <strong aria-hidden="true">*</strong></span>
            <input
              aria-label="Confirmation phrase"
              autoComplete="off"
              disabled={busy}
              onChange={(event) => { setConfirmation(event.target.value); setError(""); }}
              spellCheck={false}
              type="text"
              value={confirmation}
            />
          </label>
          <label className="reason-field">
            <span>Reason <strong aria-hidden="true">*</strong></span>
            <textarea
              aria-label="Emergency reason"
              disabled={busy}
              onChange={(event) => { setReason(event.target.value); setError(""); }}
              required
              rows={3}
              value={reason}
            />
          </label>
          {error && <ErrorMessage message={error} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">Cancel</button>
            <button className={`button ${activating ? "button-danger" : "button-primary"}`} disabled={busy || !ready} type="submit">
              {busy ? <RefreshCw className="spin" aria-hidden="true" size={17} /> : <OctagonX aria-hidden="true" size={17} />}
              {busy ? "Requesting" : activating ? "Stop all sessions" : "Restore sessions"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function OperationsView({
  cfg,
  error,
  loading,
  operations,
  onChanged,
  session,
  stale,
}: {
  cfg: AdminConfig;
  error: string;
  loading: boolean;
  operations: Operations | null;
  onChanged: () => void;
  session: Session;
  stale: boolean;
}) {
  const controls = (
    <div className="control-grid" aria-label="Enforcement controls">
      <EnforcementDialCard cfg={cfg} onApplied={onChanged} refreshKey={operations?.as_of} session={session} />
      <EmergencyStopCard cfg={cfg} emergency={operations?.emergency ?? null} onApplied={onChanged} session={session} />
    </div>
  );

  if (!operations) {
    return (
      <section className="operations-panel" aria-labelledby="operations-title">
        <div className="panel-heading operations-heading">
          <div>
            <h2 id="operations-title">Operations</h2>
            <p>{loading ? "Loading operational status…" : "Operational status is unavailable; quota administration remains independent."}</p>
          </div>
        </div>
        {error && <ErrorMessage message={error} />}
        {controls}
      </section>
    );
  }

  const config = operations.configuration;
  const metrics = operations.metrics;
  const enforcementTone = stale ? "gray" : "green";
  const reconciliationTone = stale
    ? "gray"
    : metrics.reconciliation_status === "current"
      ? "green"
      : metrics.reconciliation_status === "degraded"
        ? "red"
        : "amber";
  const cloudwatchTone = !stale && operations.cloudwatch.status === "available" && metrics.telemetry_status === "complete"
    ? "green"
    : operations.cloudwatch.status === "partial"
      ? "amber"
      : "gray";

  return (
    <section className="operations-panel" aria-labelledby="operations-title">
      <div className="panel-heading operations-heading">
        <div>
          <h2 id="operations-title">Operations</h2>
          <p>Runtime controls, live leases, component health, and alarms.</p>
        </div>
        <div className="operations-heading-meta">
          <span className={`ops-status ops-status-${stale ? "amber" : "gray"}`}>
            <span aria-hidden="true" />
            {stale ? `Cached from ${formatTimestamp(operations.as_of)} · refresh failed` : `Updated ${formatTimestamp(operations.as_of)}`}
          </span>
        </div>
      </div>
      {error && <ErrorMessage message={error} />}

      {controls}

      <LiveLeases cfg={cfg} configuration={config} session={session} />

      <div className="operations-grid">
        <OperationsCard
          icon={<ShieldCheck aria-hidden="true" size={19} />}
          title="Enforcement"
          status={formatOperationalLabel(config.mode)}
          tone={enforcementTone}
        >
          <OperationsRow label="Credential lifetime" value={formatDuration(config.credential_ttl_seconds)} />
          <OperationsRow label="Permission lease" value={`${formatDuration(config.permission_lease_seconds)}${config.permission_lease_source === "runtime" ? " · runtime dial" : " · deployment default"}`} />
          <OperationsRow label="Revocation layer" value={`Always on · ${config.revocation_policy_shards} shards`} />
          <OperationsRow label="Refresh / rate" value={`${config.refresh_overlap_seconds}s overlap · ${config.refresh_jitter_seconds}s jitter · ${config.vend_rate_limit_per_minute}/min`} />
        </OperationsCard>

        <OperationsCard
          icon={<Database aria-hidden="true" size={19} />}
          title="Revocation"
          status={formatOperationalLabel(metrics.reconciliation_status)}
          tone={reconciliationTone}
        >
          <OperationsRow label="Policy capacity" value={`${config.revocation_policy_shards} × ${formatNumber(config.revocation_policy_max_characters)} chars`} />
          <OperationsRow label="Desired identities" value={metrics.revoked_identities_desired === null ? "No data" : formatNumber(metrics.revoked_identities_desired)} />
          <OperationsRow label="Last reconciliation" value={formatTimestamp(metrics.last_reconciliation_at)} />
          <OperationsRow label="Recent failures / overflow" value={`${metrics.recent_sync_failure_count ?? "No data"} / ${metrics.recent_overflow_count ?? "No data"}`} />
        </OperationsCard>

        <OperationsCard
          icon={<Activity aria-hidden="true" size={19} />}
          title="Telemetry"
          status={formatOperationalLabel(operations.cloudwatch.status)}
          tone={cloudwatchTone}
        >
          <OperationsRow label="Detection p95" value={formatMilliseconds(metrics.detection_lag_p95_ms)} />
          <OperationsRow label="Telemetry coverage" value={formatOperationalLabel(metrics.telemetry_status)} />
          <OperationsRow label="Metric sample" value={formatTimestamp(metrics.detection_lag_timestamp)} />
          <OperationsRow label="Metric namespace" value={metrics.namespace} />
          <OperationsRow label="Emergency failures" value={metrics.recent_emergency_failure_count === null ? "No data" : formatNumber(metrics.recent_emergency_failure_count)} />
        </OperationsCard>

        <AutoBlockSweepCard
          alarmState={operations.alarms.find((alarm) => alarm.key === "auto_block_sweep_failure")?.state ?? null}
          sweep={operations.auto_block_sweep}
        />

        <SpendReconciliationCard
          alarmState={operations.alarms.find((alarm) => alarm.key === "reconciliation_delta")?.state ?? null}
          cfg={cfg}
          refreshKey={operations.as_of}
          session={session}
        />
      </div>

      <div className="alarm-strip" aria-label="Operational alarms">
        <div className="alarm-title"><BellRing aria-hidden="true" size={16} /><strong>Alarms and DLQs</strong></div>
        {operations.alarms.length ? operations.alarms.map((alarm) => (
          <span className={`ops-status ops-status-${alarmTone(alarm.state)}`} key={alarm.key} title={alarm.updated_at ? `Updated ${formatTimestamp(alarm.updated_at)}` : "No state timestamp"}>
            <span aria-hidden="true" />
            {formatOperationalLabel(alarm.key)}: {formatOperationalLabel(alarm.state)}
          </span>
        )) : <span className="operations-muted">No alarm metadata configured.</span>}
      </div>
    </section>
  );
}
