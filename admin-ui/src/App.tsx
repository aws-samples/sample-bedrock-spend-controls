import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Boxes,
  Check,
  ChevronDown,
  CircleDollarSign,
  Gauge,
  Info,
  Layers3,
  Lock,
  LogOut,
  Pencil,
  RefreshCw,
  ScrollText,
  Search,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Unlock,
  Users,
  X,
} from "lucide-react";
import { loadConfig, type AdminConfig } from "./config";
import { beginSignIn, handleAuthCallback, type Session } from "./auth";
import {
  AUTOMATIC_BLOCK_HINT,
  ApiError,
  DEFAULT_THRESHOLDS,
  api,
  apiErrorMessage,
  isAdminUser,
  isAlertOnly,
  isAutomaticBlock,
  isWorkload,
  thresholdsError,
  type AdminUser,
  type CurrentUsage,
  type Operations,
  type QuotaLimits,
  type QuotaPeriod,
  type QuotaThreshold,
  type RateLimits,
  type SetLimitsRequest,
  type Summary,
  type ThresholdAction,
  type TransportResponse,
  type UserRow,
  type UserStatus,
  type WorkloadEntry,
} from "./api";
import { formatCompact, formatNumber, formatUsd } from "./format";
import { CreateUserWizard, GlobalAuditView, UserDetailDrawer, workloadEnforcementLabel } from "./OperationalUi";
import { OverviewCharts } from "./OverviewCharts";
import { OperationsView } from "./Operations";
import { useModalLifecycle } from "./modal";

export type UserFilter = "all" | "active" | "blocked";
export type DashboardView = "overview" | "users" | "workloads" | "operations" | "audit";

/** The Users tab lists signed-in users only; workload rows never match. */
export function matchesUserFilter(user: AdminUser, filter: UserFilter): boolean {
  if (isWorkload(user)) return false;
  if (filter === "active" || filter === "blocked") return user.status === filter;
  return true;
}
const USER_PAGE_SIZE = 25;
const QUOTA_PERIODS: QuotaPeriod[] = ["daily", "weekly", "monthly"];

function periodBounds(period: QuotaPeriod, now = new Date()): { start: Date; end: Date } {
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  if (period === "weekly") start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
  if (period === "monthly") start.setUTCDate(1);
  const end = new Date(start);
  if (period === "daily") end.setUTCDate(end.getUTCDate() + 1);
  else if (period === "weekly") end.setUTCDate(end.getUTCDate() + 7);
  else end.setUTCMonth(end.getUTCMonth() + 1);
  return { start, end };
}

function emptyCurrentUsage(): CurrentUsage {
  return Object.fromEntries(QUOTA_PERIODS.map((period) => {
    const { start, end } = periodBounds(period);
    return [period, {
      period,
      window: start.toISOString().slice(0, 10),
      window_start: start.toISOString(),
      window_end: end.toISOString(),
      resets_at: end.toISOString(),
      cost_usd: 0,
      input_tokens: 0,
      output_tokens: 0,
      requests: 0,
    }];
  })) as unknown as CurrentUsage;
}

export function mergeCanonicalUser(rows: UserRow[], updated: AdminUser): UserRow[] {
  return rows.map((user) => user.user_id === updated.user_id && updated.version >= user.version
    ? { ...updated, today: user.today, current_usage: user.current_usage }
    : user);
}

export function mergeRefreshedUsers(current: UserRow[], refreshed: UserRow[]): UserRow[] {
  const currentById = new Map(current.map((user) => [user.user_id, user]));
  return refreshed.map((user) => {
    const cached = currentById.get(user.user_id);
    return cached && cached.version > user.version
      ? { ...cached, today: user.today, current_usage: user.current_usage }
      : user;
  });
}

/** Same last-writer-wins rule as users, applied to the metered subject row
 *  inside each roster entry. Roster identity always comes from the server. */
export function mergeRefreshedWorkloads(current: WorkloadEntry[], refreshed: WorkloadEntry[]): WorkloadEntry[] {
  const currentById = new Map(current.map((entry) => [entry.workload_id, entry]));
  return refreshed.map((entry) => {
    const cached = currentById.get(entry.workload_id)?.subject;
    if (!entry.subject || !cached || cached.version <= entry.subject.version) return entry;
    return { ...entry, subject: { ...cached, today: entry.subject.today, current_usage: entry.subject.current_usage } };
  });
}

export function App() {
  const [cfg, setCfg] = useState<AdminConfig | null>(null);
  const [cfgError, setCfgError] = useState("");
  const [authError, setAuthError] = useState("");
  const [authReady, setAuthReady] = useState(false);
  const [session, setSession] = useState<Session | null>(null);
  const [initialAuthHref] = useState(() => window.location.href);

  useEffect(() => {
    let active = true;
    let config: AdminConfig;
    try {
      config = loadConfig();
      setCfg(config);
    } catch (error) {
      setCfgError((error as Error).message);
      return () => {
        active = false;
      };
    }

    void handleAuthCallback(config, {
      onReauthenticate: () => {
        if (active) setSession(null);
      },
    }, initialAuthHref).then((nextSession) => {
      if (active) setSession(nextSession);
    }).catch((error: unknown) => {
      if (active) setAuthError((error as Error).message);
    }).finally(() => {
      if (active) setAuthReady(true);
    });

    return () => {
      active = false;
    };
  }, []);

  if (cfgError) {
    return (
      <Centered>
        <ErrorMessage message={cfgError} />
      </Centered>
    );
  }
  if (!cfg || !authReady) {
    return (
      <Centered>
        <LoadingState label={cfg ? "Completing sign in" : "Loading console"} />
      </Centered>
    );
  }
  if (!session) return <Login cfg={cfg} initialError={authError} />;
  return (
    <Dashboard
      cfg={cfg}
      session={session}
      onSignOut={() => {
        setSession(null);
        session.logout();
      }}
    />
  );
}

function Login({ cfg, initialError }: { cfg: AdminConfig; initialError: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await beginSignIn(cfg);
    } catch (caught) {
      setError((caught as Error).message);
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <Brand />
        <div className="login-heading">
          <p className="eyebrow">Administration</p>
          <h1 id="login-title">Sign in to Bedrock Spend Controls</h1>
          <p>Continue to your organization&apos;s sign-in page to authenticate.</p>
        </div>

        {error && <ErrorMessage message={error} />}
        <button
          className="button button-primary login-submit"
          disabled={busy}
          onClick={() => void submit()}
          type="button"
        >
          {busy && <RefreshCw className="spin" aria-hidden="true" size={17} />}
          {busy ? "Redirecting" : "Continue to sign in"}
        </button>

        <div className="login-security">
          <ShieldCheck aria-hidden="true" size={17} />
          <span>PKCE protects the redirect; access still requires the configured administrator claim.</span>
        </div>
      </section>
    </main>
  );
}

