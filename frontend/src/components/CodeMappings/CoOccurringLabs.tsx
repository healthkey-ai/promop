/**
 * Other results in the same panel or order as the code the dialog is open on
 * (#2702): what it was resulted with says what it is. Read after the dialog
 * opens, from /v1/code-mappings/<id>/co-occurring/, because the answer walks
 * patients' source records.
 */
import { useEffect, useState } from "react";
import api from "@/api/axios";
import { editMappingUrl } from "./mappingLinks";

export interface CoOccurringCode {
  source_code: string;
  source_vocabulary_id: string;
  omop_table: string;
  description: string;
  units: string[];
  system: string;
  method: string;
  /** Where System and Method come from: the code's mapping, or the LOINC the hospital sent. */
  axes_basis: "mapping" | "hospital" | "";
  axes_loinc: string;
  mapping_id: number | null;
  mapping_status: "approved" | "proposed" | "rejected" | "unmapped" | string;
  /** How many of the opened code's orders this code was resulted in. */
  orders: number;
}

export interface CoOccurringLabsPayload {
  /** The walk hit its time limit: the counts are unknown, not zero. */
  timed_out: boolean;
  records: number;
  records_read: number;
  records_with_order: number;
  orders: number;
  order_names: Array<{ name: string; count: number }>;
  order_names_total: number;
  codes: CoOccurringCode[];
  codes_total: number;
}

const CODES_COLLAPSED_LIMIT = 10;

/** No records at all: what a response is read over, so a field it omits is empty rather than undefined. */
const NOTHING: CoOccurringLabsPayload = {
  timed_out: false, records: 0, records_read: 0, records_with_order: 0, orders: 0,
  order_names: [], order_names_total: 0, codes: [], codes_total: 0,
};

const STATUS: Record<string, { label: string; tone: string }> = {
  approved: { label: "Approved", tone: "bg-green-100 text-green-800" },
  proposed: { label: "Proposed", tone: "bg-amber-100 text-amber-800" },
  rejected: { label: "Rejected", tone: "bg-red-100 text-red-800" },
  unmapped: { label: "Unmapped", tone: "bg-slate-100 text-slate-700" },
};

function count(value: number) {
  return new Intl.NumberFormat().format(value);
}

function Axis({ value, hospital }: { value: string; hospital: boolean }) {
  if (!value) return <>—</>;
  return <>
    {value}
    {hospital && <abbr title="From the LOINC the hospital sent" className="ml-1 font-semibold text-indigo-700 no-underline">H</abbr>}
  </>;
}

