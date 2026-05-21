import { useMemo } from "react";
import { ArrowDown, ArrowRight, ArrowUp, ChevronRight } from "lucide-react";
import { Line, LineChart, ResponsiveContainer, Tooltip } from "recharts";

import { DataSourceBadge } from "@/components/labs/DataSourceBadge";
import { ValueStatusBadge } from "@/components/labs/ValueStatusBadge";
import { fmtNum, formatShortDate } from "@/lib/format";
import { Card, CardContent } from "@/components/ui-labs/card";
import type { LabResultCard, LabResultValue } from "@/federation/types";

interface Props {
  card: LabResultCard;
  onNavigate: (conceptCode: string) => void;
}

export function LabValueCard({ card, onNavigate }: Props) {
  if (card.values.length === 0) return null;

  const latest = card.values[0];
  const previous = card.values[1];
  const sparklineValues = card.values.slice(0, 6);

  const cardContent = (
    <Card className="transition-colors hover:bg-muted/30">
      <CardContent className="p-5">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold text-foreground">
              {card.concept_name}
            </h3>
            {card.vocabulary_id === "LOINC" && (
              <p className="truncate text-xs text-muted-foreground">
                LOINC {card.concept_code}
              </p>
            )}
          </div>
          <div className="mt-0.5 flex shrink-0 items-center gap-1.5">
            <TrendBadge latest={latest} previous={previous} />
            <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="min-w-0">
            <div className="font-mono text-xl font-semibold text-foreground">
              {formatValue(latest)}
              {latest.unit && (
                <span className="ml-1 text-sm font-normal text-muted-foreground">
                  {latest.unit}
                </span>
              )}
            </div>
            {latest.range_low != null || latest.range_high != null ? (
              <p className="mt-0.5 whitespace-nowrap text-xs text-muted-foreground">
                Normal: {formatRange(latest.range_low, latest.range_high, latest.unit ?? "")}
              </p>
            ) : (
              <p className="mt-0.5 whitespace-nowrap text-xs italic text-muted-foreground/70">
                No range
              </p>
            )}
          </div>

          {card.values.length > 1 && (
            <div className="ml-auto h-12 w-24 shrink-0 sm:w-32">
              <Sparkline values={sparklineValues} unit={latest.unit ?? ""} />
            </div>
          )}
        </div>

        <div className="mt-3 flex items-center gap-2 text-xs">
          <ValueStatusBadge status={latest.status} />
          <DataSourceBadge
            source={latest.source}
            labName={latest.lab_name}
            reportFilename={latest.report_filename}
            timeAgo={formatTimeAgo(latest.measured_at)}
          />
        </div>
      </CardContent>
    </Card>
  );

  return (
    <button
      type="button"
      onClick={() => onNavigate(card.concept_code)}
      className="block w-full cursor-pointer rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-healthkey-brand-700 focus-visible:ring-offset-2"
      aria-label={`Open ${card.concept_name} trend`}
    >
      {cardContent}
    </button>
  );
}

function formatRange(min: number | null, max: number | null, unit: string): string {
  if (min != null && max != null) return `${fmtNum(min)}–${fmtNum(max)} ${unit}`;
  if (max != null) return `< ${fmtNum(max)} ${unit}`;
  if (min != null) return `> ${fmtNum(min)} ${unit}`;
  return "";
}

function formatValue(r: LabResultValue): string {
  if (r.value != null) return String(Number(Number(r.value).toFixed(2)));
  return r.value_string || "—";
}

