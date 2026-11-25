import { useEffect, useId, useMemo, useState } from "react";
import { AlertCircle, ChevronDown, ChevronRight, Database, Watch } from "lucide-react";
import api from "@/api/axios";
import ReadingsTable, { Moment } from "./OmopReadings";
import { OPEN_BY_DEFAULT, WEARABLE_LABEL, orderTables, type OmopGroup, type OmopTable } from "./omopRows";

interface OmopPayload {
  person_id: number;
  tables: OmopTable[];
}

function formatValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function WearableMark() {
  return (
    <span role="img" aria-label={WEARABLE_LABEL} title={WEARABLE_LABEL} data-testid="wearable-mark" className="inline-flex">
      <Watch aria-hidden="true" className="h-3.5 w-3.5 shrink-0 text-purple-600" />
    </span>
  );
}

function GroupRow({ group, table, personId }: { group: OmopGroup; table: OmopTable; personId: string }) {
  const [open, setOpen] = useState(false);
  const unresolved = group.concept_id === 0;
  const name = unresolved
    ? (group.source_value || "(no source value)")
    : (group.concept_name || `Concept ${group.concept_id}`);
  const code = unresolved
    ? null
    : [group.vocabulary_id, group.concept_code].filter(Boolean).join(" ");
  const params: Record<string, string | number> = { table: table.key, concept: group.concept_id };
  if (unresolved) {
    params.source_value = group.source_value ?? "";
    params.wearable = group.wearable ? "true" : "false";
  }
  const bodyId = useId();
  return (
    <li className="border-b border-border last:border-b-0" data-testid="omop-group">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((current) => !current)}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left text-xs hover:bg-muted/40"
      >
        {open ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
        <span className="flex min-w-0 items-center gap-1.5 font-medium text-foreground">
          <span className={unresolved ? "font-mono" : undefined}>{name}</span>
          {group.wearable && <WearableMark />}
        </span>
        {code && <span className="font-mono text-muted-foreground">{code}</span>}
        <span className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-muted-foreground">
          <span>Latest <Moment datetime={group.latest_datetime} date={group.latest_date} /></span>
          <span>Earliest <Moment datetime={group.earliest_datetime} date={group.earliest_date} /></span>
          {group.provisional_rows > 0 && <span>{group.provisional_rows} provisional</span>}
          <span className="rounded-full bg-slate-100 px-2 py-0.5 font-semibold text-slate-600">
            {group.count} {group.count === 1 ? "row" : "rows"}
          </span>
        </span>
      </button>
      {open && (
        <div id={bodyId} className="px-3 pb-3 pl-9">
          <ReadingsTable
            url={`/v1/patient-records/${personId}/omop-rows/`}
            params={params}
            label={`${name} rows`}
            showSource={!unresolved}
          />
        </div>
      )}
    </li>
  );
}

function GroupedRows({ table, personId }: { table: OmopTable; personId: string }) {
  const groups = table.groups ?? [];
  const unresolved = table.unresolved_groups ?? [];
  if (groups.length === 0 && unresolved.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border px-4 py-5 text-sm text-muted-foreground">
        No rows
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Grouped by concept, most recent first. Open a concept to read its rows, newest first. A reading
        that stores only a date is marked as such; no time of day is made up for it.
      </p>
      {groups.length > 0 && (
        <ul className="rounded-md border border-border" aria-label={`${table.label} by concept`}>
          {groups.map((group) => (
            <GroupRow
              key={`${group.concept_id}`}
              group={group}
              table={table}
              personId={personId}
            />
          ))}
        </ul>
      )}
      {unresolved.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold uppercase text-slate-500">
            Unresolved <span className="font-normal normal-case">({unresolved.length})</span>
          </h4>
          <p className="mb-2 text-xs text-muted-foreground">
            Stored at concept 0: no mapping has given these codes a concept yet. Grouped by source value.
          </p>
          <ul className="rounded-md border border-border" aria-label={`${table.label}, unresolved`}>
            {unresolved.map((group) => (
              <GroupRow
                key={`${group.source_value}-${group.wearable}`}
                group={group}
                table={table}
                personId={personId}
              />
            ))}
          </ul>
        </div>
      )}
      {table.truncated && (
        <p role="note" className="text-xs text-muted-foreground">
          More groups exist than are listed here; the most recent are shown.
        </p>
      )}
    </div>
  );
}

function RawRows({ table }: { table: OmopTable }) {
  const columns = useMemo(() => {
    const seen = new Set<string>();
    table.rows.forEach((row) => {
      Object.keys(row).forEach((key) => seen.add(key));
    });
    return Array.from(seen);
  }, [table.rows]);

  if (table.rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border px-4 py-5 text-sm text-muted-foreground">
        No rows
      </div>
    );
  }
  return (
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
                return (
                  <td
                    key={column}
                    className={[
                      "max-w-[260px] whitespace-nowrap px-3 py-2 font-mono",
                      empty ? "text-slate-400" : "text-slate-700",
                    ].join(" ")}
                    title={formatValue(value)}
                  >
                    <span className="block truncate">{formatValue(value)}</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OmopTableView({ table, personId }: { table: OmopTable; personId: string }) {
  const [open, setOpen] = useState(OPEN_BY_DEFAULT.has(table.key));
  const bodyId = useId();

  return (
    <section className="border-t border-border py-4 first:border-t-0 first:pt-0" data-testid={`omop-table-${table.key}`}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="min-w-0 flex-1 text-sm font-semibold text-foreground">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={bodyId}
            onClick={() => setOpen((current) => !current)}
            className="flex w-full items-center gap-2 rounded-md py-1 text-left hover:bg-muted/40"
          >
            {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            {table.label}
          </button>
        </h3>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-600">
          {table.count} {table.count === 1 ? "row" : "rows"}
        </span>
      </div>

      {open && (
        <div id={bodyId} className="mt-3">
          {table.grouped
            ? <GroupedRows table={table} personId={personId} />
            : <RawRows table={table} />}
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

  const tables = orderTables(payload?.tables ?? []);
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
        <OmopTableView key={table.key} table={table} personId={personId} />
      ))}
    </div>
  );
}