export function Dashboard({
  cfg,
  session,
  onSignOut,
}: {
  cfg: AdminConfig;
  session: Session;
  onSignOut: () => void;
}) {
  const [view, setView] = useState<DashboardView>("overview");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [usersNextCursor, setUsersNextCursor] = useState<string | null>(null);
  const [userCursors, setUserCursors] = useState<Array<string | null>>([null]);
  const [userPageIndex, setUserPageIndex] = useState(0);
  const [userQuery, setUserQuery] = useState("");
  const [userFilter, setUserFilter] = useState<UserFilter>("all");
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [usersError, setUsersError] = useState("");
  const [operationsError, setOperationsError] = useState("");
  const [summaryStale, setSummaryStale] = useState(false);
  const [usersStale, setUsersStale] = useState(false);
  const [operationsStale, setOperationsStale] = useState(false);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [usersLoading, setUsersLoading] = useState(true);
  const [operationsLoading, setOperationsLoading] = useState(true);
  const [workloads, setWorkloads] = useState<WorkloadEntry[]>([]);
  const [workloadsMeta, setWorkloadsMeta] = useState<{ rosterSource: string; tagKey: string } | null>(null);
  const [workloadsError, setWorkloadsError] = useState("");
  const [workloadsStale, setWorkloadsStale] = useState(false);
  const [workloadsLoading, setWorkloadsLoading] = useState(true);
  const [selectedWorkloadId, setSelectedWorkloadId] = useState<string | null>(null);
  const summaryRequest = useRef(0);
  const usersRequest = useRef(0);
  const operationsRequest = useRef(0);
  const workloadsRequest = useRef(0);

  async function refreshSummary() {
    const request = ++summaryRequest.current;
    setSummaryLoading(true);
    setSummaryError("");
    try {
      const next = await api.summary(cfg, session);
      if (request !== summaryRequest.current) return;
      setSummary(next);
      setSummaryStale(false);
    } catch (caught) {
      if (request !== summaryRequest.current) return;
      setSummaryError(apiErrorMessage(caught));
      setSummaryStale(true);
    } finally {
      if (request === summaryRequest.current) setSummaryLoading(false);
    }
  }

  async function loadUsersPage({
    cursor = userCursors[userPageIndex],
    targetIndex = userPageIndex,
    query = userQuery,
    filter = userFilter,
    resetHistory = false,
  }: {
    cursor?: string | null;
    targetIndex?: number;
    query?: string;
    filter?: UserFilter;
    resetHistory?: boolean;
  } = {}): Promise<boolean> {
    const request = ++usersRequest.current;
    setUsersLoading(true);
    setUsersError("");
    try {
      const page = await api.listUsersPage(cfg, session, {
        limit: USER_PAGE_SIZE,
        cursor,
        status: filter === "active" || filter === "blocked" ? filter : undefined,
        // Workloads have their own tab; this list is signed-in users only.
        granularity: "user",
        query,
      });
      if (request !== usersRequest.current) return false;
      setUsers((current) => mergeRefreshedUsers(current, page.users));
      setUsersNextCursor(page.next_cursor);
      setUserQuery(query);
      setUserFilter(filter);
      if (resetHistory) {
        setUserCursors([null]);
        setUserPageIndex(0);
      } else {
        setUserCursors((current) => targetIndex > userPageIndex
          ? [...current.slice(0, userPageIndex + 1), cursor]
          : current);
        setUserPageIndex(targetIndex);
      }
      setSelectedUserId((current) => current && page.users.some((user) => user.user_id === current) ? current : null);
      setUsersStale(false);
      return true;
    } catch (caught) {
      if (request !== usersRequest.current) return false;
      setUsersError(apiErrorMessage(caught));
      setUsersStale(true);
      return false;
    } finally {
      if (request === usersRequest.current) setUsersLoading(false);
    }
  }

  async function refreshOperations() {
    const request = ++operationsRequest.current;
    setOperationsLoading(true);
    setOperationsError("");
    try {
      const next = await api.operations(cfg, session);
      if (request !== operationsRequest.current) return;
      setOperations(next);
      setOperationsStale(false);
    } catch (caught) {
      if (request !== operationsRequest.current) return;
      setOperationsError(apiErrorMessage(caught));
      setOperationsStale(true);
    } finally {
      if (request === operationsRequest.current) setOperationsLoading(false);
    }
  }

  async function refreshWorkloads() {
    const request = ++workloadsRequest.current;
    setWorkloadsLoading(true);
    setWorkloadsError("");
    try {
      const next = await api.listWorkloads(cfg, session);
      if (request !== workloadsRequest.current) return;
      setWorkloads((current) => mergeRefreshedWorkloads(current, next.workloads));
      setWorkloadsMeta({ rosterSource: next.roster_source, tagKey: next.tag_key });
      setSelectedWorkloadId((current) => current && next.workloads.some((entry) => entry.workload_id === current) ? current : null);
      setWorkloadsStale(false);
    } catch (caught) {
      if (request !== workloadsRequest.current) return;
      setWorkloadsError(apiErrorMessage(caught));
      setWorkloadsStale(true);
    } finally {
      if (request === workloadsRequest.current) setWorkloadsLoading(false);
    }
  }

  async function refresh() {
    await Promise.allSettled([refreshSummary(), loadUsersPage(), refreshWorkloads(), refreshOperations()]);
  }

  function replaceWorkloadSubject(updated: AdminUser) {
    workloadsRequest.current += 1;
    setWorkloadsLoading(false);
    setWorkloads((current) => current.map((entry) => {
      if (entry.workload_id !== updated.user_id || !entry.subject) return entry;
      if (updated.version < entry.subject.version) return entry;
      return { ...entry, subject: { ...updated, today: entry.subject.today, current_usage: entry.subject.current_usage } };
    }));
  }

  function replaceUser(updated: AdminUser) {
    usersRequest.current += 1;
    setUsersLoading(false);
    setUsers((current) => {
      const cached = current.find((user) => user.user_id === updated.user_id);
      if (cached && updated.version < cached.version) return current;
      return !matchesUserFilter(updated, userFilter)
        ? current.filter((user) => user.user_id !== updated.user_id)
        : mergeCanonicalUser(current, updated);
    });
  }

  function addCreatedUser(created: AdminUser, openDetails: boolean) {
    const needle = userQuery.trim().toLocaleLowerCase();
    const matches = matchesUserFilter(created, userFilter) &&
      (!needle || created.user_id.toLocaleLowerCase().includes(needle) || created.name.toLocaleLowerCase().includes(needle));
    const fitsCurrentPage = userPageIndex === 0 && users.length < USER_PAGE_SIZE && matches;
    if (fitsCurrentPage) {
      const row: UserRow = {
        ...created,
        today: { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
        current_usage: emptyCurrentUsage(),
      };
      setUsers((current) => [row, ...current.filter((user) => user.user_id !== created.user_id)]);
      if (openDetails) setSelectedUserId(created.user_id);
    }
    void refreshSummary();
  }

  function openAuditTarget(userId: string) {
    // Audit events carry only the subject id; "workload:" is the prefix the
    // broker reserves for workload rows.
    if (userId.startsWith("workload:")) {
      setView("workloads");
      setSelectedWorkloadId(workloads.some((entry) => entry.workload_id === userId && entry.subject) ? userId : null);
      return;
    }
    setView("users");
    if (users.some((user) => user.user_id === userId)) {
      setSelectedUserId(userId);
      return;
    }
    setSelectedUserId(null);
    void loadUsersPage({ cursor: null, targetIndex: 0, query: userId, filter: "all", resetHistory: true });
  }

  useEffect(() => { void refresh(); }, []);

  const refreshing = summaryLoading || usersLoading || workloadsLoading || operationsLoading;
  const tabs: Array<{ id: DashboardView; label: string; icon: React.ReactNode }> = [
    { id: "overview", label: "Overview", icon: <Gauge aria-hidden="true" size={15} /> },
    { id: "users", label: "Users", icon: <Users aria-hidden="true" size={15} /> },
    { id: "workloads", label: "Workloads", icon: <Boxes aria-hidden="true" size={15} /> },
    { id: "operations", label: "Operations", icon: <SlidersHorizontal aria-hidden="true" size={15} /> },
    { id: "audit", label: "Audit log", icon: <ScrollText aria-hidden="true" size={15} /> },
  ];

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-inner">
          <Brand />
          <div className="header-account">
            <div className="account-copy"><span>{session.email}</span><small>Administrator</small></div>
            <IconButton label="Refresh data" onClick={() => void refresh()}><RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" size={18} /></IconButton>
            <IconButton label="Sign out" onClick={onSignOut}><LogOut aria-hidden="true" size={18} /></IconButton>
          </div>
        </div>
      </header>

      <nav aria-label="Primary" className="primary-nav">
        <div>
          {tabs.map((tab) => (
            <button aria-current={view === tab.id ? "page" : undefined} key={tab.id} onClick={() => setView(tab.id)} type="button">
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="dashboard">
        {view === "overview" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>Spend overview</h1></div>
              {summary && <p className="updated-at">Updated {new Date(summary.enforcement.as_of).toLocaleString()}</p>}
            </div>

            {(summaryError || (summaryStale && summary)) && (
              <div className="panel-feedback" aria-label="Summary refresh status">
                {summaryStale && summary && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Cached summary · refresh failed</span>}
                {summaryError && <ErrorMessage message={summaryError} />}
              </div>
            )}
            {summary ? <SummaryPanel onNavigate={setView} summary={summary} /> : summaryLoading ? <SummarySkeleton /> : <UnavailableState label="Summary unavailable" />}

            <OverviewCharts cfg={cfg} refreshKey={summary?.enforcement.as_of} session={session} users={users} />
          </>
        )}

        {view === "users" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>User management</h1><p className="page-subtitle">Signed-in users from your identity provider who obtain short-lived Bedrock credentials from the broker. Apps on their own IAM principals are managed under <button className="link-button" onClick={() => setView("workloads")} type="button">Workloads</button>.</p></div>
            </div>
            <UsersPanel
              cfg={cfg}
              enforcement={!summaryStale ? summary?.enforcement ?? null : null}
              error={usersError}
              filter={userFilter}
              hasNext={Boolean(usersNextCursor)}
              hasPrevious={userPageIndex > 0}
              loading={usersLoading}
              onCreateUser={addCreatedUser}
              onFilterChange={(filter) => void loadUsersPage({ cursor: null, targetIndex: 0, query: userQuery, filter, resetHistory: true })}
              onNext={() => usersNextCursor && void loadUsersPage({ cursor: usersNextCursor, targetIndex: userPageIndex + 1 })}
              onPrevious={() => void loadUsersPage({ cursor: userCursors[userPageIndex - 1], targetIndex: userPageIndex - 1 })}
              onSearch={(query) => void loadUsersPage({ cursor: null, targetIndex: 0, query, filter: userFilter, resetHistory: true })}
              onSelectedUserChange={setSelectedUserId}
              onSummaryRefresh={refreshSummary}
              onUserChanged={replaceUser}
              query={userQuery}
              selectedUserId={selectedUserId}
              session={session}
              stale={usersStale}
              users={users}
            />
          </>
        )}

        {view === "workloads" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>Workload management</h1><p className="page-subtitle">Applications that call Bedrock directly with their own IAM credentials, attributed by application inference profile. They never vend credentials; a block is an inline IAM Deny on the workload role.</p></div>
            </div>
            <WorkloadsPanel
              cfg={cfg}
              enforcement={!summaryStale ? summary?.enforcement ?? null : null}
              error={workloadsError}
              loading={workloadsLoading}
              meta={workloadsMeta}
              onSelectedWorkloadChange={setSelectedWorkloadId}
              onSubjectChanged={replaceWorkloadSubject}
              onSummaryRefresh={refreshSummary}
              selectedWorkloadId={selectedWorkloadId}
              session={session}
              stale={workloadsStale}
              workloads={workloads}
            />
          </>
        )}

        {view === "operations" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>Operations</h1></div>
            </div>
            <OperationsView
              cfg={cfg}
              error={operationsError}
              loading={operationsLoading}
              onChanged={() => { void refreshOperations(); void refreshSummary(); }}
              operations={operations}
              session={session}
              stale={operationsStale}
            />
          </>
        )}

        {view === "audit" && (
          <>
            <div className="page-heading"><div><p className="eyebrow">Amazon Bedrock</p><h1>Audit log</h1></div></div>
            <GlobalAuditView cfg={cfg} onTargetUser={openAuditTarget} session={session} />
          </>
        )}
      </main>
    </div>
  );
}

