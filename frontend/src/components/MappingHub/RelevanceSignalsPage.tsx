import PageTitle from "@/components/Branding/PageTitle";
import api from "@/api/axios";
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

/** Absent-from-relevance signals from extraction services (#1942, #1972).
 *  A decision covers a (disease, concept) pair, so every period's report of
 *  the pair is one row here. */

type Status = "proposed" | "accepted" | "rejected";

interface Signal {
  id: number;
  source: string;
  disease: string;
  concept_id: number;
  concept_name: string | null;
  period: string;
  document_count: number;
  document_total: number | null;
  status: Status;
  reviewed_at: string | null;
}

const DISEASES: [string, string][] = [
  ["BC", "Breast cancer"], ["FL", "Follicular lymphoma"], ["MM", "Multiple myeloma"],
  ["CLL", "Chronic lymphocytic leukemia"], ["MCL", "Mantle cell lymphoma"],
];
const STATUSES: [Status, string][] = [["proposed", "Proposed"], ["accepted", "Accepted"], ["rejected", "Rejected"]];

interface Rights {
  can_accept: boolean;
  can_reject: boolean;
}

interface Pair {
  key: string;
  disease: string;
  concept_id: number;
  concept_name: string | null;
  status: Status;
  latest: Signal;
  periods: number;
  sources: string[];
}

/** One row per pair, showing its most recent period's counts. */
function groupSignals(signals: Signal[]): Pair[] {
  const pairs = new Map<string, Pair>();
  for (const signal of signals) {
    const key = `${signal.disease}:${signal.concept_id}`;
    const pair = pairs.get(key);
    if (!pair) {
      pairs.set(key, { key, disease: signal.disease, concept_id: signal.concept_id, concept_name: signal.concept_name,
        status: signal.status, latest: signal, periods: 1, sources: [signal.source] });
      continue;
    }
    pair.periods += 1;
    if (!pair.sources.includes(signal.source)) pair.sources.push(signal.source);
    if (signal.period > pair.latest.period) pair.latest = signal;
  }
  return [...pairs.values()].sort((a, b) =>
    a.disease.localeCompare(b.disease) || b.latest.document_count - a.latest.document_count);
}

const share = (signal: Signal) => signal.document_total
  ? ` of ${signal.document_total.toLocaleString()} (${Math.round(100 * signal.document_count / signal.document_total)}%)`
  : "";