/** Mount with `key={mappingId}`: what was read for one mapping is never shown for another. */
export default function CoOccurringLabs({ mappingId }: { mappingId: number }) {
  // null until the read settles; `data` null when it failed.
  const [current, setCurrent] = useState<{ data: CoOccurringLabsPayload | null } | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const { data } = await api.get<CoOccurringLabsPayload>(`/v1/code-mappings/${mappingId}/co-occurring/`);
        if (active) setCurrent({ data: { ...NOTHING, ...data } });
      } catch {
        if (active) setCurrent({ data: null });
      }
    })();
    return () => { active = false; };
  }, [mappingId]);

  const data = current?.data ?? null;
  const codes = data?.codes ?? [];
  const visible = expanded ? codes : codes.slice(0, CODES_COLLAPSED_LIMIT);
  const hidden = codes.length - CODES_COLLAPSED_LIMIT;
  const unlisted = data ? data.codes_total - codes.length : 0;
  const unnamed = data ? data.order_names_total - data.order_names.length : 0;

  return <div className="mt-4 border-t border-slate-200 pt-4">
    <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-600">
      Other results in the same panel/order (co-occurring labs)
    </h4>
    <p className="mt-1 text-xs text-slate-500">
      Other Observations that share a panel or an order with this code: the same basedOn order, or listed in the
      same DiagnosticReport. Shows which panel this code belongs to.
    </p>
    {!current && <p role="status" className="mt-2 text-sm text-slate-600">Looking for order links…</p>}
    {current && !data && <p role="alert" className="mt-2 text-sm text-red-700">
      Could not load co-occurring labs. Close and reopen this mapping to retry.
    </p>}
    {data?.timed_out && <p role="alert" className="mt-2 text-sm text-amber-800">
      Reading the source records took too long. Close and reopen this mapping to retry.
    </p>}
    {data && !data.timed_out && data.records_with_order === 0 && (
      <p className="mt-2 text-sm text-slate-600">No order links in the source records.</p>
    )}
    {data && !data.timed_out && data.records_with_order > 0 && <>
      <dl className="mt-2 grid gap-2 text-xs sm:grid-cols-3">
        <div>
          <dt className="font-semibold text-slate-600">Records with an order reference</dt>
          <dd className="text-slate-800">{count(data.records_with_order)} of {count(data.records)}</dd>
        </div>
        <div>
          <dt className="font-semibold text-slate-600">Distinct orders</dt>
          <dd className="text-slate-800">
            {count(data.orders)}
            {data.records_read < data.records && <span className="text-slate-500">
              {" "}in the {count(data.records_read)} records read
            </span>}
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-slate-600">Order names</dt>
          <dd className="text-slate-800">
            {data.order_names.length === 0 ? "The orders carry no name"
              : data.order_names.map((order) => `${order.name} (${count(order.count)})`).join(" · ")}
            {unnamed > 0 && <span className="text-slate-500"> · and {count(unnamed)} more</span>}
          </dd>
        </div>
      </dl>

      {codes.length === 0
        ? <p className="mt-3 text-sm text-slate-600">No other result shares these orders in the records read.</p>
        : <div className="mt-3 overflow-x-auto">
          <table aria-label="Co-occurring labs" className="w-full text-left text-xs">
            <thead className="text-slate-500"><tr>
              <th className="pb-2 pr-3 font-semibold">Source code</th>
              <th className="pb-2 pr-3 font-semibold">Description</th>
              <th className="pb-2 pr-3 font-semibold">Units</th>
              <th className="pb-2 pr-3 font-semibold">System</th>
              <th className="pb-2 pr-3 font-semibold">Method</th>
              <th className="pb-2 font-semibold">Mapping status</th>
            </tr></thead>
            <tbody className="divide-y divide-slate-200">
              {visible.map((code) => {
                const status = STATUS[code.mapping_status] ?? { label: code.mapping_status, tone: STATUS.unmapped.tone };
                const seen = `In ${count(code.orders)} of ${count(data.orders)} orders`;
                const hospital = code.axes_basis === "hospital";
                return <tr key={`${code.source_vocabulary_id}|${code.source_code}`}>
                  <td className="py-2 pr-3 font-mono text-slate-900">
                    {code.mapping_id != null
                      ? <a className="text-sky-700 underline hover:text-sky-900" href={editMappingUrl(code.mapping_id)}
                        target="_blank" rel="noopener noreferrer"
                        title={`Open Edit Mapping for ${code.source_code}. ${seen}.`}>{code.source_code}</a>
                      : <span title={`Not in the mapping queue yet. ${seen}.`}>{code.source_code}</span>}
                  </td>
                  <td className="py-2 pr-3 text-slate-800">{code.description || "—"}</td>
                  <td className="py-2 pr-3 text-slate-700">{code.units.length ? code.units.join(", ") : "—"}</td>
                  <td className="py-2 pr-3 text-slate-700"><Axis value={code.system} hospital={hospital} /></td>
                  <td className="py-2 pr-3 text-slate-700"><Axis value={code.method} hospital={hospital} /></td>
                  <td className="py-2">
                    <span className={`rounded px-1.5 py-0.5 font-medium ${status.tone}`}>{status.label}</span>
                  </td>
                </tr>;
              })}
            </tbody>
          </table>
        </div>}
      {hidden > 0 && <button type="button" onClick={() => setExpanded((prev) => !prev)}
        className="mt-1 text-xs font-medium text-sky-700 hover:text-sky-900">
        {expanded ? "Show fewer" : `Show ${count(hidden)} more`}
      </button>}
      {unlisted > 0 && (expanded || hidden <= 0) && <p className="mt-1 text-xs text-slate-500">
        {count(unlisted)} more {unlisted === 1 ? "code shares" : "codes share"} these orders and {unlisted === 1 ? "is" : "are"} not listed.
      </p>}
      {codes.length > 0 && <p className="mt-2 text-xs text-slate-500">
        System and Method come from each code's current mapping. <span className="font-semibold text-indigo-700">H</span> =
        taken from the LOINC the hospital sent, when the mapping gives none. "—" when neither exists. Click a code to
        open its Edit Mapping. Read from the one sample record each patient's inventory keeps per code, most common first.
      </p>}
    </>}
  </div>;
}
