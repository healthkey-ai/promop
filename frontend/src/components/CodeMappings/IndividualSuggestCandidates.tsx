import type { CandidateActivity, SuggestCandidate } from "./SuggestCandidates";

const STRATEGY_LABELS: Record<string, string> = { umls: "U", lexical: "L", vectors: "V", semantic: "V", additional: "+" };

const RANKER_STYLE: Record<string, string> = {
  jev: "bg-indigo-100 text-indigo-700",
  anthropic: "bg-sky-100 text-sky-700",
};

type MergedCandidate = SuggestCandidate & { strategies: Set<string> };

function mergeCandidates(searches: CandidateActivity[], effectiveResult?: CandidateActivity): MergedCandidate[] {
  const byId = new Map<number, MergedCandidate>();
  for (const event of searches) {
    for (const c of event.candidates ?? []) {
      const existing = byId.get(c.concept_id);
      if (existing) {
        existing.strategies.add(event.strategy || "");
        if (c.umls_score != null && existing.umls_score == null) existing.umls_score = c.umls_score;
        if (c.semantic_score != null && existing.semantic_score == null) existing.semantic_score = c.semantic_score;
        if (c.vector_distance != null && existing.vector_distance == null) existing.vector_distance = c.vector_distance;
      } else {
        byId.set(c.concept_id, { ...c, strategies: new Set([event.strategy || ""]) });
      }
    }
  }
  for (const c of effectiveResult?.candidates ?? []) {
    if (!byId.has(c.concept_id)) {
      byId.set(c.concept_id, { ...c, strategies: new Set(["additional"]) });
    }
  }
  return [...byId.values()];
}

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
  const candidates = mergeCandidates(searches, effectiveResult);

  // Build a confidence lookup from the ranked/result event's alternatives.
  const confidences = new Map<number, { confidence: number; ranker?: string }[]>();
  for (const alt of effectiveResult?.alternatives ?? []) {
    const existing = confidences.get(alt.concept_id) ?? [];
    existing.push({ confidence: alt.confidence, ranker: alt.ranker });
    confidences.set(alt.concept_id, existing);
  }

  return <section aria-label="Individual suggestion candidates" className="mb-3 rounded-md border border-sky-200 bg-sky-50 p-3">
    <h3 className="text-sm font-semibold">Suggestion candidates</h3>
    <p role="status" className="mt-1 text-xs text-slate-600">
      {running ? "Searching\u2026 Candidates appear as each search finishes. You can choose one now." : "Choose a candidate to fill the destination, then Save your mapping."}
    </p>
    <div className="mt-2 max-h-64 overflow-y-auto">
      {!candidates.length && <p className="text-xs text-slate-500">No matches</p>}
      {candidates.length > 0 && <table className="w-full text-left text-xs">
        <thead><tr className="border-b text-slate-500">
          <th className="pb-1 pr-1 font-medium">Candidate</th>
          <th className="pb-1 px-1 font-medium" title="U=UMLS, L=Lexical, V=Vectors">Via</th>
          <th className="pb-1 px-1 font-medium">Conf</th>
          <th className="pb-1 pl-1 font-medium"></th>
        </tr></thead>
        <tbody>{candidates.map(candidate => {
          const confs = confidences.get(candidate.concept_id) ?? [];
          const strategies = [...candidate.strategies].map(s => STRATEGY_LABELS[s] || s).filter(Boolean).join("");
          const isWinner = effectiveResult?.suggested?.concept_id === candidate.concept_id;
          const isSelected = selectedId === String(candidate.concept_id);
          return <tr key={candidate.concept_id} className={`border-b border-slate-100 cursor-pointer hover:bg-sky-100 ${isSelected ? "bg-sky-100 ring-1 ring-sky-600" : isWinner ? "bg-sky-50" : ""}`}
            onClick={() => onSelect(candidate)}>
            <td className="py-1 pr-1">
              <span className="text-sm">{candidate.concept_name}</span>
              <span className="ml-1 text-slate-500">{candidate.vocabulary_id}:{candidate.concept_code}</span>
              <span className="ml-1 text-slate-400">OMOP {candidate.concept_id}</span>
            </td>
            <td className="py-1 px-1">
              <span className="rounded bg-slate-100 px-1 py-0.5 font-mono" title={[...candidate.strategies].join(", ")}>{strategies}</span>
              {candidate.vector_distance != null && <span className="ml-1 text-slate-500" title="Cosine distance">{candidate.vector_distance.toFixed(3)}</span>}
            </td>
            <td className="py-1 px-1">
              {confs.map((conf, ci) => <span key={ci} className={`mr-0.5 rounded px-1 py-0.5 font-medium ${RANKER_STYLE[conf.ranker ?? ""] ?? "bg-slate-100 text-slate-700"}`}
                title={conf.ranker ? `${conf.ranker} confidence` : "confidence"}>{Math.round(conf.confidence * 100)}%{conf.ranker ? ` ${conf.ranker[0].toUpperCase()}` : ""}</span>)}
            </td>
            <td className="py-1 pl-1 whitespace-nowrap">
              {isWinner && <strong className="text-sky-700">Winner</strong>}
              {isSelected && !isWinner && <span className="text-sky-600">Selected</span>}
            </td>
          </tr>;
        })}</tbody>
      </table>}
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