function SummaryPanel({ onNavigate, summary }: { onNavigate: (view: DashboardView) => void; summary: Summary }) {
  const enforcement = summary.enforcement;
  const subjects = enforcement.subjects;

  return (
    <>
      <section className="subject-grid" aria-label="Quota summary by subject kind">
        <SubjectGroup
          icon={<Users aria-hidden="true" size={18} />}
          kind="users"
          metrics={[
            { label: "Managed", value: formatNumber(subjects.users.total), icon: <Users aria-hidden="true" size={18} />, tone: "blue" },
            { label: "Blocked", value: formatNumber(subjects.users.blocked), icon: <Lock aria-hidden="true" size={18} />, tone: subjects.users.blocked > 0 ? "red" : "green" },
            { label: "Spend today", value: formatUsd(subjects.users.today.cost_usd), icon: <CircleDollarSign aria-hidden="true" size={18} />, tone: "orange" },
            { label: "Requests today", value: formatNumber(subjects.users.today.requests), icon: <Gauge aria-hidden="true" size={18} />, tone: "green" },
          ]}
          onNavigate={() => onNavigate("users")}
          subtitle="Signed-in users · credentials vended by the broker"
          title="Users"
        />
        <SubjectGroup
          chips={[
            subjects.workloads.awaiting_traffic > 0 ? { label: `${subjects.workloads.awaiting_traffic} awaiting traffic`, tone: "gray" as const, title: "Configured in workloads.json but no invocation since deploy; the row appears on the first metered call." } : null,
            subjects.workloads.metering_only > 0 ? { label: `${subjects.workloads.metering_only} metering only`, tone: "amber" as const, title: "No IAM role configured: metered and alerted, blocks are recorded but not enforced." } : null,
            subjects.workloads.unregistered > 0 ? { label: `${subjects.workloads.unregistered} unregistered`, tone: "gray" as const, title: "Metered rows no longer in the deployed roster." } : null,
          ].filter((chip): chip is { label: string; tone: "gray" | "amber"; title: string } => chip !== null)}
          icon={<Boxes aria-hidden="true" size={18} />}
          kind="workloads"
          metrics={[
            { label: "Configured", value: formatNumber(subjects.workloads.configured), icon: <Boxes aria-hidden="true" size={18} />, tone: "blue" },
            { label: "Blocked", value: formatNumber(subjects.workloads.blocked), icon: <Lock aria-hidden="true" size={18} />, tone: subjects.workloads.blocked > 0 ? "red" : "green" },
            { label: "Spend today", value: formatUsd(subjects.workloads.today.cost_usd), icon: <CircleDollarSign aria-hidden="true" size={18} />, tone: "orange" },
            { label: "Requests today", value: formatNumber(subjects.workloads.today.requests), icon: <Gauge aria-hidden="true" size={18} />, tone: "green" },
          ]}
          onNavigate={() => onNavigate("workloads")}
          subtitle="Apps on their own IAM role · attributed by inference profile"
          title="Workloads"
        />
      </section>

      <section className="system-strip" aria-label="Enforcement details">
        <div className="system-status">
          <span className="status-dot" aria-hidden="true" />
          <strong>Enforcement active</strong>
          <span className="system-window">Window {enforcement.window}</span>
          <span className="system-window">All subjects today {formatUsd(enforcement.today.cost_usd)} · {formatNumber(enforcement.today.requests)} req</span>
        </div>
        <dl className="system-details">
          <SystemDetail label="Credential lifetime" value={formatDuration(enforcement.credential_ttl_seconds)} />
          <SystemDetail label="Permission lease" value={`${formatDuration(enforcement.permission_lease_seconds)} · ${enforcement.permission_lease_source === "runtime" ? "runtime dial" : "deployment default"}`} />
          <SystemDetail label="Refresh" value={`${enforcement.refresh_overlap_seconds}s overlap · ${enforcement.refresh_jitter_seconds}s jitter`} />
          <SystemDetail label="Vend rate limit" value={`${enforcement.vend_rate_limit_per_minute} / min per user`} />
        </dl>
        <details className="system-technical">
          <summary>Technical details</summary>
          <dl className="system-details">
            <SystemDetail label="Enforcement mode" value={formatOperationalLabel(enforcement.mode)} />
            <SystemDetail label="Quota store" value={formatOperationalLabel(enforcement.source)} />
            <SystemDetail label="Telemetry source" value={formatOperationalLabel(summary.observability.source)} />
            <SystemDetail label="Telemetry delivery" value={formatOperationalLabel(summary.observability.delivery)} />
            <SystemDetail label="Detection lag metric" value={summary.observability.detection_lag_metric} mono />
            <SystemDetail label="Metrics namespace" value={summary.observability.metrics_namespace} mono />
          </dl>
        </details>
      </section>
    </>
  );
}

type MetricTone = "blue" | "red" | "orange" | "green";

function SubjectGroup({
  chips = [],
  icon,
  kind,
  metrics,
  onNavigate,
  subtitle,
  title,
}: {
  chips?: Array<{ label: string; tone: "gray" | "amber"; title: string }>;
  icon: React.ReactNode;
  kind: "users" | "workloads";
  metrics: Array<{ label: string; value: string; icon: React.ReactNode; tone: MetricTone }>;
  onNavigate: () => void;
  subtitle: string;
  title: string;
}) {
  return (
    <article aria-labelledby={`subject-${kind}-title`} className={`subject-group subject-group-${kind}`}>
      <header className="subject-group-heading">
        <div className="subject-group-title">
          <span className={`subject-group-icon subject-group-icon-${kind}`}>{icon}</span>
          <div>
            <h2 id={`subject-${kind}-title`}>{title}</h2>
            <p>{subtitle}</p>
          </div>
        </div>
        <div className="subject-group-tools">
          {chips.map((chip) => <span className={`ops-status ops-status-${chip.tone}`} key={chip.label} title={chip.title}><span aria-hidden="true" />{chip.label}</span>)}
          <button className="button button-secondary button-small" onClick={onNavigate} type="button">Manage {title.toLowerCase()}</button>
        </div>
      </header>
      <div className="subject-metrics">
        {metrics.map((metric) => (
          <div className="metric-card metric-card-compact" key={metric.label}>
            <div className={`metric-icon metric-icon-${metric.tone}`}>{metric.icon}</div>
            <div>
              <p>{metric.label}</p>
              <strong>{metric.value}</strong>
            </div>
          </div>
        ))}
      </div>
    </article>
  );
}

function formatOperationalLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${seconds / 60} min`;
}

export function UsersPanel({
  cfg,
  enforcement,
  error,
  filter = "all",
  hasNext = false,
  hasPrevious = false,
  loading,
  onCreateUser,
  onFilterChange = () => undefined,
  onNext = () => undefined,
  onPrevious = () => undefined,
  onSearch = () => undefined,
  onSelectedUserChange = () => undefined,
  onSummaryRefresh,
  onUserChanged,
  query = "",
  selectedUserId = null,
  session,
  stale,
  users,
}: {
  cfg: AdminConfig;
  enforcement: Summary["enforcement"] | null;
  error: string;
  filter?: UserFilter;
  hasNext?: boolean;
  hasPrevious?: boolean;
  loading: boolean;
  onCreateUser?: (user: AdminUser, openDetails: boolean) => void;
  onFilterChange?: (filter: UserFilter) => void;
  onNext?: () => void;
  onPrevious?: () => void;
  onSearch?: (query: string) => void;
  onSelectedUserChange?: (userId: string | null) => void;
  onSummaryRefresh: () => Promise<void>;
  onUserChanged: (user: AdminUser) => void;
  query?: string;
  selectedUserId?: string | null;
  session: Session;
  stale: boolean;
  users: UserRow[];
}) {
  const [searchDraft, setSearchDraft] = useState(query);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [statusChanging, setStatusChanging] = useState<UserRow | null>(null);
  const [busyUsers, setBusyUsers] = useState<Set<string>>(() => new Set());
  const [period, setPeriod] = useState<QuotaPeriod>("daily");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const selectedUser = selectedUserId ? users.find((user) => user.user_id === selectedUserId) ?? null : null;

  useEffect(() => { setSearchDraft(query); }, [query]);
  useEffect(() => {
    if (!enforcement) setStatusChanging(null);
  }, [enforcement]);

  function setUserBusy(userId: string, busy: boolean) {
    setBusyUsers((current) => {
      const next = new Set(current);
      if (busy) next.add(userId);
      else next.delete(userId);
      return next;
    });
  }

  function applyCanonicalUser(existing: UserRow, canonical: AdminUser): UserRow {
    onUserChanged(canonical);
    return {
      ...canonical,
      today: existing.today,
      current_usage: existing.current_usage,
    };
  }

  async function mutate(
    user: UserRow,
    successMessage: string,
    fn: () => Promise<TransportResponse<{ user: AdminUser }>>,
  ): Promise<boolean> {
    setUserBusy(user.user_id, true);
    setActionError("");
    setNotice("");
    try {
      const result = await fn();
      applyCanonicalUser(user, result.data.user);
      setNotice(successMessage);
      void onSummaryRefresh().catch(() => undefined);
      return true;
    } catch (caught) {
      let message = apiErrorMessage(caught);
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "version_conflict") {
        const details = caught.details as { current_user?: unknown } | undefined;
        const current = details?.current_user;
        if (isAdminUser(current) && current.user_id === user.user_id && current.version > user.version) {
          const reconciled = applyCanonicalUser(user, current);
          if (editing?.user_id === user.user_id) setEditing(reconciled);
          if (statusChanging?.user_id === user.user_id) setStatusChanging(reconciled);
        } else {
          message = apiErrorMessage(new ApiError(
            "The version conflict response did not identify the requested user.",
            409,
            "invalid_response",
            undefined,
            caught.requestId,
          ));
        }
      }
      setActionError(message);
      return false;
    } finally {
      setUserBusy(user.user_id, false);
    }
  }

  async function saveLimits(user: UserRow, limits: SetLimitsRequest) {
    const saved = await mutate(user, `Limits saved for ${displayName(user)}.`, () => api.setLimits(cfg, session, user, limits));
    if (saved) setEditing(null);
  }

  async function saveStatus(user: UserRow, reason: string) {
    const nextStatus: UserStatus = user.status === "active" ? "blocked" : "active";
    const saved = await mutate(user, `${displayName(user)} is now ${nextStatus}.`, () => api.setStatus(cfg, session, user, nextStatus, reason));
    if (saved) setStatusChanging(null);
  }

  function edit(user: UserRow) {
    setActionError("");
    setNotice("");
    setEditing(user);
  }

  function changeStatus(user: UserRow) {
    setActionError("");
    setNotice("");
    setStatusChanging(user);
  }

  return (
    <section className="users-panel" aria-labelledby="users-title" aria-busy={loading}>
      <div className="panel-heading">
        <div><h2 id="users-title">Users</h2><p>{error && users.length === 0 ? "User data unavailable" : `${users.length} ${users.length === 1 ? "user" : "users"} on this page`}</p></div>
        <div className="users-heading-actions">
          {stale && users.length > 0 && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Cached users · refresh failed</span>}
          <button className="button button-primary" onClick={() => { setActionError(""); setNotice(""); setCreating(true); }} type="button">Create user</button>
          <div className="user-tools">
            <div className="select-wrap"><select aria-label="Usage period" value={period} onChange={(event) => setPeriod(event.target.value as QuotaPeriod)}><option value="daily">Daily window</option><option value="weekly">Weekly window</option><option value="monthly">Monthly window</option></select><ChevronDown aria-hidden="true" size={16} /></div>
            <form className="search-form" onSubmit={(event) => { event.preventDefault(); onSearch(searchDraft.trim()); }}>
              <div className="search-field"><Search aria-hidden="true" size={17} /><input aria-label="Search users" placeholder="Search users" type="search" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} /></div>
              <button className="button button-secondary" disabled={loading} type="submit">Search</button>
            </form>
            <div className="select-wrap"><select aria-label="Filter users" disabled={loading} value={filter} onChange={(event) => onFilterChange(event.target.value as UserFilter)}><option value="all">All statuses</option><option value="active">Active</option><option value="blocked">Blocked</option></select><ChevronDown aria-hidden="true" size={16} /></div>
          </div>
        </div>
      </div>

      {error && <ErrorMessage message={error} />}
      {actionError && !editing && !statusChanging && <ErrorMessage message={actionError} dismiss={() => setActionError("")} />}
      {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}

      <div aria-label="Users on current page" className="table-scroll" role="region" tabIndex={0}>
        <table>
          <thead><tr><th>User</th><th>Status</th><th>{periodLabel(period)} USD</th><th>{periodLabel(period)} input</th><th>{periodLabel(period)} output</th><th>Requests</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{loading && users.length === 0 ? <TableSkeleton /> : users.map((user) => (
            <UserTableRow
              busy={busyUsers.has(user.user_id)}
              key={user.user_id}
              onEdit={() => edit(user)}
              onOpen={() => onSelectedUserChange(user.user_id)}
              onRequestStatus={() => changeStatus(user)}
              period={period}
              statusActionAvailable={enforcement !== null}
              user={user}
            />
          ))}</tbody>
        </table>
        {!loading && users.length === 0 && (error ? <UnavailableState label="Users unavailable" /> : <EmptyState hasFilters={Boolean(query) || filter !== "all"} />)}
      </div>
      <div className="pagination users-pagination"><p className="period-display-note"><Info aria-hidden="true" size={13} />Showing {period} usage · all enabled calendar periods are enforced concurrently</p><div><button className="button button-secondary" disabled={loading || !hasPrevious} onClick={onPrevious} type="button">Previous</button><button className="button button-secondary" disabled={loading || !hasNext} onClick={onNext} type="button">Next</button></div></div>

      {creating && <CreateUserWizard cfg={cfg} onClose={() => setCreating(false)} onCreated={(created, openDetails) => { onCreateUser?.(created, openDetails); setNotice(`${created.name || created.user_id} was created.`); }} session={session} />}
      {selectedUser && <UserDetailDrawer cfg={cfg} onCanonical={onUserChanged} onClose={() => onSelectedUserChange(null)} onEdit={() => edit(selectedUser)} onStatus={() => changeStatus(selectedUser)} session={session} statusActionAvailable={enforcement !== null} suspended={Boolean(editing || statusChanging)} user={selectedUser} />}
      {editing && <LimitsDialog apiError={actionError} busy={busyUsers.has(editing.user_id)} key={`${editing.user_id}:${editing.version}`} onClose={() => setEditing(null)} onSave={(limits) => void saveLimits(editing, limits)} user={editing} />}
      {statusChanging && enforcement && <StatusDialog apiError={actionError} busy={busyUsers.has(statusChanging.user_id)} enforcement={enforcement} key={`${statusChanging.user_id}:${statusChanging.version}`} onClose={() => setStatusChanging(null)} onConfirm={(reason) => void saveStatus(statusChanging, reason)} user={statusChanging} />}
    </section>
  );
}

export function WorkloadsPanel({
  cfg,
  enforcement,
  error,
  loading,
  meta,
  onSelectedWorkloadChange = () => undefined,
  onSubjectChanged,
  onSummaryRefresh,
  selectedWorkloadId = null,
  session,
  stale,
  workloads,
}: {
  cfg: AdminConfig;
  enforcement: Summary["enforcement"] | null;
  error: string;
  loading: boolean;
  meta: { rosterSource: string; tagKey: string } | null;
  onSelectedWorkloadChange?: (workloadId: string | null) => void;
  onSubjectChanged: (user: AdminUser) => void;
  onSummaryRefresh: () => Promise<void>;
  selectedWorkloadId?: string | null;
  session: Session;
  stale: boolean;
  workloads: WorkloadEntry[];
}) {
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [statusChanging, setStatusChanging] = useState<UserRow | null>(null);
  const [busy, setBusy] = useState<Set<string>>(() => new Set());
  const [period, setPeriod] = useState<QuotaPeriod>("daily");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const selected = selectedWorkloadId ? workloads.find((entry) => entry.workload_id === selectedWorkloadId)?.subject ?? null : null;
  const configured = workloads.filter((entry) => entry.registered).length;
  const metered = workloads.filter((entry) => entry.subject !== null).length;

  useEffect(() => {
    if (!enforcement) setStatusChanging(null);
  }, [enforcement]);

  function setSubjectBusy(id: string, value: boolean) {
    setBusy((current) => {
      const next = new Set(current);
      if (value) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function applyCanonical(existing: UserRow, canonical: AdminUser): UserRow {
    onSubjectChanged(canonical);
    return { ...canonical, today: existing.today, current_usage: existing.current_usage };
  }

  async function mutate(subject: UserRow, successMessage: string, fn: () => Promise<TransportResponse<{ user: AdminUser }>>): Promise<boolean> {
    setSubjectBusy(subject.user_id, true);
    setActionError("");
    setNotice("");
    try {
      const result = await fn();
      applyCanonical(subject, result.data.user);
      setNotice(successMessage);
      void onSummaryRefresh().catch(() => undefined);
      return true;
    } catch (caught) {
      let message = apiErrorMessage(caught);
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "version_conflict") {
        const current = (caught.details as { current_user?: unknown } | undefined)?.current_user;
        if (isAdminUser(current) && current.user_id === subject.user_id && current.version > subject.version) {
          const reconciled = applyCanonical(subject, current);
          if (editing?.user_id === subject.user_id) setEditing(reconciled);
          if (statusChanging?.user_id === subject.user_id) setStatusChanging(reconciled);
        } else {
          message = apiErrorMessage(new ApiError("The version conflict response did not identify the requested workload.", 409, "invalid_response", undefined, caught.requestId));
        }
      }
      setActionError(message);
      return false;
    } finally {
      setSubjectBusy(subject.user_id, false);
    }
  }

  async function saveLimits(subject: UserRow, limits: SetLimitsRequest) {
    if (await mutate(subject, `Limits saved for ${displayName(subject)}.`, () => api.setLimits(cfg, session, subject, limits))) setEditing(null);
  }

  async function saveStatus(subject: UserRow, reason: string) {
    const nextStatus: UserStatus = subject.status === "active" ? "blocked" : "active";
    const enforced = subject.workload?.enforcement_ready === true;
    const message = nextStatus === "blocked" && !enforced
      ? `Block recorded for ${displayName(subject)} (not enforced: no IAM role).`
      : `${displayName(subject)} is now ${nextStatus}.`;
    if (await mutate(subject, message, () => api.setStatus(cfg, session, subject, nextStatus, reason))) setStatusChanging(null);
  }

  return (
    <section className="users-panel" aria-labelledby="workloads-title" aria-busy={loading}>
      <div className="panel-heading">
        <div><h2 id="workloads-title">Workloads</h2><p>{error && workloads.length === 0 ? "Workload data unavailable" : `${configured} configured · ${metered} metered`}</p></div>
        <div className="users-heading-actions">
          {stale && workloads.length > 0 && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Cached workloads · refresh failed</span>}
          <div className="user-tools">
            <div className="select-wrap"><select aria-label="Workload usage period" value={period} onChange={(event) => setPeriod(event.target.value as QuotaPeriod)}><option value="daily">Daily window</option><option value="weekly">Weekly window</option><option value="monthly">Monthly window</option></select><ChevronDown aria-hidden="true" size={16} /></div>
          </div>
        </div>
      </div>

      {error && <ErrorMessage message={error} />}
      {actionError && !editing && !statusChanging && <ErrorMessage message={actionError} dismiss={() => setActionError("")} />}
      {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}

      <div aria-label="Configured workloads" className="table-scroll" role="region" tabIndex={0}>
        <table className="workloads-table">
          <thead><tr><th>Workload</th><th>Model</th><th>Enforcement</th><th>Status</th><th>{periodLabel(period)} USD</th><th>Requests</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{loading && workloads.length === 0 ? <TableSkeleton /> : workloads.map((entry) => (
            <WorkloadTableRow
              busy={busy.has(entry.workload_id)}
              entry={entry}
              key={entry.workload_id}
              onEdit={() => { if (entry.subject) { setActionError(""); setNotice(""); setEditing(entry.subject); } }}
              onOpen={() => entry.subject && onSelectedWorkloadChange(entry.workload_id)}
              onRequestStatus={() => { if (entry.subject) { setActionError(""); setNotice(""); setStatusChanging(entry.subject); } }}
              period={period}
              statusActionAvailable={enforcement !== null}
            />
          ))}</tbody>
        </table>
        {!loading && workloads.length === 0 && (error ? <UnavailableState label="Workloads unavailable" /> : <WorkloadsEmptyState />)}
      </div>
      <div className="pagination users-pagination"><p className="period-display-note"><Info aria-hidden="true" size={13} />Showing {period} usage · roster from {meta ? formatOperationalLabel(meta.rosterSource) : "deployment"}{meta && <> · cost tag <code>{meta.tagKey}</code></>}</p><div /></div>

      {selected && <UserDetailDrawer cfg={cfg} onCanonical={onSubjectChanged} onClose={() => onSelectedWorkloadChange(null)} onEdit={() => { setActionError(""); setNotice(""); setEditing(selected); }} onStatus={() => { setActionError(""); setNotice(""); setStatusChanging(selected); }} session={session} statusActionAvailable={enforcement !== null} suspended={Boolean(editing || statusChanging)} user={selected} />}
      {editing && <LimitsDialog apiError={actionError} busy={busy.has(editing.user_id)} key={`${editing.user_id}:${editing.version}`} onClose={() => setEditing(null)} onSave={(limits) => void saveLimits(editing, limits)} user={editing} />}
      {statusChanging && enforcement && <StatusDialog apiError={actionError} busy={busy.has(statusChanging.user_id)} enforcement={enforcement} key={`${statusChanging.user_id}:${statusChanging.version}`} onClose={() => setStatusChanging(null)} onConfirm={(reason) => void saveStatus(statusChanging, reason)} user={statusChanging} />}
    </section>
  );
}

function WorkloadTableRow({
  busy,
  entry,
  onEdit,
  onOpen,
  onRequestStatus,
  period,
  statusActionAvailable,
}: {
  busy: boolean;
  entry: WorkloadEntry;
  onEdit: () => void;
  onOpen: () => void;
  onRequestStatus: () => void;
  period: QuotaPeriod;
  statusActionAvailable: boolean;
}) {
  const subject = entry.subject;
  const enforcementState = workloadEnforcementLabel(entry);
  const isActive = subject?.status === "active";
  const usage = subject?.current_usage[period];
  const limits = subject?.limits[period] ?? null;
  const highest = subject ? highestUtilization(subject) : null;
  const shortModel = entry.model ? entry.model.replace(/^[a-z]{2}\./, "") : null;

  return (
    <tr className={subject ? undefined : "workload-row-silent"}>
      <td>
        <div className="user-cell">
          <div className="user-avatar user-avatar-workload" aria-hidden="true"><Boxes aria-hidden="true" size={15} /></div>
          <div>
            {subject
              ? <button className="user-name-button" disabled={busy} onClick={onOpen} title={entry.name} type="button">{entry.name}</button>
              : <strong className="user-name-static" title={entry.name}>{entry.name}</strong>}
            <span title={entry.workload_id}>{entry.workload_id}</span>
            {highest && <span className={`highest-utilization highest-${highest.level}`}>Highest: {periodLabel(highest.period)} {highest.percent}%</span>}
          </div>
        </div>
      </td>
      <td>{entry.model ? <code className="model-id" title={entry.model}>{shortModel}</code> : <span className="operations-muted">Unknown</span>}</td>
      <td>
        <div className="status-stack">
          <span className={`ops-status ops-status-plain ops-status-${enforcementState.tone}`} title={enforcementState.detail}><span aria-hidden="true" />{enforcementState.label}</span>
          {entry.role_arn && <details className="status-details"><summary>Role</summary><code className="break-all">{entry.role_arn}</code></details>}
        </div>
      </td>
      <td>
        {subject ? (
          <div className="status-stack">
            <span className={`status-badge status-${isActive ? "active" : "blocked"}`}><span aria-hidden="true" />{subject.status}</span>
            <details className="status-details">
              <summary>Status details</summary>
              <dl>
                <div><dt>Origin</dt><dd>{subject.status_origin || "Not provided"}</dd></div>
                <div><dt>Reason</dt><dd>{subject.status_reason || "Not provided"}</dd></div>
                {isAutomaticBlock(subject) && <div><dt>Lifts</dt><dd>{AUTOMATIC_BLOCK_HINT}</dd></div>}
              </dl>
            </details>
          </div>
        ) : (
          <span className="ops-status ops-status-plain ops-status-gray" title="Configured at deploy time; the quota row is created on the first metered invocation."><span aria-hidden="true" />Awaiting traffic</span>
        )}
      </td>
      <td>{usage ? <QuotaUsage current={usage.cost_usd} enabled={limits !== null} format={(value) => formatUsd(value)} limit={limits?.usd ?? 0} /> : <span className="operations-muted">—</span>}</td>
      <td className="request-count">{usage ? formatNumber(usage.requests) : "—"}</td>
      <td>
        <div className="row-actions">
          <IconButton disabled={busy || !subject} label={subject ? `Edit limits for ${entry.name}` : `${entry.name} has no quota row yet; limits apply after its first invocation`} onClick={onEdit}>
            <Pencil aria-hidden="true" size={17} />
          </IconButton>
          <IconButton
            danger={Boolean(subject) && isActive}
            disabled={busy || !subject || !statusActionAvailable}
            label={!subject
              ? `${entry.name} has no quota row yet`
              : !statusActionAvailable
                ? `Status change unavailable for ${entry.name} until a fresh enforcement summary loads`
                : isActive
                  ? (entry.enforcement_ready ? `Block ${entry.name}` : `Record block for ${entry.name} (not enforced)`)
                  : `Unblock ${entry.name}`}
            onClick={onRequestStatus}
          >
            {busy ? <RefreshCw className="spin" aria-hidden="true" size={17} /> : isActive || !subject ? <Lock aria-hidden="true" size={17} /> : <Unlock aria-hidden="true" size={17} />}
          </IconButton>
        </div>
      </td>
    </tr>
  );
}

function WorkloadsEmptyState() {
  return (
    <div className="empty-state">
      <Boxes aria-hidden="true" size={24} />
      <strong>No workloads configured</strong>
      <span>Declare apps in <code>cdk/config/workloads.json</code> (name, model, optional role_arn) and redeploy. Each gets an application inference profile; rows appear here on the first metered invocation.</span>
    </div>
  );
}

function UserTableRow({
  busy,
  onEdit,
  onOpen,
  onRequestStatus,
  period,
  statusActionAvailable,
  user,
}: {
  busy: boolean;
  onEdit: () => void;
  onOpen: () => void;
  onRequestStatus: () => void;
  period: QuotaPeriod;
  statusActionAvailable: boolean;
  user: UserRow;
}) {
  const isActive = user.status === "active";
  const usage = user.current_usage[period];
  const limits = user.limits[period];
  const highest = highestUtilization(user);

  return (
    <tr>
      <td>
        <div className="user-cell">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <button className="user-name-button" disabled={busy} onClick={onOpen} title={user.name} type="button">{displayName(user)}</button>
            <span title={user.user_id}>{user.user_id}</span>
            {highest && <span className={`highest-utilization highest-${highest.level}`}>Highest: {periodLabel(highest.period)} {highest.percent}%</span>}
          </div>
        </div>
      </td>
      <td>
        <div className="status-stack">
          <span className={`status-badge status-${isActive ? "active" : "blocked"}`}>
            <span aria-hidden="true" />
            {user.status}
          </span>
          <details className="status-details">
            <summary>Status details</summary>
            <dl>
              <div><dt>Origin</dt><dd>{user.status_origin || "Not provided"}</dd></div>
              <div><dt>Reason</dt><dd>{user.status_reason || "Not provided"}</dd></div>
              {isAutomaticBlock(user) && <div><dt>Lifts</dt><dd>{AUTOMATIC_BLOCK_HINT}</dd></div>}
            </dl>
          </details>
        </div>
      </td>
      <td>
        <QuotaUsage
          current={usage.cost_usd}
          enabled={limits !== null}
          format={(value) => formatUsd(value)}
          limit={limits?.usd ?? 0}
        />
      </td>
      <td>
        <QuotaUsage
          current={usage.input_tokens}
          enabled={limits !== null}
          format={formatCompact}
          limit={limits?.input_tokens ?? 0}
        />
      </td>
      <td>
        <QuotaUsage
          current={usage.output_tokens}
          enabled={limits !== null}
          format={formatCompact}
          limit={limits?.output_tokens ?? 0}
        />
      </td>
      <td className="request-count">{formatNumber(usage.requests)}</td>
      <td>
        <div className="row-actions">
          <IconButton disabled={busy} label={`Edit limits for ${displayName(user)}`} onClick={onEdit}>
            <Pencil aria-hidden="true" size={17} />
          </IconButton>
          <IconButton
            danger={isActive}
            disabled={busy || !statusActionAvailable}
            label={statusActionAvailable
              ? `${isActive ? "Block" : "Unblock"} ${displayName(user)}`
              : `Status change unavailable for ${displayName(user)} until a fresh enforcement summary loads`}
            onClick={onRequestStatus}
          >
            {busy ? (
              <RefreshCw className="spin" aria-hidden="true" size={17} />
            ) : isActive ? (
              <Lock aria-hidden="true" size={17} />
            ) : (
              <Unlock aria-hidden="true" size={17} />
            )}
          </IconButton>
        </div>
      </td>
    </tr>
  );
}

export function QuotaUsage({
  current,
  enabled = true,
  format,
  limit,
}: {
  current: number;
  enabled?: boolean;
  format: (value: number) => string;
  limit: number;
}) {
  if (!enabled) {
    return (
      <div className="quota-usage quota-disabled">
        <div><strong>{format(current)}</strong><span>period disabled</span></div>
        <span className="unlimited-label">Disabled</span>
      </div>
    );
  }
  if (limit === 0) {
    return (
      <div className="quota-usage quota-unlimited">
        <div>
          <strong>{format(current)}</strong>
          <span>of Unlimited</span>
        </div>
        <span className="unlimited-label">Unlimited</span>
      </div>
    );
  }

  const percentage = (current / limit) * 100;
  const level = percentage >= 100 ? "critical" : percentage >= 80 ? "warning" : "normal";
  const roundedPercentage = Math.round(percentage);

  return (
    <div className="quota-usage">
      <div>
        <strong>{format(current)}</strong>
        <span>of {format(limit)}</span>
      </div>
      <div
        aria-label={`${roundedPercentage} percent used`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.min(roundedPercentage, 100)}
        aria-valuetext={`${format(current)} of ${format(limit)} used (${roundedPercentage} percent)`}
        className="progress-track"
        role="progressbar"
      >
        <span
          className={`progress-fill progress-${level}`}
          style={{ width: `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}

