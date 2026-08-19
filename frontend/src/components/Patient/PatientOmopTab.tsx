import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Database } from "lucide-react";
import api from "@/api/axios";

interface OmopTable {
  key: string;
  label: string;
  count: number;
  rows: Array<Record<string, unknown>>;
}

interface OmopPayload {
  person_id: number;
  tables: OmopTable[];
}

function formatValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Columns whose name ends with `_concept_name` are hidden; their value is
 *  shown as a tooltip on the matching `_concept` ID cell instead. */
function OmopTableView({ table }: { table: OmopTable }) {
  const columns = useMemo(() => {
    const seen = new Set<string>();
    table.rows.forEach((row) => {
      Object.keys(row).forEach((key) => seen.add(key));
    });
    // Hide *_concept_name columns — they feed tooltips, not visible cells.
    return Array.from(seen).filter((col) => !col.endsWith("_concept_name"));
  }, [table.rows]);

  return (
    <section className="border-t border-border py-6 first:border-t-0 first:pt-0">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{table.label}</h3>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
          {table.count} {table.count === 1 ? "row" : "rows"}
        </span>
      </div>

      {table.rows.length === 0 ? (
        <div className="rounded-md border border-dashed border-border px-4 py-5 text-sm text-muted-foreground">
          No rows
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="min-w-full border-collapse text-left text-xs">
            <thead className="bg-slate-50 text-[11px] uppercase text-slate-500">
              <tr>
                {columns.map((column) => (
                  <th key={column} className="whitespace-nowrap border-b border-border px-3 py-2 font-semibold">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="bg-background align-top">
                  {columns.map((column) => {
                    const value = row[column];
                    const empty = value === null || value === undefined || value === "";
                    // For *_concept columns, use the companion _concept_name as tooltip
                    const nameCol = column.endsWith("_concept") ? column + "_name" : null;
                    const conceptName = nameCol ? row[nameCol] : null;
                    const tooltip = conceptName
                      ? `${formatValue(value)} — ${conceptName}`
                      : formatValue(value);
                    const hasName = typeof conceptName === "string";
                    return (
                      <td
                        key={column}
                        className={[
                          "max-w-[260px] whitespace-nowrap px-3 py-2 font-mono",
                          empty ? "text-slate-400" : "text-slate-700",
                        ].join(" ")}
                        title={tooltip}
                      >
                        <span className="block truncate">
                          {formatValue(value)}
                          {hasName && (
                            <span className="ml-1 font-sans text-[10px] text-slate-400">
                              {conceptName}
                            </span>
                          )}
                        </span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function PatientOmopTab({ personId }: { personId: string }) {
  const [payload, setPayload] = useState<OmopPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const res = await api.get(`/v1/patient-records/${personId}/omop/`);
        if (!cancelled) {
          setPayload(res.data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          const detail =
            err && typeof err === "object" && "response" in err
              ? (err as { response?: { data?: { detail?: string; error?: string } } }).response?.data
              : undefined;
          setError(detail?.detail || detail?.error || "Failed to load OMOP rows.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [personId]);

  if (loading) {
    return (
      <div className="space-y-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse rounded-md bg-slate-100" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>{error}</span>
      </div>
    );
  }

  const tables = payload?.tables ?? [];
  const totalRows = tables.reduce((sum, table) => sum + table.count, 0);

  return (
    <div>
      <div className="mb-5 flex items-center gap-3 rounded-md border border-slate-200 bg-slate-50 px-4 py-3">
        <Database className="h-4 w-4 text-slate-500" />
        <div>
          <p className="text-sm font-semibold text-slate-800">{totalRows} OMOP rows</p>
          <p className="text-xs text-slate-500">Person #{payload?.person_id ?? personId}</p>
        </div>
      </div>
      {tables.map((table) => (
        <OmopTableView key={table.key} table={table} />
      ))}
    </div>
  );
}
