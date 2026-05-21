import { fmtNum, formatShortDate } from "@/lib/format";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Dot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { LabResultValue, LabValueStatus } from "@/federation/types";

interface Props {
  values: LabResultValue[];
  unit: string;
}

interface ChartPoint {
  date: string;
  label: string;
  value: number;
  status: LabValueStatus;
  unit: string;
}

export function LabTrendChart({ values, unit }: Props) {
  const numeric = values.filter((r) => r.value != null) as Array<LabResultValue & { value: number }>;
  if (numeric.length === 0) {
    return (
      <EmptyState
        title="No data yet"
        body="We'll show a trend here once you add some lab values for this test."
      />
    );
  }

  const chartData: ChartPoint[] = [...numeric]
    .reverse()
    .map((r) => ({
      date: r.measured_at,
      label: formatShortDate(r.measured_at),
      value: Number(r.value),
      status: r.status,
      unit: r.unit ?? unit,
    }));

  const latest = numeric[0];
  const refMin = latest.range_low != null ? Number(latest.range_low) : null;
  const refMax = latest.range_high != null ? Number(latest.range_high) : null;

  const vals = chartData.map((p) => p.value);
  let yMin = Math.min(...vals, refMin ?? Infinity);
  let yMax = Math.max(...vals, refMax ?? -Infinity);
  const pad = (yMax - yMin) * 0.15 || yMax * 0.1 || 1;
  yMin = Math.max(0, yMin - pad);
  yMax = yMax + pad;

  if (chartData.length === 1) {
    return (
      <div>
        <SinglePoint point={chartData[0]} refMin={refMin} refMax={refMax} />
        <p className="mt-3 text-xs text-muted-foreground">
          Not enough data for a trend yet — add more measurements to see how this changes over time.
        </p>
      </div>
    );
  }

  return (
    <div className="w-full">
      {(refMin != null || refMax != null) && (
        <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
          <svg width="16" height="8" aria-hidden="true" className="shrink-0">
            <line
              x1="0" y1="4" x2="16" y2="4"
              stroke="currentColor" strokeWidth="1.5" strokeDasharray="4 3"
            />
          </svg>
          <span>
            Normal range:{" "}
            <span className="font-mono text-foreground">
              {refMin != null && refMax != null
                ? `${fmtNum(refMin)}–${fmtNum(refMax)}`
                : refMax != null
                  ? `< ${fmtNum(refMax)}`
                  : `> ${fmtNum(refMin!)}`}{" "}
              {unit}
            </span>
          </span>
        </div>
      )}

      <div className="h-60 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={chartData} margin={{ top: 12, right: 12, left: -24, bottom: 8 }}>
            <defs>
              <linearGradient id="labTrendFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-brand-700)" stopOpacity={0.25} />
                <stop offset="100%" stopColor="var(--color-brand-700)" stopOpacity={0.02} />
              </linearGradient>
            </defs>

            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
            <XAxis
              dataKey="label"
              stroke="var(--color-muted-foreground)"
              fontSize={11}
              tickMargin={6}
              padding={{ left: 8, right: 8 }}
            />
            <YAxis
              domain={[yMin, yMax]}
              stroke="var(--color-muted-foreground)"
              fontSize={11}
              width={40}
              tickMargin={2}
            />

            {refMin != null && (
              <ReferenceLine
                y={refMin}
                stroke="var(--color-muted-foreground)"
                strokeDasharray="4 3"
                strokeWidth={1.5}
                ifOverflow="extendDomain"
              />
            )}
            {refMax != null && (
              <ReferenceLine
                y={refMax}
                stroke="var(--color-muted-foreground)"
                strokeDasharray="4 3"
                strokeWidth={1.5}
                ifOverflow="extendDomain"
              />
            )}

            <Tooltip
              cursor={{ stroke: "var(--color-muted-foreground)", strokeWidth: 1, strokeDasharray: "3 3" }}
              contentStyle={{
                backgroundColor: "var(--color-card)",
                border: "1px solid var(--color-border)",
                borderRadius: 8,
                fontSize: 12,
                padding: "6px 10px",
              }}
              labelStyle={{ color: "var(--color-foreground)", fontWeight: 600, marginBottom: 2 }}
              itemStyle={{ color: "var(--color-foreground)", padding: 0 }}
              labelFormatter={(_label, payload) => {
                const first = Array.isArray(payload) && payload.length > 0 ? payload[0] : null;
                const iso = (first?.payload as ChartPoint | undefined)?.date ?? (_label as string);
                if (!iso) return "";
                const d = new Date(iso);
                if (Number.isNaN(d.getTime())) return String(_label);
                return d.toLocaleDateString(undefined, {
                  year: "numeric", month: "long", day: "numeric", timeZone: "UTC",
                });
              }}
              formatter={(value, _name, item) => {
                const u = (item as unknown as { payload?: ChartPoint }).payload?.unit ?? "";
                const display = typeof value === "number" ? fmtNum(value) : value;
                return [`${display} ${u}`, "Value"];
              }}
            />

            <Area
              type="monotone"
              dataKey="value"
              stroke="var(--color-brand-700)"
              strokeWidth={2}
              fill="url(#labTrendFill)"
              fillOpacity={1}
              dot={(dotProps) => {
                const { cx, cy, payload, key } = dotProps as {
                  cx: number; cy: number; payload: ChartPoint; key?: string;
                };
                const color =
                  payload.status === "in_range"
                    ? "var(--color-success-700)"
                    : payload.status === "unknown"
                      ? "var(--color-muted-foreground)"
                      : "var(--color-warning-700)";
                return (
                  <Dot key={key} cx={cx} cy={cy} r={5} fill={color} stroke="var(--color-background)" strokeWidth={2} />
                );
              }}
              activeDot={{ r: 7, stroke: "var(--color-background)", strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function SinglePoint({
  point,
  refMin,
  refMax,
}: {
  point: ChartPoint;
  refMin: number | null;
  refMax: number | null;
}) {
  return (
    <div className="rounded-md border border-border bg-card p-6">
      <p className="text-xs text-muted-foreground">{point.label}</p>
      <p className="mt-1 font-mono text-3xl font-semibold text-foreground">
        {fmtNum(point.value)} <span className="text-base text-muted-foreground">{point.unit}</span>
      </p>
      {(refMin != null || refMax != null) && (
        <p className="mt-1 text-xs text-muted-foreground">
          Normal range:{" "}
          {refMin != null && refMax != null
            ? `${fmtNum(refMin)}–${fmtNum(refMax)}`
            : refMax != null
              ? `< ${fmtNum(refMax)}`
              : `> ${fmtNum(refMin!)}`}{" "}
          {point.unit}
        </p>
      )}
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-card p-8 text-center">
      <p className="text-base font-semibold text-foreground">{title}</p>
      <p className="mt-1 text-sm text-muted-foreground">{body}</p>
    </div>
  );
}