type ThresholdDraft = { at: string; action: ThresholdAction };

type PeriodLimitDraft = {
  enabled: boolean;
  usd: string;
  input_tokens: string;
  output_tokens: string;
  thresholds: ThresholdDraft[];
};

type LimitDraft = Record<QuotaPeriod, PeriodLimitDraft>;

type RateDraft = { rpm: string; tpm: string };

function thresholdDrafts(thresholds: QuotaThreshold[] | undefined): ThresholdDraft[] {
  return (thresholds ?? DEFAULT_THRESHOLDS).map((entry) => ({
    // Percent in the editor; ratio on the wire.
    at: String(Math.round(entry.at * 10_000) / 100),
    action: entry.action,
  }));
}

/** Percent strings -> ratios; null when any entry is not a finite number. */
export function parseThresholdDrafts(drafts: ThresholdDraft[]): QuotaThreshold[] | null {
  const parsed: QuotaThreshold[] = [];
  for (const draft of drafts) {
    const percent = draft.at.trim() === "" ? Number.NaN : Number(draft.at);
    if (!Number.isFinite(percent)) return null;
    parsed.push({ at: Math.round(percent * 100) / 10_000, action: draft.action });
  }
  return parsed;
}

function limitDraft(limits: QuotaLimits): LimitDraft {
  return Object.fromEntries(QUOTA_PERIODS.map((period) => {
    const value = limits[period];
    return [period, {
      enabled: value !== null,
      usd: String(value?.usd ?? 0),
      input_tokens: String(value?.input_tokens ?? 0),
      output_tokens: String(value?.output_tokens ?? 0),
      thresholds: thresholdDrafts(value?.thresholds),
    }];
  })) as unknown as LimitDraft;
}

