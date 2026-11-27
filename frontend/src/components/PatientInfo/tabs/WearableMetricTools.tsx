import { useContext, useEffect, useId, useRef, useState, type ComponentProps } from 'react';
import { ChartLine, Database, ListTree } from 'lucide-react';
import {
  CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import api from '@/api/axios';
import ClinicalField from '../ClinicalField';
import type { OmopFocus } from '@/components/Patient/omopRows';
import { MetricContext } from './wearableMetricContext';
import metricKeys from './wearableMetricKeys.json';
import { wearableMetricMappingsUrl } from '@/components/CodeMappings/mappingLinks';
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui-labs/dialog';

/**
 * What sits to the right of each 30-day aggregate on the Wearables tab (#2887):
 * a link to the OMOP rows it was computed from, and a chart of its daily values
 * over time with the 30-day window marked.
 */

interface MetricDay {
  date: string;
  value: number;
  rows: number;
  status: 'accepted' | 'provisional';
  /** Provisional rows beside accepted ones on this day: not part of `value`. */
  provisional_rows_excluded: number;
  in_window: boolean;
  /** Whether the stored aggregate counts this day (in the window, recent enough, and accepted where it needs to be). */
  counted: boolean;
}

interface MetricGroup extends OmopFocus {
  rows: number;
  concept_name?: string | null;
  concept_code?: string | null;
  vocabulary_id?: string | null;
}

interface MetricSeries {
  field: string;
  label: string;
  statistic: 'sum' | 'mean' | 'min';
  aggregate: string;
  window: { start: string; end: string } | null;
  days: MetricDay[];
  /** The derivation reads no reading older than this day, when any wearable reading is newer. */
  recency_cutoff: string | null;
  truncated: boolean;
  /** Null for a reader who does not see the OMOP tab. */
  groups: MetricGroup[] | null;
}


const STATUS_COLOR = { accepted: '#0f766e', provisional: '#b45309' } as const;

const STATUS_LABEL = { accepted: 'Accepted', provisional: 'Provisional' } as const;

function groupName(group: MetricGroup) {
  return group.concept_id === 0
    ? (group.source_value || '(no source value)')
    : (group.concept_name || `Concept ${group.concept_id}`);
}

function useMetricSeries(personId: number | undefined, field: string, enabled: boolean) {
  const [series, setSeries] = useState<MetricSeries | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'error'>('idle');
  useEffect(() => {
    if (!enabled || personId == null) return undefined;
    let cancelled = false;
    (async () => {
      setState('loading');
      try {
        const res = await api.get(`/v1/patient-records/${personId}/wearable-metric/`, { params: { field } });
        if (!cancelled) {
          setSeries(res.data);
          setState('idle');
        }
      } catch {
        if (!cancelled) setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabled, personId, field]);
  return { series, state };
}

const DAY_MS = 86_400_000;
const toTime = (iso: string) => new Date(`${iso}T00:00:00Z`).getTime();

function dayLabel(iso: string) {
  const parsed = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(parsed.getTime())
    ? iso
    : parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function Chart({ series }: { series: MetricSeries }) {
  const { days, window } = series;
  // A time axis, so a gap in syncing is a gap on the chart and the 30-day band is 30 days wide.
  const points = days.map((d) => ({ ...d, time: toTime(d.date) }));
  const uncounted = days.filter((d) => d.in_window && !d.counted).length;
  const low = Math.min(points[0].time, window ? toTime(window.start) : Infinity);
  const high = Math.max(points[points.length - 1].time, window ? toTime(window.end) : -Infinity);
  return (
    <div>
      {/* A chart is a picture of numbers; the same numbers as a table are what a
          screen reader (and a test) can read. */}
      <table className="sr-only">
        <caption>{series.label} by day</caption>
        <thead><tr><th>Date</th><th>Value</th><th>Rows</th><th>Status</th><th>In the aggregate</th></tr></thead>
        <tbody>
          {days.map((d) => (
            <tr key={d.date}>
              <td>{d.date}</td><td>{d.value}</td><td>{d.rows}</td><td>{STATUS_LABEL[d.status]}</td>
              <td>{d.counted ? 'Counted' : 'Not counted'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="h-64 w-full" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 8, right: 12, bottom: 4, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="time"
              type="number"
              scale="time"
              domain={[low - DAY_MS, high + DAY_MS]}
              tickFormatter={(time) => dayLabel(new Date(Number(time)).toISOString().slice(0, 10))}
              tick={{ fontSize: 11 }}
              minTickGap={24}
            />
            <YAxis tick={{ fontSize: 11 }} width={48} domain={['auto', 'auto']} />
            {window && (
              <ReferenceArea
                x1={toTime(window.start)}
                x2={toTime(window.end)}
                fill="#0f766e"
                fillOpacity={0.08}
                ifOverflow="extendDomain"
              />
            )}
            <Tooltip
              labelFormatter={(time) => dayLabel(new Date(Number(time)).toISOString().slice(0, 10))}
              formatter={(value) => [String(value), series.label]}
            />
            <Line
              type="linear"
              dataKey="value"
              stroke="#64748b"
              strokeWidth={1.5}
              isAnimationActive={false}
              dot={(props: { cx?: number; cy?: number; payload?: MetricDay; index?: number }) => {
                const { cx, cy, payload, index } = props;
                if (cx == null || cy == null || !payload) return <g key={index} />;
                const color = STATUS_COLOR[payload.status];
                // A day in the window the aggregate does not count is drawn hollow.
                const hollow = payload.in_window && !payload.counted;
                return (
                  <circle
                    key={payload.date}
                    cx={cx}
                    cy={cy}
                    r={3.5}
                    fill={hollow ? '#fff' : color}
                    stroke={color}
                    strokeWidth={hollow ? 1.5 : 1}
                  />
                );
              }}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS_COLOR.accepted }} />
          Accepted
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: STATUS_COLOR.provisional }} />
          Provisional (no accepted reading that day)
        </span>
        {uncounted > 0 && (
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-full border-2 border-slate-500 bg-white" />
            In the window but not counted in the aggregate ({uncounted})
          </span>
        )}
        {window && (
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-2.5 w-4 rounded-sm bg-teal-700/10" />
            The 30 days the aggregate covers: {dayLabel(window.start)} to {dayLabel(window.end)}
          </span>
        )}
      </div>
      {uncounted > 0 && (
        <p role="note" className="mt-1 text-xs text-muted-foreground">
          {series.recency_cutoff && `Readings before ${dayLabel(series.recency_cutoff)} are not read while newer ones exist. `}
          A day that has only provisional readings is not in the value while accepted readings are.
        </p>
      )}
    </div>
  );
}

function ChartDialog({
  open, onOpenChange, label, personId, field, onOpenOmopRows,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  label: string;
  personId: number | undefined;
  field: string;
  onOpenOmopRows?: (focus: OmopFocus) => void;
}) {
  const { series, state } = useMetricSeries(personId, field, open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[44rem]">
        <DialogHeader>
          <DialogTitle>{label}</DialogTitle>
          <DialogDescription>
            {series?.aggregate ?? 'Daily values over time'}
          </DialogDescription>
        </DialogHeader>
        {state === 'loading' && !series && (
          <p role="status" className="text-sm text-muted-foreground">Loading the daily values…</p>
        )}
        {state === 'error' && (
          <p role="alert" className="text-sm text-red-700">The daily values could not be loaded.</p>
        )}
        {series && series.days.length === 0 && (
          <p className="text-sm text-muted-foreground">No readings are stored for this metric yet.</p>
        )}
        {series && series.days.length > 0 && <Chart series={series} />}
        {series?.truncated && (
          <p role="note" className="text-xs text-muted-foreground">Only the 365 most recent days with readings are shown.</p>
        )}
        {series?.groups && series.groups.length > 0 && onOpenOmopRows && (
          <div>
            <h4 className="mb-1 text-xs font-semibold uppercase text-slate-500">OMOP rows behind it</h4>
            <ul className="divide-y divide-border rounded-md border border-border text-sm">
              {series.groups.map((group) => (
                <li key={`${group.table}-${group.concept_id}-${group.source_value}`}>
                  <button
                    type="button"
                    onClick={() => {
                      onOpenChange(false);
                      onOpenOmopRows(group);
                    }}
                    className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-muted/40"
                  >
                    <span>
                      {groupName(group)}
                      <span className="ml-2 text-xs text-muted-foreground">{group.table}</span>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {group.rows} {group.rows === 1 ? 'row' : 'rows'} counted
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** The buttons beside a 30-day value. */
function MetricTools({ field, label, hasValue }: { field: string; label: string; hasValue: boolean }) {
  const { personId, onOpenOmopRows, canOpenMappings } = useContext(MetricContext);
  const metricKey = (metricKeys as Record<string, string>)[field];
  const [chartOpen, setChartOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const [failed, setFailed] = useState(false);
  const noteId = useId();
  // A lookup that answers after the reader has left the tab must not move them.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  if (!hasValue || personId == null) return null;

  // The rows are found when asked: a page of eighteen metrics reads none until
  // one is clicked, and a metric's groups are what the OMOP tab opens.
  const openRows = async () => {
    if (!onOpenOmopRows) return;
    setOpening(true);
    setFailed(false);
    try {
      const res = await api.get(`/v1/patient-records/${personId}/wearable-metric/`, { params: { field } });
      if (!mounted.current) return;
      const groups: MetricGroup[] = res.data?.groups ?? [];
      if (groups.length === 0) {
        setFailed(true);
      } else if (groups.length === 1) {
        onOpenOmopRows(groups[0]);
      } else {
        // Several groups hold rows of one metric: the chart's list names each.
        setChartOpen(true);
      }
    } catch {
      if (mounted.current) setFailed(true);
    } finally {
      if (mounted.current) setOpening(false);
    }
  };

  return (
    <div className="flex shrink-0 items-center gap-1 pt-7">
      {onOpenOmopRows && (
        <button
          type="button"
          onClick={openRows}
          disabled={opening}
          aria-label={`Show the OMOP rows behind ${label}`}
          aria-describedby={failed ? noteId : undefined}
          title="Show the OMOP rows this value was computed from"
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 disabled:opacity-50"
        >
          <Database aria-hidden="true" size={16} />
        </button>
      )}
      {canOpenMappings && metricKey && (
        <a
          href={wearableMetricMappingsUrl(metricKey)}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open the code mappings for ${label}`}
          title="Open the code mappings for this metric's source codes (opens in a new tab)"
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1"
        >
          <ListTree aria-hidden="true" size={16} />
        </a>
      )}
      <button
        type="button"
        onClick={() => setChartOpen(true)}
        aria-label={`Chart ${label} over time`}
        title="Chart this value over time"
        className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1"
      >
        <ChartLine aria-hidden="true" size={16} />
      </button>
      {failed && (
        <span id={noteId} role="status" className="ml-1 whitespace-nowrap text-[11px] text-red-700">
          No OMOP rows found.
        </span>
      )}
      {chartOpen && (
        <ChartDialog
          open={chartOpen}
          onOpenChange={setChartOpen}
          label={label}
          personId={personId}
          field={field}
          onOpenOmopRows={onOpenOmopRows}
        />
      )}
    </div>
  );
}

type ClinicalFieldProps = ComponentProps<typeof ClinicalField>;

/** A 30-day field with its tools to the right; props are ClinicalField's. */
export function MetricField(props: ClinicalFieldProps) {
  const { value } = props;
  const hasValue = value !== undefined && value !== null && value !== '';
  return (
    <div className="flex items-start gap-1">
      <div className="min-w-0 flex-1">
        <ClinicalField {...props} />
      </div>
      <MetricTools field={props.name} label={props.label} hasValue={hasValue} />
    </div>
  );
}
