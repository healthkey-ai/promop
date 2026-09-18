import { useState } from "react";
import api from "@/api/axios";

export type SuggestCandidate = {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  score?: number;
  umls_score?: number;
  lexical_score?: number;
  semantic_score?: number;
  vector_distance?: number;
};
type Alternative = {
  concept_id: number;
  concept_name?: string;
  confidence: number;
  ranker?: string;
};
export type CandidateActivity = {
  stage: string;
  mapping_id?: number;
  source_code?: string;
  source_vocabulary_id?: string;
  source_description?: string;
  source_code_description?: string;
  strategy?: string;
  candidates?: SuggestCandidate[];
  suggested?: SuggestCandidate | null;
  note?: string;
  updated?: boolean;
  dry_run?: boolean;
  alternatives?: Alternative[];
  ranking_timings?: Record<string, number>;
};

type Props = {
  activity: CandidateActivity[];
  finished: boolean;
  onSaved?: () => void;
};

const STRATEGY_LABELS: Record<string, string> = { umls: "U", lexical: "L", vectors: "V", semantic: "V" };

const RANKER_STYLE: Record<string, string> = {
  jev: "bg-indigo-100 text-indigo-700",
  anthropic: "bg-sky-100 text-sky-700",
};

type MergedCandidate = SuggestCandidate & { strategies: Set<string> };

/** Deduplicate candidates across strategy events into a single list. */
function mergeCandidates(searches: CandidateActivity[], ranked?: CandidateActivity): MergedCandidate[] {
  const byId = new Map<number, MergedCandidate>();
  for (const event of searches) {
    for (const c of event.candidates ?? []) {
      const existing = byId.get(c.concept_id);
      if (existing) {
        existing.strategies.add(event.strategy || "");
        if (c.umls_score != null && existing.umls_score == null) existing.umls_score = c.umls_score;
        if (c.lexical_score != null && existing.lexical_score == null) existing.lexical_score = c.lexical_score;
        if (c.semantic_score != null && existing.semantic_score == null) existing.semantic_score = c.semantic_score;
        if (c.vector_distance != null && existing.vector_distance == null) existing.vector_distance = c.vector_distance;
      } else {
        byId.set(c.concept_id, { ...c, strategies: new Set([event.strategy || ""]) });
      }
    }
  }
  // Add any candidates from ranking that weren't in retrieval.
  for (const c of ranked?.candidates ?? []) {
    if (!byId.has(c.concept_id)) {
      byId.set(c.concept_id, { ...c, strategies: new Set(["additional"]) });
    }
  }
  return [...byId.values()];
}