function rateDraft(rate: RateLimits | null | undefined): RateDraft {
  return { rpm: String(rate?.rpm ?? 0), tpm: String(rate?.tpm ?? 0) };
}

/** Returns null when the draft is not a valid rate; `{rpm:0,tpm:0}` = off. */
export function parseRateDraft(draft: RateDraft): RateLimits | null {
  const rpm = draft.rpm.trim() === "" ? Number.NaN : Number(draft.rpm);
  const tpm = draft.tpm.trim() === "" ? Number.NaN : Number(draft.tpm);
  if (!Number.isInteger(rpm) || rpm < 0 || !Number.isInteger(tpm) || tpm < 0) return null;
  return { rpm, tpm };
}

function sameThresholdList(a: QuotaThreshold[] | undefined, b: QuotaThreshold[] | undefined): boolean {
  const left = a ?? DEFAULT_THRESHOLDS;
  const right = b ?? DEFAULT_THRESHOLDS;
  return left.length === right.length &&
    left.every((entry, index) => Math.abs(entry.at - right[index].at) < 1e-9 && entry.action === right[index].action);
}

/** Parse the editor draft. `original` lets the payload omit an unchanged
 *  thresholds list (the API keeps the stored list when omitted), so an
 *  ordinary USD/token edit sends exactly what it did before thresholds
 *  existed. */
function parseLimitDraft(draft: LimitDraft, original?: QuotaLimits): QuotaLimits | null {
  const parsed: Partial<QuotaLimits> = {};
  for (const period of QUOTA_PERIODS) {
    const value = draft[period];
    if (!value.enabled) {
      parsed[period] = null;
      continue;
    }
    const usd = value.usd.trim() === "" ? Number.NaN : Number(value.usd);
    const input = value.input_tokens.trim() === "" ? Number.NaN : Number(value.input_tokens);
    const output = value.output_tokens.trim() === "" ? Number.NaN : Number(value.output_tokens);
    if (!Number.isFinite(usd) || usd < 0 || !Number.isInteger(input) || input < 0 || !Number.isInteger(output) || output < 0) return null;
    const thresholds = parseThresholdDrafts(value.thresholds);
    if (thresholds === null || thresholdsError(thresholds) !== null) return null;
    const previous = original?.[period];
    // Omit when unchanged from the stored list, or when a newly enabled
    // period keeps the default (the server materializes the deployment
    // default, which may differ from DEFAULT_THRESHOLDS' 80 %).
    const unchanged = previous
      ? sameThresholdList(previous.thresholds, thresholds)
      : sameThresholdList(undefined, thresholds);
    parsed[period] = unchanged
      ? { usd, input_tokens: input, output_tokens: output }
      : { usd, input_tokens: input, output_tokens: output, thresholds };
  }
  return Object.values(parsed).some((value) => value !== null)
    ? parsed as QuotaLimits
    : null;
}

function ThresholdsEditor({
  busy,
  idPrefix,
  onChange,
  periodLabelText,
  thresholds,
}: {
  busy: boolean;
  idPrefix: string;
  onChange: (next: ThresholdDraft[]) => void;
  periodLabelText: string;
  thresholds: ThresholdDraft[];
}) {
  const parsed = parseThresholdDrafts(thresholds);
  const problem = parsed === null ? "Every threshold needs a numeric percentage." : thresholdsError(parsed);
  const alertOnly = parsed !== null && problem === null && isAlertOnly(parsed);
  return (
    <fieldset className="thresholds-editor" data-testid={`${idPrefix}-thresholds`}>
      <legend>{periodLabelText} thresholds</legend>
      <ol className="thresholds-list">
        {thresholds.map((entry, index) => (
          <li key={index}>
            <label>
              <span className="sr-only">{periodLabelText} threshold {index + 1} percent</span>
              <div className="number-input">
                <input
                  aria-label={`${periodLabelText} threshold ${index + 1} percent`}
                  disabled={busy}
                  inputMode="decimal"
                  min="0.01"
                  max="1000"
                  step="0.01"
                  type="number"
                  value={entry.at}
                  onChange={(event) => onChange(thresholds.map((item, i) => i === index ? { ...item, at: event.target.value } : item))}
                />
                <span aria-hidden="true">%</span>
              </div>
            </label>
            <select
              aria-label={`${periodLabelText} threshold ${index + 1} action`}
              disabled={busy}
              value={entry.action}
              onChange={(event) => onChange(thresholds.map((item, i) => i === index ? { ...item, action: event.target.value as ThresholdAction } : item))}
            >
              <option value="warn">Warn</option>
              <option value="block">Block</option>
            </select>
            <button
              aria-label={`Remove ${periodLabelText} threshold ${index + 1}`}
              className="icon-button"
              disabled={busy || thresholds.length === 1}
              onClick={() => onChange(thresholds.filter((_, i) => i !== index))}
              type="button"
            >
              <X aria-hidden="true" size={14} />
            </button>
          </li>
        ))}
      </ol>
      <div className="thresholds-actions">
        <button
          className="button button-secondary button-small"
          disabled={busy}
          onClick={() => {
            const last = thresholds[thresholds.length - 1];
            const lastAt = Number(last?.at ?? 0);
            const nextAt = Number.isFinite(lastAt) && lastAt > 0 ? lastAt + 10 : 50;
            // Insert before a trailing block so the block stays last.
            const next = last?.action === "block"
              ? [...thresholds.slice(0, -1), { at: String(nextAt - 20 > 0 ? nextAt - 20 : nextAt), action: "warn" as ThresholdAction }, last]
              : [...thresholds, { at: String(nextAt), action: "warn" as ThresholdAction }];
            onChange(next);
          }}
          type="button"
        >
          Add threshold
        </button>
        {alertOnly && <span className="ops-status ops-status-amber" role="status"><span aria-hidden="true" />Alert-only: this period warns but never blocks</span>}
      </div>
      {problem && <p className="field-error" role="alert">{problem}</p>}
    </fieldset>
  );
}