function TrendBadge({
  latest,
  previous,
}: {
  latest: LabResultValue;
  previous?: LabResultValue;
}) {
  if (!previous || latest.value == null || previous.value == null) return null;

  const latestVal = Number(latest.value);
  const prevVal = Number(previous.value);
  const delta = latestVal - prevVal;
  const pctChange = Math.abs(delta) / (Math.abs(prevVal) || 1);
  const direction: "up" | "down" | "flat" =
    pctChange < 0.02 ? "flat" : delta > 0 ? "up" : "down";

  let tone: "improving" | "worsening" | "stable" = "stable";
  if (latest.range_low != null && latest.range_high != null) {
    const rangeLow = Number(latest.range_low);
    const rangeHigh = Number(latest.range_high);
    const distanceFromRange = (v: number) => {
      if (v < rangeLow) return rangeLow - v;
      if (v > rangeHigh) return v - rangeHigh;
      return 0;
    };
    const distNow = distanceFromRange(latestVal);
    const distPrev = distanceFromRange(prevVal);
    const EPS = 1e-4;
    if (distNow < distPrev - EPS) tone = "improving";
    else if (distNow > distPrev + EPS) tone = "worsening";
  }

  const styles: Record<typeof tone, string> = {
    improving: "bg-success-50 text-success-700 border-success-200",
    worsening: "bg-warning-50 text-warning-700 border-warning-200",
    stable: "bg-muted text-muted-foreground border-border",
  };

  const Icon =
    direction === "up" ? ArrowUp : direction === "down" ? ArrowDown : ArrowRight;

  const absRounded =
    Math.abs(delta) >= 10
      ? Math.round(Math.abs(delta)).toString()
      : (Math.round(Math.abs(delta) * 10) / 10).toString();
  const formattedDelta =
    direction === "flat" ? "±0" : `${delta > 0 ? "+" : "−"}${absRounded}`;

  const toneLabel =
    tone === "improving"
      ? "Moved toward normal range"
      : tone === "worsening"
        ? "Moved away from normal range"
        : latest.range_low == null
          ? "Change from previous reading"
          : "Still within normal range";

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${styles[tone]}`}
      title={`${formattedDelta} ${latest.unit ?? ""} — ${toneLabel}`}
      aria-label={`Trend: ${tone}, ${formattedDelta} ${latest.unit ?? ""}`}
    >
      <Icon className="h-3 w-3" strokeWidth={2.5} aria-hidden="true" />
      <span className="font-mono">{formattedDelta}</span>
    </span>
  );
}

type SparkPoint = { value: number; measured_at: string };

function Sparkline({ values, unit }: { values: LabResultValue[]; unit: string }) {
  const chartData = useMemo<SparkPoint[]>(
    () =>
      values
        .filter((v): v is LabResultValue & { value: number } => v.value != null)
        .reverse()
        .map((v) => ({ value: Number(v.value), measured_at: v.measured_at })),
    [values],
  );
  if (chartData.length < 2) return null;

  return (
    <div
      className="h-full w-full text-healthkey-brand-700"
      role="img"
      aria-label={`Recent ${unit} trend`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={chartData} margin={{ top: 6, right: 6, bottom: 6, left: 6 }}>
          <Tooltip
            cursor={false}
            allowEscapeViewBox={{ x: true, y: true }}
            wrapperStyle={{ outline: "none", zIndex: 10 }}
            content={<SparkTooltip unit={unit} />}
          />
          <Line
            type="natural"
            dataKey="value"
            stroke="currentColor"
            strokeWidth={1.75}
            strokeLinecap="round"
            strokeLinejoin="round"
            isAnimationActive={false}
            dot={{
              r: 2.5,
              fill: "currentColor",
              stroke: "hsl(var(--background))",
              strokeWidth: 1.5,
            }}
            activeDot={{
              r: 3.5,
              fill: "currentColor",
              stroke: "hsl(var(--background))",
              strokeWidth: 1.5,
            }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function SparkTooltip({
  active,
  payload,
  unit,
}: {
  active?: boolean;
  payload?: Array<{ payload: SparkPoint }>;
  unit: string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const { value, measured_at } = payload[0].payload;
  return (
    <div className="pointer-events-none whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-md">
      <div className="font-mono font-semibold text-foreground">
        {String(Number(value.toFixed(2)))}{" "}
        <span className="text-muted-foreground">{unit}</span>
      </div>
      <div className="text-muted-foreground">{formatShortDate(measured_at)}</div>
    </div>
  );
}

function formatTimeAgo(iso: string | null): string | undefined {
  if (!iso) return undefined;
  const then = new Date(iso);
  const now = new Date();
  const days = Math.floor((now.getTime() - then.getTime()) / (1000 * 60 * 60 * 24));
  if (days < 1) return "today";
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return then.toISOString().slice(0, 10);
}

function CardSkeleton() {
  return (
    <Card>
      <CardContent className="p-5">
        <div className="h-4 w-24 animate-pulse rounded bg-muted" />
        <div className="mt-3 h-8 w-16 animate-pulse rounded bg-muted" />
        <div className="mt-2 h-3 w-32 animate-pulse rounded bg-muted" />
      </CardContent>
    </Card>
  );
}

export { CardSkeleton };
