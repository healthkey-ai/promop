import { useEffect, useState } from "react";
import api from "@/api/axios";
import { DASH, DATE_ONLY_TITLE, formatMoment, type Reading } from "./omopRows";

interface ReadingsPage {
  rows: Reading[];
  next_cursor: string | null;
}

const BADGE = "inline-flex whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium";
// The colours the Source Codes tab uses for the same three kinds of row.
const STATUS: Record<Reading["status"], { label: string; className: string; title: string }> = {
  accepted: {
    label: "Accepted",
    className: `${BADGE} bg-green-100 text-green-800`,
    title: "Stored at an approved mapping's concept.",
  },
  provisional: {
    label: "Provisional",
    className: `${BADGE} bg-amber-100 text-amber-800`,
    title: "Given a concept by a proposed mapping. Accepted once the mapping is approved.",
  },
  unresolved: {
    label: "Unresolved",
    className: `${BADGE} bg-slate-100 text-slate-600`,
    title: "Stored and waiting: no mapping gives this row a concept yet.",
  },
};

export function Moment({ datetime, date }: { datetime?: string | null; date?: string | null }) {
  const { text, dateOnly } = formatMoment(datetime, date);
  return (
    <span className="whitespace-nowrap" title={dateOnly ? DATE_ONLY_TITLE : undefined}>
      {text}
      {dateOnly && <span className="ml-1 text-[10px] text-muted-foreground">date only</span>}
    </span>
  );
}

function value(reading: Reading): string {
  if (reading.value_as_number != null) return String(reading.value_as_number);
  return reading.value_as_string || DASH;
}

const errorText = (err: unknown): string => {
  const data = err && typeof err === "object" && "response" in err
    ? (err as { response?: { data?: { detail?: string; error?: string } } }).response?.data
    : undefined;
  return data?.detail || data?.error || "Failed to load these readings.";
};

/**
 * The readings of one group, newest first, a page at a time: the first page
 * when this mounts (a closed group renders nothing, so nothing is read for
 * it), then "Show more". ``showSource`` adds the source value, for a concept
 * whose rows came under several codes.
 */
export default function ReadingsTable({
  url,
  params,
  label,
  showSource = false,
}: {
  url: string;
  params: Record<string, string | number>;
  /** The table's accessible name. */
  label: string;
  showSource?: boolean;
}) {
  const [rows, setRows] = useState<Reading[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Params arrive as a new object each render; the key is what changed.
  const paramsKey = JSON.stringify(params);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const res = await api.get<ReadingsPage>(url, { params: JSON.parse(paramsKey) });
        if (!cancelled) {
          setRows(res.data.rows);
          setNext(res.data.next_cursor);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(errorText(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [url, paramsKey]);

  const showMore = async () => {
    if (!next) return;
    setLoading(true);
    try {
      const res = await api.get<ReadingsPage>(url, { params: { ...params, cursor: next } });
      setRows((current) => [...current, ...res.data.rows]);
      setNext(res.data.next_cursor);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div data-testid="readings" className="space-y-2">
      {error && (
        <p role="alert" className="text-xs text-red-700">{error}</p>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="min-w-full border-collapse text-left text-xs" aria-label={label}>
            <thead className="bg-slate-50 text-[11px] uppercase text-slate-500">
              <tr>
                <th scope="col" className="border-b border-border px-3 py-1.5 font-semibold">Date / time</th>
                <th scope="col" className="border-b border-border px-3 py-1.5 text-right font-semibold">Value</th>
                <th scope="col" className="border-b border-border px-3 py-1.5 font-semibold">Unit</th>
                {showSource && (
                  <th scope="col" className="border-b border-border px-3 py-1.5 font-semibold">Source value</th>
                )}
                <th scope="col" className="border-b border-border px-3 py-1.5 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((reading) => {
                const status = STATUS[reading.status] ?? STATUS.unresolved;
                return (
                  <tr key={reading.id} className="bg-background align-top" data-testid="reading">
                    <td className="px-3 py-1.5 font-mono"><Moment datetime={reading.datetime} date={reading.date} /></td>
                    <td className="px-3 py-1.5 text-right font-mono tabular-nums">{value(reading)}</td>
                    <td className="px-3 py-1.5 font-mono">{reading.unit || DASH}</td>
                    {showSource && <td className="px-3 py-1.5 font-mono">{reading.source_value || DASH}</td>}
                    <td className="px-3 py-1.5">
                      <span className={status.className} title={status.title}>
                        {status.label}
                        {reading.status === "provisional" && reading.confidence != null
                          ? ` ${reading.confidence}%` : ""}
                      </span>
                      {reading.is_erroneous && (
                        <span className={`${BADGE} ml-1 bg-red-100 text-red-800`}>Entered in error</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {loading && <p role="status" className="text-xs text-muted-foreground">Loading readings…</p>}
      {!loading && !error && rows.length === 0 && (
        <p className="text-xs text-muted-foreground">No readings.</p>
      )}
      {next && !loading && (
        <button
          type="button"
          onClick={showMore}
          className="rounded-md border border-border px-3 py-1 text-xs font-medium hover:bg-muted/50"
        >
          Show more
        </button>
      )}
    </div>
  );
}
