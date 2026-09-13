import { useState } from "react";
import api from "@/api/axios";

interface Concept {
  concept_id: number;
  concept_name: string;
  vocabulary_id: string;
  concept_code: string;
  domain_id: string;
  standard_concept?: string;
}
export interface ValueMapping {
  target_concept: Concept | null;
  question_concept: Concept | null;
  role: string;
  status: string;
  outcome: string;
  notes: string;
  vocabulary_release: string;
  revision: number;
}

export function ValueMappingEditor({ choiceId, initial, onSaved }: {
  choiceId: number; initial?: ValueMapping | null; onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [mapping, setMapping] = useState<ValueMapping>(initial || {
    target_concept: null, question_concept: null, role: "answer", status: "proposed",
    outcome: "needs_review", notes: "", vocabulary_release: "", revision: 0,
  });
  const [query, setQuery] = useState("");
  const [searchFor, setSearchFor] = useState<"target_concept" | "question_concept">("target_concept");
  const [results, setResults] = useState<Concept[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<{ revision: number; decision: ValueMapping }[]>([]);
  const search = async () => {
    setBusy(true); setError("");
    try {
      const { data } = await api.get("/v1/concepts/search/", {
        params: { q: query, standard_concept: "S", limit: 50 },
      });
      const concepts: Concept[] = data.results || data || [];
      setResults(concepts.filter(c => searchFor === "question_concept"
        ? ["Measurement", "Observation"].includes(c.domain_id)
        : mapping.role !== "answer" || c.domain_id === "Meas Value"));
    } catch { setError("Concept search failed. Your draft is unchanged."); }
    finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true); setError("");
    try {
      await api.patch(`/v1/field-choices/${choiceId}/mapping/`, {
        target_concept: mapping.outcome === "mapped" ? mapping.target_concept?.concept_id ?? null : null,
        question_concept: mapping.outcome === "mapped" && mapping.role === "answer" ? mapping.question_concept?.concept_id ?? null : null,
        role: mapping.role, status: mapping.status, outcome: mapping.outcome,
        notes: mapping.notes, vocabulary_release: mapping.vocabulary_release,
      });
      onSaved();
    } catch (e) {
      const data = (e as { response?: { data?: unknown } }).response?.data;
      setError(data ? JSON.stringify(data) : "Could not save the review. Your draft is unchanged.");
    } finally { setBusy(false); }
  };
  const showHistory = async () => {
    try {
      const { data } = await api.get(`/v1/field-choices/${choiceId}/mapping/`);
      setHistory(data.history);
    } catch { setError("Could not load review history."); }
  };
  const inputClass = "mt-1 w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm";
  return <div className="mt-2 border-t border-gray-100 pt-2">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
      className="text-xs font-medium text-blue-700 hover:underline">
      {initial?.status === "approved" ? "Reviewed" : "Review value mapping"} · {(initial?.outcome || "needs_review").replace(/_/g, " ")}
    </button>
    {open && <div className="mt-3 space-y-3 rounded bg-slate-50 p-3">
      <p className="text-xs text-slate-600">The field identifies the question. This review identifies the answer or a separate clinical assertion. Unmapped values remain usable.</p>
      <div className="grid grid-cols-2 gap-3">
        <label className="text-xs">Mapping role<select className={inputClass} value={mapping.role} onChange={e => setMapping({ ...mapping, role: e.target.value, target_concept: null, question_concept: null })}>
          <option value="answer">Coded answer</option><option value="fact">Clinical assertion (adapter required)</option><option value="structured">Structured representation</option>
        </select></label>
        <label className="text-xs">Disposition<select className={inputClass} value={mapping.outcome} onChange={e => setMapping({ ...mapping, outcome: e.target.value })}>
          {['needs_review', 'mapped', 'ambiguous', 'no_equivalent', 'not_applicable', 'structured'].map(v => <option key={v} value={v}>{v.replace(/_/g, ' ')}</option>)}
        </select></label>
      </div>
      {mapping.outcome === "mapped" && <>
        <div className="text-xs space-y-1">
          <p>Target: {mapping.target_concept ? `${mapping.target_concept.concept_name} · ${mapping.target_concept.vocabulary_id}:${mapping.target_concept.concept_code}` : "Not selected"}</p>
          <p>Question override: {mapping.question_concept?.concept_name || "Use field mapping"}</p>
          {mapping.question_concept && <button type="button" className="text-blue-700" onClick={() => setMapping({ ...mapping, question_concept: null })}>Clear question override</button>}
        </div>
        <label className="block text-xs">Search for<select className={inputClass} value={searchFor} onChange={e => { setSearchFor(e.target.value as typeof searchFor); setResults([]); }}>
          <option value="target_concept">Value target</option><option value="question_concept" disabled={mapping.role !== "answer"}>Question override</option>
        </select></label>
        <div className="flex gap-2"><input aria-label="Search value concepts" className={inputClass} value={query} onChange={e => setQuery(e.target.value)} placeholder="Concept name, code or ID" />
          <button type="button" disabled={busy || query.trim().length < 3} onClick={search} className="text-sm text-blue-700 disabled:opacity-40">Search</button></div>
        <ul className="max-h-40 overflow-y-auto divide-y divide-slate-200">{results.map(c => <li key={c.concept_id}>
          <button type="button" className="w-full p-2 text-left text-xs hover:bg-blue-50" onClick={() => { setMapping({ ...mapping, [searchFor]: c }); setResults([]); }}>
            <span className="block font-medium">{c.concept_name}</span>{c.vocabulary_id}:{c.concept_code} · {c.domain_id} · {c.concept_id}
          </button></li>)}</ul>
      </>}
      <label className="block text-xs">Evidence / rationale<textarea className={inputClass} rows={3} value={mapping.notes} onChange={e => setMapping({ ...mapping, notes: e.target.value })} /></label>
      <label className="block text-xs">Vocabulary release<input className={inputClass} value={mapping.vocabulary_release} onChange={e => setMapping({ ...mapping, vocabulary_release: e.target.value })} placeholder="Release used to verify this decision" /></label>
      <label className="block text-xs">Review status<select className={inputClass} value={mapping.status} onChange={e => setMapping({ ...mapping, status: e.target.value })}>
        <option value="proposed">Proposed</option><option value="approved">Approved</option><option value="rejected">Rejected</option>
      </select></label>
      {error && <p role="alert" className="text-xs text-red-700">{error}</p>}
      <div className="flex justify-between"><button type="button" onClick={showHistory} className="text-xs text-slate-600 underline">Review history</button>
        <button type="button" disabled={busy} onClick={save} className="rounded bg-blue-700 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">{busy ? "Working…" : "Save review"}</button></div>
      {history.map(h => <p className="text-xs text-slate-600" key={h.revision}>Revision {h.revision}: {h.decision.status} / {h.decision.outcome} — {h.decision.notes}</p>)}
    </div>}
  </div>;
}
