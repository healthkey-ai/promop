import type { CandidateActivity, SuggestCandidate } from "./SuggestCandidates";

const labels: Record<string, string> = { umls: "UMLS", lexical: "Lexical", vectors: "Vectors", semantic: "Vectors", additional: "Additional candidates" };

export default function IndividualSuggestCandidates({ activity, running, selectedId, onSelect }: {
  activity: CandidateActivity[];
  running: boolean;
  selectedId: string;
  onSelect: (candidate: SuggestCandidate) => void;
}) {
  const result = activity.filter(event => event.stage === "result").at(-1);
  const ranked = activity.filter(event => event.stage === "ranked").at(-1);
  const effectiveResult = result ?? ranked;
  const searches = activity.filter(event => event.stage === "candidates");
  const seen = new Set(searches.flatMap(event => (event.candidates ?? []).map(candidate => candidate.concept_id)));
  const additional = effectiveResult?.candidates?.filter(candidate => !seen.has(candidate.concept_id)) ?? [];
  const lists = [...searches, ...(additional.length ? [{ strategy: "additional", candidates: additional }] : [])];

  // Build a confidence lookup from the ranked/result event's alternatives.
  const confidences = new Map<number, { confidence: number; ranker?: string }>();
  for (const alt of effectiveResult?.alternatives ?? []) {
    const existing = confidences.get(alt.concept_id);
    if (!existing || alt.confidence > existing.confidence) {
      confidences.set(alt.concept_id, { confidence: alt.confidence, ranker: alt.ranker });
    }
  }

  return <section aria-label="Individual suggestion candidates" className="mb-3 rounded-md border border-sky-200 bg-sky-50 p-3">
    <h3 className="text-sm font-semibold">Suggestion candidates</h3>
    <p role="status" className="mt-1 text-xs text-slate-600">
      {running ? "Searching… Candidates appear as each search finishes. You can choose one now." : "Choose a candidate to fill the destination, then Save your mapping."}
    </p>
    <div className="mt-2 max-h-64 space-y-2 overflow-y-auto">
      {lists.map((event, index) => <div key={index}>
        <h4 className="text-xs font-semibold">{labels[event.strategy || ""] || event.strategy}</h4>
        {!event.candidates?.length && <p className="text-xs text-slate-500">No matches</p>}
        <ul className="space-y-1">{event.candidates?.map(candidate => {
          const conf = confidences.get(candidate.concept_id);
          return <li key={candidate.concept_id}>
            <button type="button" aria-pressed={selectedId === String(candidate.concept_id)}
              onClick={() => onSelect(candidate)}
              className="w-full rounded border border-slate-200 bg-white px-2 py-1 text-left text-sm hover:bg-sky-100 aria-pressed:border-sky-600">
              {candidate.concept_name} — {candidate.vocabulary_id}:{candidate.concept_code} (OMOP {candidate.concept_id})
              {candidate.vector_distance != null && <span className="ml-2 text-xs" title="Cosine distance; lower is closer">Distance {candidate.vector_distance.toFixed(4)}</span>}
              {conf != null && <span className={`ml-2 inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-xs font-medium ${conf.ranker === "jev" ? "bg-indigo-100 text-indigo-700" : conf.ranker === "anthropic" ? "bg-sky-100 text-sky-700" : "bg-slate-100 text-slate-700"}`}
                title={conf.ranker ? `${conf.ranker} confidence` : "confidence"}>
                {Math.round(conf.confidence * 100)}%{conf.ranker ? ` ${conf.ranker[0].toUpperCase()}` : ""}
              </span>}
              {effectiveResult?.suggested?.concept_id === candidate.concept_id && <strong className="ml-2">Winner</strong>}
              {selectedId === String(candidate.concept_id) && <span className="ml-2 text-xs">Selected</span>}
            </button>
          </li>;
        })}</ul>
      </div>)}
    </div>
    {effectiveResult && <p className="mt-2 text-sm font-medium">{effectiveResult.suggested ? `Winner: ${effectiveResult.suggested.concept_name}` : "No winner selected"}</p>}
    {effectiveResult?.note && <p className="mt-1 text-xs text-slate-600">{effectiveResult.note}</p>}
    {(ranked ?? result)?.ranking_timings && <p className="mt-1 text-xs text-slate-500">
      Ranking: {Object.entries((ranked ?? result)!.ranking_timings!).map(([k, v]) =>
        `${k.replace(/_ms$/, "")} ${(v / 1000).toFixed(1)}s`
      ).join(" · ")}
    </p>}
  </section>;
}
