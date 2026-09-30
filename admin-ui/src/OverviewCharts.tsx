import { useEffect, useState } from "react";
import { BarChart3, Users } from "lucide-react";
import type { AdminConfig } from "./config";
import type { Session } from "./auth";
import {
  api,
  apiErrorMessage,
  type UsageMetricKey,
  type UsageMetrics,
  type UsageTopUser,
} from "./api";
import { formatNumber, formatUsd } from "./format";

/** The broker labels every top spender with its subject kind. */
export function isWorkloadSpender(entry: UsageTopUser): boolean {
  return entry.granularity === "workload";
}

const METRIC_OPTIONS: Array<{ key: UsageMetricKey; label: string }> = [
  { key: "cost_usd", label: "Spend (USD)" },
  { key: "requests", label: "Requests" },
  { key: "input_tokens", label: "Input tokens" },
  { key: "output_tokens", label: "Output tokens" },
];

const RANGE_OPTIONS = [7, 14, 30] as const;

// Deterministic per-model palette; falls back to gray beyond ten models.
const MODEL_COLORS = [
  "#0972d3", "#e07941", "#037f0c", "#7d2ff1", "#d13212",
  "#0891b2", "#b3599b", "#946800", "#5f6b7a", "#3e9b56",
];

export function modelColor(index: number): string {
  return MODEL_COLORS[index] ?? "#8d99a8";
}

export function formatMetricValue(metric: UsageMetricKey, value: number): string {
  if (metric === "cost_usd") return formatUsd(value);
  return formatNumber(Math.round(value));
}

function dayLabel(day: string): string {
  return day.slice(5);
}

const CHART_WIDTH = 720;
const CHART_HEIGHT = 200;
const BAR_AREA_TOP = 12;
const BAR_AREA_BOTTOM = 176;
const LABEL_Y = 194;