export default function RelevanceSignalsPage() {
  // The server decides who may do what; it mirrors the mapping pages' roles.
  const [rights, setRights] = useState<Rights>({ can_accept: false, can_reject: false });
  const [searchParams, setSearchParams] = useSearchParams();
  const status = (searchParams.get("status") as Status) || "proposed";
  const disease = searchParams.get("disease") || "";
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let active = true;
    void api.get<{ results: Signal[]; permissions?: Rights }>("/v1/relevance/signals/", {
      params: { status, ...(disease ? { disease } : {}) },
    }).then(({ data }) => {
      if (active) {
        setSignals(data.results);
        setRights(data.permissions ?? { can_accept: false, can_reject: false });
        setError("");
      }
    }).catch(() => {
      if (active) { setSignals([]); setError("Could not load relevance signals."); }
    });
    return () => { active = false; };
  }, [status, disease, refresh]);

  const pairs = useMemo(() => groupSignals(signals ?? []), [signals]);
  const setFilter = (name: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(name, value); else next.delete(name);
    setSearchParams(next);
    setSignals(null);
  };
  const decide = async (pair: Pair, decision: Status) => {
    setSaving(pair.key);
    setError("");
    try {
      await api.patch(`/v1/relevance/signals/${pair.latest.id}/`, { status: decision });
      setRefresh((value) => value + 1);
    } catch {
      setError(`Could not save the decision on ${pair.concept_name ?? pair.concept_id}.`);
    } finally {
      setSaving(null);
    }
  };
  const diseaseName = (code: string) => DISEASES.find(([c]) => c === code)?.[1] ?? code;

  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-7xl">
    <Link to="/mappings" className="text-sm text-sky-700 underline">← Mapping Administration</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Disease Relevance Signals</PageTitle>
    <p className="mt-2 max-w-4xl text-sm text-slate-600">
      Concepts an extraction service kept finding in a disease's documents that are not in that disease's relevance set.
      Counts are aggregate: no patient or document is named. Accepting adds the concept to the disease's set as <em>curated</em>,
      which extractors then emphasise. A decision covers every period's report of the concept for that disease.
    </p>

    <div className="mt-5 flex flex-wrap items-end gap-4 rounded-md border border-slate-200 bg-white p-4">
      <div role="tablist" aria-label="Signal status" className="flex gap-1">
        {STATUSES.map(([value, label]) => <button key={value} type="button" role="tab" aria-selected={status === value}
          onClick={() => setFilter("status", value === "proposed" ? "" : value)}
          className={`rounded px-3 py-1.5 text-sm font-medium ${status === value ? "bg-sky-800 text-white" : "text-slate-700 hover:bg-slate-100"}`}>
          {label}</button>)}
      </div>
      <label className="text-sm font-medium text-slate-700">Disease
        <select value={disease} onChange={(event) => setFilter("disease", event.target.value)}
          className="ml-2 rounded border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">All diseases</option>
          {DISEASES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
        </select>
      </label>
    </div>

    {signals !== null && !rights.can_accept && <p className="mt-3 text-sm text-slate-600">{rights.can_reject
      ? "Doctors can reject a signal or return a rejected one to proposed. Only organization admins can accept."
      : "You can view the queue. Organization admins accept and reject signals; doctors can reject them."}</p>}
    {error && <p role="alert" className="mt-4 text-sm text-red-700">{error}</p>}
    {signals === null && <p role="status" className="mt-5 text-sm text-slate-600">Loading signals…</p>}
    {signals !== null && pairs.length === 0 && !error && <p className="mt-5 rounded border border-slate-200 bg-white p-4 text-sm text-slate-700">
      No {status} signals{disease ? ` for ${diseaseName(disease)}` : ""}.</p>}
    {pairs.length > 0 && <div className="mt-5 overflow-x-auto rounded-md border border-slate-200 bg-white">
      <table aria-label="Relevance signals" className="w-full min-w-[900px] text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>
          <th className="px-3 py-3">Disease</th>
          <th className="px-3 py-3">Concept</th>
          <th className="px-3 py-3">Documents (latest period)</th>
          <th className="px-3 py-3">Periods</th>
          <th className="px-3 py-3">Reported by</th>
          <th className="px-3 py-3 text-right">Decision</th>
        </tr></thead>
        <tbody>{pairs.map((pair) => <tr key={pair.key} className="border-t border-slate-100">
          <td className="px-3 py-2">{diseaseName(pair.disease)}</td>
          <td className="px-3 py-2"><span className="font-medium">{pair.concept_name ?? "Unknown concept"}</span>
            <span className="ml-2 font-mono text-xs text-slate-500">{pair.concept_id}</span></td>
          <td className="px-3 py-2">{pair.latest.document_count.toLocaleString()}{share(pair.latest)}
            <span className="block text-xs text-slate-500">{pair.latest.period}</span></td>
          <td className="px-3 py-2">{pair.periods}</td>
          <td className="px-3 py-2">{pair.sources.join(", ")}</td>
          <td className="px-3 py-2 text-right"><span className="inline-flex justify-end gap-2">
            {pair.status === "proposed" && rights.can_accept && <button type="button" disabled={saving === pair.key}
              onClick={() => void decide(pair, "accepted")} aria-label={`Accept ${pair.concept_name ?? pair.concept_id} for ${pair.disease}`}
              className="rounded bg-green-700 px-3 py-1 text-xs font-medium text-white hover:bg-green-800 disabled:opacity-50">Accept</button>}
            {pair.status === "proposed" && rights.can_reject && <button type="button" disabled={saving === pair.key}
              onClick={() => void decide(pair, "rejected")} aria-label={`Reject ${pair.concept_name ?? pair.concept_id} for ${pair.disease}`}
              className="rounded border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50">Reject</button>}
            {pair.status !== "proposed" && (pair.status === "accepted" ? rights.can_accept : rights.can_reject) &&
              <button type="button" disabled={saving === pair.key} onClick={() => void decide(pair, "proposed")}
                aria-label={`Return ${pair.concept_name ?? pair.concept_id} for ${pair.disease} to proposed`}
                className="text-xs text-sky-700 underline disabled:text-slate-400">Return to proposed</button>}
          </span></td>
        </tr>)}</tbody>
      </table>
    </div>}
  </main></div>;
}