export function LimitsDialog({
  apiError,
  busy,
  onClose,
  onSave,
  user,
}: {
  apiError: string;
  busy: boolean;
  onClose: () => void;
  onSave: (limits: SetLimitsRequest) => void;
  user: UserRow;
}) {
  // Editable snapshot of the user's limits. The parent keys this dialog by
  // user id and version, so a changed prop remounts it with a fresh draft.
  const [draft, setDraft] = useState<LimitDraft>(() => limitDraft(user.limits)); // nosemgrep
  const [rate, setRate] = useState<RateDraft>(() => rateDraft(user.rate)); // nosemgrep
  const [reason, setReason] = useState("");
  const [unlimitedConfirmed, setUnlimitedConfirmed] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  useModalLifecycle(busy, onClose, dialogRef, firstFieldRef);

  const parsedLimits = parseLimitDraft(draft, user.limits);
  const parsedRate = parseRateDraft(rate);
  const periodChanges = parsedLimits ? QUOTA_PERIODS.filter((period) => (user.limits[period] === null) !== (parsedLimits[period] === null)).map((period) => `${period} period`) : [];
  const unlimitedFields: string[] = [];
  const belowUsageFields: string[] = [];
  const alertOnlyPeriods: string[] = [];
  if (parsedLimits) {
    for (const period of QUOTA_PERIODS) {
      const previous = user.limits[period];
      const next = parsedLimits[period];
      if (!next) continue;
      const usage = user.current_usage[period];
      for (const dimension of ["usd", "input_tokens", "output_tokens"] as const) {
        if ((!previous || previous[dimension] > 0) && next[dimension] === 0) unlimitedFields.push(`${period} ${dimension.replace(/_/g, " ")}`);
        const current = dimension === "usd" ? usage.cost_usd : usage[dimension];
        if (next[dimension] > 0 && next[dimension] < current) belowUsageFields.push(`${period} ${dimension.replace(/_/g, " ")}`);
      }
      if (isAlertOnly(next.thresholds) && !isAlertOnly(previous?.thresholds)) alertOnlyPeriods.push(`${period} period`);
    }
  }
  const currentRate = user.rate ?? { rpm: 0, tpm: 0 };
  const rateChanged = parsedRate !== null && (parsedRate.rpm !== currentRate.rpm || parsedRate.tpm !== currentRate.tpm);
  const reasonRequired = periodChanges.length > 0 || unlimitedFields.length > 0 || belowUsageFields.length > 0 || alertOnlyPeriods.length > 0;

  function changed(period: QuotaPeriod, patch: Partial<PeriodLimitDraft>) {
    setDraft((current) => ({ ...current, [period]: { ...current[period], ...patch } }));
    setUnlimitedConfirmed(false);
    setError("");
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!parsedLimits) {
      setError(Object.values(draft).some((value) => value.enabled)
        ? "Enter a non-negative USD amount, whole token values, and a valid thresholds list. Fields cannot be blank."
        : "Enable at least one calendar quota period.");
      return;
    }
    if (parsedRate === null) {
      setError("Rate limits must be whole non-negative numbers (0 disables).");
      return;
    }
    if (unlimitedFields.length > 0 && !unlimitedConfirmed) {
      setError("Confirm that the selected limits should become Unlimited.");
      return;
    }
    const trimmedReason = reason.trim();
    if (reasonRequired && !trimmedReason) {
      setError("Enter a reason for this sensitive limit change.");
      return;
    }
    onSave({
      limits: parsedLimits,
      ...(rateChanged ? { rate: parsedRate.rpm === 0 && parsedRate.tpm === 0 ? null : parsedRate } : {}),
      ...(trimmedReason ? { reason: trimmedReason } : {}),
    });
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-describedby="limits-zero-help"
        aria-labelledby="limits-title"
        aria-modal="true"
        className="dialog dialog-wide"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Calendar allowances</p>
            <h2 id="limits-title">Edit quota limits</h2>
          </div>
          <IconButton label="Close dialog" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" size={19} />
          </IconButton>
        </div>
        <div className="dialog-user">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <strong>{displayName(user)}</strong>
            <span>{user.user_id}</span>
          </div>
        </div>
        <form onSubmit={submit}>
          <p className="field-help" id="limits-zero-help">Calendar windows use UTC. Enter 0 for an Unlimited dimension. Weekly and monthly limits include usage already recorded since the current period began.</p>
          <div className="quota-limit-matrix">
            {QUOTA_PERIODS.map((period, index) => (
              <fieldset className="quota-period-card" key={period}>
                <legend>
                  <label className="quota-period-toggle">
                    <input checked={draft[period].enabled} disabled={busy} onChange={(event) => changed(period, { enabled: event.target.checked })} type="checkbox" />
                    <span>{periodLabel(period)}</span>
                  </label>
                  <small>Resets {formatTimestamp(user.current_usage[period].resets_at)}</small>
                </legend>
                <div className="field-grid">
                  <label><span>{periodLabel(period)} USD limit</span><div className="number-input"><span aria-hidden="true">$</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} USD limit`} disabled={busy || !draft[period].enabled} min="0" ref={index === 0 ? firstFieldRef : undefined} step="0.000001" type="number" value={draft[period].usd} onChange={(event) => changed(period, { usd: event.target.value })} /></div></label>
                  <label><span>{periodLabel(period)} input token limit</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} input token limit`} disabled={busy || !draft[period].enabled} min="0" step="1" type="number" value={draft[period].input_tokens} onChange={(event) => changed(period, { input_tokens: event.target.value })} /></label>
                  <label><span>{periodLabel(period)} output token limit</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} output token limit`} disabled={busy || !draft[period].enabled} min="0" step="1" type="number" value={draft[period].output_tokens} onChange={(event) => changed(period, { output_tokens: event.target.value })} /></label>
                </div>
                {draft[period].enabled && (
                  <ThresholdsEditor
                    busy={busy}
                    idPrefix={period}
                    onChange={(thresholds) => changed(period, { thresholds })}
                    periodLabelText={periodLabel(period)}
                    thresholds={draft[period].thresholds}
                  />
                )}
              </fieldset>
            ))}
          </div>
          <fieldset className="quota-period-card rate-limits-card">
            <legend><span>Rate limits</span><small>Per UTC minute · 0 disables</small></legend>
            <p className="field-help">Counted from metered invocations: requests, and uncached input + output tokens. A breach blocks the subject through the automatic path and lifts on its own once the next minute is under the limit.</p>
            <div className="field-grid">
              <label><span>Requests per minute</span><input aria-label="Requests per minute limit" disabled={busy} min="0" step="1" type="number" value={rate.rpm} onChange={(event) => { setRate((current) => ({ ...current, rpm: event.target.value })); setError(""); }} /></label>
              <label><span>Tokens per minute</span><input aria-label="Tokens per minute limit" disabled={busy} min="0" step="1" type="number" value={rate.tpm} onChange={(event) => { setRate((current) => ({ ...current, tpm: event.target.value })); setError(""); }} /></label>
            </div>
          </fieldset>
          {periodChanges.length > 0 && (
            <div className="safety-warning" role="status"><ShieldAlert aria-hidden="true" size={18} /><span>Enabling or disabling {formatList(periodChanges)} changes enforcement immediately. Newly enabled periods include usage accumulated since their UTC boundary.</span></div>
          )}
          {alertOnlyPeriods.length > 0 && (
            <div className="safety-warning" role="status"><ShieldAlert aria-hidden="true" size={18} /><span>The {formatList(alertOnlyPeriods)} has no block threshold: it becomes alert-only and will never block this subject automatically.</span></div>
          )}
          {belowUsageFields.length > 0 && (
            <div className="safety-warning" role="status">
              <ShieldAlert aria-hidden="true" size={18} />
              <span>The new finite {formatList(belowUsageFields)} limit is below current-period usage. Additional use may be blocked immediately.</span>
            </div>
          )}
          {unlimitedFields.length > 0 && (
            <div className="safety-warning safety-warning-critical">
              <ShieldAlert aria-hidden="true" size={18} />
              <label>
                <input
                  checked={unlimitedConfirmed}
                  disabled={busy}
                  onChange={(event) => setUnlimitedConfirmed(event.target.checked)}
                  type="checkbox"
                />
                <span>I confirm the {formatList(unlimitedFields)} limit should change from a finite value to Unlimited.</span>
              </label>
            </div>
          )}
          <label className="reason-field">
            <span>Reason {reasonRequired && <strong aria-hidden="true">*</strong>}</span>
            <textarea
              aria-describedby="limits-reason-help"
              aria-required={reasonRequired}
              disabled={busy}
              onChange={(event) => {
                setReason(event.target.value);
                setError("");
              }}
              required={reasonRequired}
              rows={3}
              value={reason}
            />
          </label>
          <p className="field-help" id="limits-reason-help">
            {reasonRequired
              ? "Required for period enable/disable, Unlimited limits, alert-only thresholds, or finite limits below current-period usage. "
              : "Optional for this limit change. "}
            When provided, the trimmed reason is stored in the audit log.
          </p>
          {error && <ErrorMessage message={error} />}
          {apiError && <ErrorMessage message={apiError} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">
              Cancel
            </button>
            <button
              className="button button-primary"
              disabled={
                busy ||
                (unlimitedFields.length > 0 && !unlimitedConfirmed) ||
                (reasonRequired && !reason.trim())
              }
              type="submit"
            >
              {busy ? (
                <RefreshCw className="spin" aria-hidden="true" size={17} />
              ) : (
                <Check aria-hidden="true" size={17} />
              )}
              {busy ? "Saving" : "Save limits"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function StatusDialog({
  apiError,
  busy,
  enforcement,
  onClose,
  onConfirm,
  user,
}: {
  apiError: string;
  busy: boolean;
  enforcement: Summary["enforcement"];
  onClose: () => void;
  onConfirm: (reason: string) => void;
  user: UserRow;
}) {
  const [reason, setReason] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const nextStatus: UserStatus = user.status === "active" ? "blocked" : "active";
  const blocking = nextStatus === "blocked";
  const workload = isWorkload(user) ? user.workload ?? null : null;
  const subjectLabel = isWorkload(user) ? "workload" : "user";
  const recordOnly = isWorkload(user) && (!workload || !workload.enforcement_ready);
  useModalLifecycle(busy, onClose, dialogRef, reasonRef);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = reason.trim();
    if (!trimmed) return;
    onConfirm(trimmed);
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-describedby="status-enforcement-message"
        aria-labelledby="status-title"
        aria-modal="true"
        className="dialog status-dialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Access status</p>
            <h2 id="status-title">Confirm {blocking ? "block" : "unblock"}</h2>
          </div>
          <IconButton label="Close dialog" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" size={19} />
          </IconButton>
        </div>
        <div className="dialog-user">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <strong>{displayName(user)}</strong>
            <span>{user.user_id}</span>
          </div>
        </div>
        <form onSubmit={submit}>
          <div className="status-transition" aria-label={`Status changes from ${user.status} to ${nextStatus}`}>
            <div><span>Current status</span><strong>{user.status}</strong></div>
            <span aria-hidden="true">→</span>
            <div><span>Next status</span><strong>{nextStatus}</strong></div>
          </div>
          <dl className="usage-snapshot" aria-label="Current usage and limits">
            {QUOTA_PERIODS.map((period) => {
              const limits = user.limits[period];
              const usage = user.current_usage[period];
              return <div key={period}><dt>{periodLabel(period)}</dt><dd>{limits ? `${formatUsd(usage.cost_usd)} / ${formatLimit(limits.usd, (value) => formatUsd(value))} · ${formatCompact(usage.input_tokens)} / ${formatLimit(limits.input_tokens, formatCompact)} input · ${formatCompact(usage.output_tokens)} / ${formatLimit(limits.output_tokens, formatCompact)} output` : "Disabled"}</dd></div>;
            })}
          </dl>
          <div className={`enforcement-warning${blocking ? " enforcement-warning-destructive" : ""}`} id="status-enforcement-message">
            <ShieldAlert aria-hidden="true" size={18} />
            <div>
              <strong>{isWorkload(user) ? (recordOnly ? "Not enforced" : "IAM Deny on workload role") : `${formatOperationalLabel(enforcement.mode)} mode`}</strong>
              <p>{statusEnforcementMessage(enforcement, nextStatus, user)}</p>
            </div>
          </div>
          <label className="reason-field">
            <span>Reason <strong aria-hidden="true">*</strong></span>
            <textarea
              aria-describedby="status-reason-help"
              disabled={busy}
              onChange={(event) => setReason(event.target.value)}
              ref={reasonRef}
              required
              rows={3}
              value={reason}
            />
          </label>
          <p className="field-help" id="status-reason-help">Required. This trimmed reason is stored in the audit log.</p>
          {apiError && <ErrorMessage message={apiError} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">
              Cancel
            </button>
            <button
              className={`button ${blocking ? "button-danger" : "button-primary"}`}
              disabled={busy || !reason.trim()}
              type="submit"
            >
              {busy ? <RefreshCw className="spin" aria-hidden="true" size={17} /> : blocking ? <Lock aria-hidden="true" size={17} /> : <Unlock aria-hidden="true" size={17} />}
              {busy ? "Saving" : blocking ? (recordOnly ? "Record block" : `Block ${subjectLabel}`) : `Unblock ${subjectLabel}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function statusEnforcementMessage(
  enforcement: Summary["enforcement"],
  nextStatus: UserStatus,
  user?: Pick<AdminUser, "user_id" | "granularity" | "workload"> & Partial<Pick<AdminUser, "status" | "status_origin" | "status_reason">>,
): string {
  if (user && isWorkload(user)) {
    const workload = user.workload;
    if (workload && !workload.registered) {
      return nextStatus === "blocked"
        ? "This workload is not in the deployed roster, so no IAM role is known: the block is recorded and alerted but no traffic is stopped. Re-add it to workloads.json with a role_arn and redeploy to enforce."
        : "Unblocking clears the recorded status. The workload is not in the deployed roster, so nothing was being enforced.";
    }
    if (workload && !workload.enforcement_ready) {
      return nextStatus === "blocked"
        ? "This workload has no IAM role configured: the block is recorded and alerted but traffic is NOT stopped. Add role_arn to workloads.json and redeploy to enforce it."
        : "Unblocking clears the recorded status. This workload has no IAM role, so no Deny was in place.";
    }
    return nextStatus === "blocked"
      ? "Blocking attaches an inline IAM Deny for Bedrock invoke actions to the workload role. It takes effect within seconds and is re-applied automatically every five minutes; in-flight requests complete."
      : "Unblocking removes the inline IAM Deny from the workload role. All configured calendar limits continue to apply.";
  }
  if (nextStatus === "active") {
    const base = "Unblocking allows new credentials to be issued. All configured calendar limits continue to apply.";
    return user && user.status !== undefined && isAutomaticBlock(user as Pick<AdminUser, "status" | "status_origin" | "status_reason">)
      ? `${base} ${AUTOMATIC_BLOCK_HINT}`
      : base;
  }
  const window = formatDuration(enforcement.permission_lease_seconds);
  return `Blocking prevents new credentials from being issued and requests active-session revocation. Existing permissions expire with their lease (up to ${window} after detection); revocation usually cuts them earlier, so bounded overspend is limited to whichever ends first.`;
}

function formatLimit(limit: number, format: (value: number) => string): string {
  return limit === 0 ? "Unlimited" : format(limit);
}

function formatList(values: string[]): string {
  if (values.length < 2) return values[0] ?? "";
  if (values.length === 2) return `${values[0]} and ${values[1]}`;
  return `${values.slice(0, -1).join(", ")}, and ${values[values.length - 1]}`;
}

function Brand() {
  return (
    <div className="brand">
      <div className="brand-mark"><Layers3 aria-hidden="true" size={22} /></div>
      <div>
        <strong>Bedrock Spend Controls</strong>
        <span>Admin console</span>
      </div>
    </div>
  );
}

function SystemDetail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="system-detail">
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined} title={value}>{value}</dd>
    </div>
  );
}