export default function SuggestCandidates({ activity, finished, onSaved }: Props) {
  const [saving, setSaving] = useState<number | null>(null);
  const [selected, setSelected] = useState<Record<number, number>>({});
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const groups = new Map<number, CandidateActivity[]>();
  for (const event of activity) {
    if (event.mapping_id == null) continue;
    const events = groups.get(event.mapping_id) ?? [];
    events.push(event);
    groups.set(event.mapping_id, events);
  }
  const choose = async (mappingId: number, candidate: SuggestCandidate) => {
    setSaving(mappingId);
    setError("");
    try {
      await api.patch(`/v1/code-mappings/${mappingId}/`, {
        destination_concept_id: candidate.concept_id,
        status: "proposed",
      });
      setSelected(current => ({ ...current, [mappingId]: candidate.concept_id }));
      onSaved?.();
    } catch {
      setError("Could not save the alternative candidate. Please try again.");
    } finally {
      setSaving(null);
    }
  };

  // Build a confidence lookup from the ranked event's alternatives.
  const confidenceLookup = (events: CandidateActivity[]): Map<number, Alternative[]> => {
    const ranked = events.filter(e => e.stage === "ranked").at(-1);
    const alts = ranked?.alternatives;
    if (!alts) return new Map();
    const m = new Map<number, Alternative[]>();
    for (const a of alts) {
      const existing = m.get(a.concept_id) ?? [];
      existing.push(a);
      m.set(a.concept_id, existing);
    }
    return m;
  };

  const sourceDescription = (events: CandidateActivity[]): string | undefined => {
    for (const e of events) {
      const desc = e.source_description || e.source_code_description;
      if (desc) return desc;
    }
    return undefined;
  };

  return <section aria-label="Suggestion candidates" className="mb-4 space-y-3 rounded-md border border-slate-200 bg-white p-4">
    <button type="button" className="flex w-full items-center justify-between text-left"
      onClick={() => setCollapsed(c => !c)} aria-expanded={!collapsed}>
      <h2 className="font-semibold">Suggestion candidates</h2>
      <span className="text-slate-400">{collapsed ? "+" : "\u2212"}</span>
    </button>
    {!collapsed && <>
      <p className="text-sm text-slate-600">Candidates appear as each search finishes. After the run finishes, you can choose an alternative and review it in the mapping table.</p>
      {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
      {!groups.size && <p role="status" className="text-sm">{finished ? "No candidates recorded for this run." : "Waiting for candidates\u2026"}</p>}
      {[...groups].map(([mappingId, events]) => {
        const source = events[0];
        const description = sourceDescription(events);
        const result = events.filter(event => event.stage === "result").at(-1);
        const ranked = events.filter(event => event.stage === "ranked").at(-1);
        const winner = (result ?? ranked)?.suggested;
        const activeId = selected[mappingId] ?? winner?.concept_id;
        const searches = events.filter(event => event.stage === "candidates");
        const candidates = mergeCandidates(searches, ranked);
        const confidences = confidenceLookup(events);
        return <article key={mappingId} className="rounded border border-slate-200 p-3">
          <h3 className="font-medium">
            {source.source_vocabulary_id || "Uncoded"}:{source.source_code}
            {description && <span className="ml-2 font-normal text-slate-600">{description}</span>}
          </h3>
          {!candidates.length && <p className="mt-1 text-sm text-slate-500">No matches</p>}
          {candidates.length > 0 && <table className="mt-2 w-full text-left text-sm">
            <thead><tr className="border-b text-xs text-slate-500">
              <th className="pb-1 pr-2 font-medium">Candidate</th>
              <th className="pb-1 px-2 font-medium" title="Retrieval strategies: U=UMLS, L=Lexical, V=Vectors">Found by</th>
              <th className="pb-1 px-2 font-medium">Confidence</th>
              <th className="pb-1 pl-2 font-medium">Action</th>
            </tr></thead>
            <tbody>{candidates.map(candidate => {
              const confs = confidences.get(candidate.concept_id) ?? [];
              const strategies = [...candidate.strategies].map(s => STRATEGY_LABELS[s] || s).filter(Boolean).join("");
              return <tr key={candidate.concept_id} className={`border-b border-slate-100 ${candidate.concept_id === winner?.concept_id ? "bg-sky-50" : ""}`}>
                <td className="py-1.5 pr-2">
                  <span>{candidate.vocabulary_id}:{candidate.concept_code}</span>
                  <span className="ml-1 text-slate-600">— {candidate.concept_name}</span>
                  <span className="ml-1 text-xs text-slate-400">OMOP {candidate.concept_id}</span>
                </td>
                <td className="py-1.5 px-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono" title={[...candidate.strategies].join(", ")}>{strategies}</span>
                  {candidate.vector_distance != null && <span className="ml-1 text-xs text-slate-500" title="Cosine distance">{candidate.vector_distance.toFixed(3)}</span>}
                </td>
                <td className="py-1.5 px-2">
                  {confs.map((conf, ci) => <span key={ci} className={`mr-1 rounded px-1.5 py-0.5 text-xs font-medium ${RANKER_STYLE[conf.ranker ?? ""] ?? "bg-slate-100 text-slate-700"}`}
                    title={conf.ranker ? `${conf.ranker} confidence` : "confidence"}>{Math.round(conf.confidence * 100)}%{conf.ranker ? ` ${conf.ranker[0].toUpperCase()}` : ""}</span>)}
                </td>
                <td className="py-1.5 pl-2 whitespace-nowrap">
                  {candidate.concept_id === winner?.concept_id && <span className="font-semibold text-sky-700">Winner</span>}
                  {selected[mappingId] === candidate.concept_id && <span role="status" className="text-xs">Selected</span>}
                  {finished && result?.updated && !result.dry_run && candidate.concept_id !== activeId && <button
                    type="button" disabled={saving !== null} className="rounded border px-2 py-0.5 text-xs disabled:opacity-50"
                    aria-label={`Use ${candidate.concept_name} for ${source.source_code}`}
                    onClick={() => void choose(mappingId, candidate)}>{saving === mappingId ? "Saving\u2026" : "Use"}</button>}
                </td>
              </tr>;
            })}</tbody>
          </table>}
          {ranked && <p className="mt-2 text-sm font-medium">{winner ? `Winner: ${winner.concept_name}` : "No destination proposed"}</p>}
          {(result ?? ranked)?.note && <p className="mt-1 text-sm text-slate-600">{(result ?? ranked)?.note}</p>}
          {ranked?.ranking_timings && <p className="mt-1 text-xs text-slate-500">
            Ranking: {Object.entries(ranked.ranking_timings).map(([k, v]) =>
              `${k.replace(/_ms$/, "")} ${(v / 1000).toFixed(1)}s`
            ).join(" · ")}
          </p>}
        </article>;
      })}
    </>}
  </section>;
}