function UsageChart({ data, metric }: { data: UsageMetrics; metric: UsageMetricKey }) {
  const days = data.days;
  const dayTotals = days.map((_, index) =>
    data.models.reduce((sum, model) => sum + (model.series[metric][index] ?? 0), 0),
  );
  const max = Math.max(...dayTotals, 0);
  if (max <= 0) {
    return <p className="usage-chart-empty">No Bedrock usage in the selected range.</p>;
  }
  const slot = CHART_WIDTH / days.length;
  const barWidth = Math.max(4, Math.min(48, slot * 0.62));
  const scale = (BAR_AREA_BOTTOM - BAR_AREA_TOP) / max;
  const labelStep = days.length > 10 ? Math.ceil(days.length / 6) : 1;
  const metricLabel = METRIC_OPTIONS.find((option) => option.key === metric)?.label ?? metric;
  return (
    <svg
      aria-label={`Daily ${metricLabel} by model`}
      className="usage-chart-svg"
      preserveAspectRatio="xMidYMid meet"
      role="img"
      viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
    >
      <text className="usage-chart-max" x="2" y="10">{formatMetricValue(metric, max)}</text>
      <line className="usage-chart-axis" x1="0" x2={CHART_WIDTH} y1={BAR_AREA_BOTTOM} y2={BAR_AREA_BOTTOM} />
      {days.map((day, index) => {
        const x = index * slot + (slot - barWidth) / 2;
        let cursor = BAR_AREA_BOTTOM;
        const segments = data.models.map((model, modelIndex) => {
          const value = model.series[metric][index] ?? 0;
          if (value <= 0) return null;
          const height = Math.max(1, value * scale);
          cursor -= height;
          return (
            <rect
              fill={modelColor(modelIndex)}
              height={height}
              key={model.model}
              rx="1"
              width={barWidth}
              x={x}
              y={cursor}
            >
              <title>{`${day} · ${model.model}: ${formatMetricValue(metric, value)}`}</title>
            </rect>
          );
        });
        return (
          <g key={day}>
            {segments}
            {index % labelStep === 0 && (
              <text className="usage-chart-label" textAnchor="middle" x={index * slot + slot / 2} y={LABEL_Y}>
                {dayLabel(day)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function OverviewCharts({
  cfg,
  refreshKey,
  session,
  users,
}: {
  cfg: AdminConfig;
  refreshKey?: string;
  session: Session;
  users: Array<{ user_id: string; name: string }>;
}) {
  const [days, setDays] = useState<number>(14);
  const [metric, setMetric] = useState<UsageMetricKey>("cost_usd");
  const [data, setData] = useState<UsageMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.usageMetrics(cfg, session, days)
      .then((payload) => {
        if (cancelled) return;
        setData(payload);
        setError("");
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(apiErrorMessage(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [cfg, session, days, refreshKey]);

  // Prefer the server-resolved name (works even for identities beyond the
  // loaded users page); fall back to the local page, then to the raw key.
  const userName = (user: { user_id: string; name?: string }): string =>
    user.name
    || users.find((candidate) => candidate.user_id === user.user_id)?.name
    || user.user_id;
  const maxUserCost = Math.max(...(data?.top_users.map((user) => user.cost_usd) ?? [0]), 0);

  return (
    <section aria-labelledby="usage-charts-title" className="usage-panel">
      <div className="panel-heading usage-heading">
        <div>
          <h2 id="usage-charts-title"><BarChart3 aria-hidden="true" size={18} /> Bedrock usage</h2>
          <p>Per-model activity from CloudWatch metrics; quota accounting stays on the daily ledger.</p>
        </div>
        <div className="usage-controls">
          <select aria-label="Usage metric" onChange={(event) => setMetric(event.target.value as UsageMetricKey)} value={metric}>
            {METRIC_OPTIONS.map((option) => (
              <option key={option.key} value={option.key}>{option.label}</option>
            ))}
          </select>
          <select aria-label="Usage range" onChange={(event) => setDays(Number(event.target.value))} value={days}>
            {RANGE_OPTIONS.map((range) => (
              <option key={range} value={range}>Last {range} days</option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <div className="message message-error" role="alert"><span>{error}</span></div>
      )}
      {!error && loading && !data && <p className="usage-chart-empty">Loading usage metrics…</p>}

      {data && (
        <>
          {data.status === "unavailable" && (
            <p className="usage-chart-empty">
              CloudWatch metrics are unavailable{data.error_code ? ` (${data.error_code})` : ""}; usage charts will return when telemetry recovers.
            </p>
          )}
          {data.status === "partial" && (
            <span className="ops-status ops-status-amber usage-status"><span aria-hidden="true" />Partial CloudWatch data</span>
          )}
          {data.status !== "unavailable" && (
            <div className="usage-layout">
              <div className="usage-chart-card">
                <UsageChart data={data} metric={metric} />
                {data.models.length > 0 && (
                  <ul aria-label="Model legend" className="usage-legend">
                    {data.models.map((model, index) => (
                      <li key={model.model}>
                        <span aria-hidden="true" className="usage-swatch" style={{ backgroundColor: modelColor(index) }} />
                        <span className="usage-legend-name">{model.model}</span>
                        <span className="usage-legend-value">{formatMetricValue(metric, model.totals[metric])}</span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="usage-totals">
                  Range totals: {formatMetricValue("cost_usd", data.totals.cost_usd)} · {formatMetricValue("requests", data.totals.requests)} requests · {formatMetricValue("input_tokens", data.totals.input_tokens)} in / {formatMetricValue("output_tokens", data.totals.output_tokens)} out tokens
                </p>
              </div>

              <aside aria-label="Top spenders" className="usage-top-users">
                <h3><Users aria-hidden="true" size={15} /> Top spenders</h3>
                {data.top_users.length === 0 && <p className="usage-chart-empty">No activity in range.</p>}
                <ul>
                  {data.top_users.map((user) => (
                    <li key={user.user_id}>
                      <div className="usage-user-row">
                        <span className="usage-user-name" title={user.user_id}>
                          {isWorkloadSpender(user) && <span className="status-badge status-workload usage-user-kind" title="Workload: app on its own IAM role, attributed by inference profile">workload</span>}
                          {userName(user)}
                        </span>
                        <span className="usage-user-cost">{formatMetricValue("cost_usd", user.cost_usd)} · {formatNumber(user.requests)} req</span>
                      </div>
                      <div className="progress-track usage-user-track">
                        <div
                          className="progress-fill progress-normal"
                          style={{ width: `${maxUserCost > 0 ? Math.max(2, (user.cost_usd / maxUserCost) * 100) : 2}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              </aside>
            </div>
          )}
        </>
      )}
    </section>
  );
}