function IconButton({
  children,
  danger = false,
  disabled = false,
  label,
  onClick,
}: {
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      className={`icon-button${danger ? " icon-button-danger" : ""}`}
      disabled={disabled}
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  );
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

function LoadingState({ label }: { label: string }) {
  return (
    <div className="loading-state">
      <RefreshCw className="spin" aria-hidden="true" size={24} />
      <span>{label}</span>
    </div>
  );
}

/** Mirrors the subject grid: one group per subject kind, four compact metric cards each. */
function SummarySkeleton() {
  return (
    <section className="subject-grid" aria-label="Loading quota summary">
      {(["users", "workloads"] as const).map((kind) => (
        <article className={`subject-group subject-group-${kind}`} key={kind}>
          <header className="subject-group-heading">
            <div className="subject-group-title">
              <span className="skeleton skeleton-square" />
              <div>
                <span className="skeleton skeleton-line" />
                <span className="skeleton skeleton-line" />
              </div>
            </div>
          </header>
          <div className="subject-metrics">
            {[0, 1, 2, 3].map((item) => (
              <div className="metric-card metric-card-compact skeleton-card" key={item}>
                <span className="skeleton skeleton-square" />
                <div>
                  <span className="skeleton skeleton-line" />
                  <span className="skeleton skeleton-value" />
                </div>
              </div>
            ))}
          </div>
        </article>
      ))}
    </section>
  );
}

function TableSkeleton() {
  return (
    <>
      {[0, 1, 2].map((row) => (
        <tr className="table-skeleton" key={row}>
          <td><span className="skeleton skeleton-row-wide" /></td>
          <td><span className="skeleton skeleton-row-small" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-small" /></td>
          <td />
        </tr>
      ))}
    </>
  );
}

function UnavailableState({ label }: { label: string }) {
  return (
    <section className="unavailable-state" aria-label={label}>
      <AlertCircle aria-hidden="true" size={22} />
      <span>{label}</span>
    </section>
  );
}

function EmptyState({ hasFilters }: { hasFilters: boolean }) {
  return (
    <div className="empty-state">
      <Users aria-hidden="true" size={24} />
      <strong>{hasFilters ? "No matching users" : "No users yet"}</strong>
      <span>{hasFilters ? "Try a different search or status." : "Users appear after they request access."}</span>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <main className="centered">{children}</main>;
}

function displayName(user: UserRow): string {
  return user.name || "Unnamed identity";
}

function initials(user: UserRow): string {
  const source = displayName(user).trim();
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length > 1) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function highestUtilization(user: UserRow): { period: QuotaPeriod; percent: number; level: string } | null {
  let highest: { period: QuotaPeriod; ratio: number } | null = null;
  for (const period of QUOTA_PERIODS) {
    const limits = user.limits[period];
    if (!limits) continue;
    const usage = user.current_usage[period];
    const ratios = [
      limits.usd > 0 ? usage.cost_usd / limits.usd : 0,
      limits.input_tokens > 0 ? usage.input_tokens / limits.input_tokens : 0,
      limits.output_tokens > 0 ? usage.output_tokens / limits.output_tokens : 0,
    ];
    const ratio = Math.max(...ratios);
    if (highest === null || ratio > highest.ratio) highest = { period, ratio };
  }
  if (highest === null) return null;
  return {
    period: highest.period,
    percent: Math.round(highest.ratio * 100),
    level: highest.ratio >= 1 ? "critical" : highest.ratio >= 0.8 ? "warning" : "normal",
  };
}

function periodLabel(period: QuotaPeriod): string {
  return period[0].toUpperCase() + period.slice(1);
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Invalid timestamp" : timestamp.toLocaleString();
}

