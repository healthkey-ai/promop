import { useState } from "react";
import api from "@/api/axios";

export type SuggestCandidate = {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  score?: number;
  umls_score?: number;
  semantic_score?: number;
  vector_distance?: number;
};
export type CandidateActivity = {
  stage: string;
  mapping_id?: number;
  source_code?: string;
  source_vocabulary_id?: string;
  strategy?: string;
  candidates?: SuggestCandidate[];
  suggested?: SuggestCandidate | null;
  note?: string;
  updated?: boolean;
  dry_run?: boolean;
};

type Props = {
  activity: CandidateActivity[];
  finished: boolean;
  onSaved?: () => void;
};
const stages = { umls: "UMLS", lexical: "Lexical", semantic: "Semantic (vector distance)" };

export default function SuggestCandidates({ activity, finished, onSaved }: Props) {
  const [saving, setSaving] = useState<number | null>(null);
  const [selected, setSelected] = useState<Record<number, number>>({});
  const [error, setError] = useState("");
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
  return <section aria-label="Suggestion candidates" className="mb-4 space-y-3 rounded-md border border-slate-200 bg-white p-4">
    <h2 className="font-semibold">Suggestion candidates</h2>
    <p className="text-sm text-slate-600">Candidates appear as each search finishes. After the run finishes, you can choose an alternative and review it in the mapping table.</p>
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {!groups.size && <p role="status" className="text-sm">{finished ? "No candidates recorded for this run." : "Waiting for candidates…"}</p>}
    {[...groups].map(([mappingId, events]) => {
      const source = events[0];
      const result = events.filter(event => event.stage === "result").at(-1);
      const ranked = events.filter(event => event.stage === "ranked").at(-1);
      const winner = (result ?? ranked)?.suggested;
      const activeId = selected[mappingId] ?? winner?.concept_id;
      const searches = events.filter(event => event.stage === "candidates");
      // Ranking may add candidates through query expansion or enrichment.
      const seen = new Set(searches.flatMap(event => (event.candidates ?? []).map(candidate => candidate.concept_id)));
      const additional = (ranked?.candidates ?? []).filter(candidate => !seen.has(candidate.concept_id));
      const lists = [...searches, ...(additional.length ? [{ stage: "candidates", strategy: "additional", candidates: additional }] : [])];
      return <article key={mappingId} className="rounded border border-slate-200 p-3">
        <h3 className="font-medium">{source.source_vocabulary_id || "Uncoded"}:{source.source_code}</h3>
        {lists.map((event, index) => <div key={index} className="mt-2">
          <h4 className="text-sm font-medium">{stages[event.strategy as keyof typeof stages] ?? "Additional ranking candidates"}</h4>
          {!event.candidates?.length && <p className="text-sm text-slate-500">No matches</p>}
          <ul className="space-y-1 text-sm">
            {event.candidates?.map(candidate => <li key={candidate.concept_id} className="flex flex-wrap items-center gap-2">
              <span>{candidate.vocabulary_id}:{candidate.concept_code} — {candidate.concept_name} (OMOP {candidate.concept_id})</span>
              {(candidate.vector_distance != null || candidate.semantic_score != null) &&
                <span className="text-slate-600">Distance {(candidate.vector_distance ?? (1 - candidate.semantic_score!)).toFixed(4)}</span>}
              {candidate.concept_id === winner?.concept_id && <span className="font-semibold">Winner</span>}
              {selected[mappingId] === candidate.concept_id && <span role="status">Selected alternative · awaiting review</span>}
              {finished && result?.updated && !result.dry_run && candidate.concept_id !== activeId && <button
                type="button" disabled={saving !== null} className="rounded border px-2 py-1 disabled:opacity-50"
                aria-label={`Use ${candidate.concept_name} for ${source.source_code}`}
                onClick={() => void choose(mappingId, candidate)}>{saving === mappingId ? "Saving…" : "Use candidate"}</button>}
            </li>)}
          </ul>
        </div>)}
        {ranked && <p className="mt-2 text-sm font-medium">{winner ? `Winner: ${winner.concept_name}` : "No destination proposed"}</p>}
        {(result ?? ranked)?.note && <p className="mt-1 text-sm text-slate-600">{(result ?? ranked)?.note}</p>}
      </article>;
    })}
  </section>;
}
