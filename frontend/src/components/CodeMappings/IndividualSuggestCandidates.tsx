import type { CandidateActivity, SuggestCandidate } from "./SuggestCandidates";

const labels: Record<string, string> = { umls: "UMLS", lexical: "Lexical", semantic: "Semantic", additional: "Additional candidates" };

export default function IndividualSuggestCandidates({ activity, running, selectedId, onSelect }: {
  activity: CandidateActivity[];
  running: boolean;
  selectedId: string;
  onSelect: (candidate: SuggestCandidate) => void;
}) {
  const result = activity.filter(event => event.stage === "result").at(-1);
  const searches = activity.filter(event => event.stage === "candidates");
  const seen = new Set(searches.flatMap(event => (event.candidates ?? []).map(candidate => candidate.concept_id)));
  const additional = result?.candidates?.filter(candidate => !seen.has(candidate.concept_id)) ?? [];
  const lists = [...searches, ...(additional.length ? [{ strategy: "additional", candidates: additional }] : [])];
  return <section aria-label="Individual suggestion candidates" className="mb-3 rounded-md border border-sky-200 bg-sky-50 p-3">
    <h3 className="text-sm font-semibold">Suggestion candidates</h3>
    <p role="status" className="mt-1 text-xs text-slate-600">
      {running ? "Searching… Candidates appear as each search finishes. You can choose one now." : "Choose a candidate to fill the destination, then Save your mapping."}
    </p>
    <div className="mt-2 max-h-64 space-y-2 overflow-y-auto">
      {lists.map((event, index) => <div key={index}>
        <h4 className="text-xs font-semibold">{labels[event.strategy || ""] || event.strategy}</h4>
        {!event.candidates?.length && <p className="text-xs text-slate-500">No matches</p>}
        <ul className="space-y-1">{event.candidates?.map(candidate => <li key={candidate.concept_id}>
          <button type="button" aria-pressed={selectedId === String(candidate.concept_id)}
            onClick={() => onSelect(candidate)}
            className="w-full rounded border border-slate-200 bg-white px-2 py-1 text-left text-sm hover:bg-sky-100 aria-pressed:border-sky-600">
            {candidate.concept_name} — {candidate.vocabulary_id}:{candidate.concept_code} (OMOP {candidate.concept_id})
            {candidate.vector_distance != null && <span className="ml-2 text-xs" title="Cosine distance; lower is closer">Distance {candidate.vector_distance.toFixed(4)}</span>}
            {result?.suggested?.concept_id === candidate.concept_id && <strong className="ml-2">Winner</strong>}
            {selectedId === String(candidate.concept_id) && <span className="ml-2 text-xs">Selected</span>}
          </button>
        </li>)}</ul>
      </div>)}
    </div>
    {result && <p className="mt-2 text-sm font-medium">{result.suggested ? `Winner: ${result.suggested.concept_name}` : "No winner selected"}</p>}
    {result?.note && <p className="mt-1 text-xs text-slate-600">{result.note}</p>}
  </section>;
}
