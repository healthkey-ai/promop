import { useRef, useState } from "react";
import InlineDestinationPicker from "./InlineDestinationPicker";
import MintConceptDialog from "./MintConceptDialog";
import { destinationError, saveMappingDestination } from "./destinationSearch";

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
  standard_concept?: string | null;
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
  canApprove?: boolean;
  vocabularies?: { vocabulary_id: string; vocabulary_name: string }[];
  domains?: { domain_id: string; label: string }[];
};

const STRATEGY_LABELS: Record<string, string> = { umls: "U", lexical: "L", vectors: "V", semantic: "V" };

const RANKER_STYLE: Record<string, string> = {
  jev: "bg-indigo-100 text-indigo-700",
  anthropic: "bg-sky-100 text-sky-700",
};

const STANDARD_BADGE: Record<string, { label: string; className: string }> = {
  C: { label: "Classification", className: "bg-slate-100 text-slate-600" },
};

type MergedCandidate = SuggestCandidate & { strategies: Set<string> };
type PickedConcept = Pick<SuggestCandidate, "concept_id" | "concept_name">;
type SavedChoice = { concept: PickedConcept; approved: boolean };

function winnerConfidence(events: CandidateActivity[], conceptId: number): number | undefined {
  const ranked = events.filter(event => event.stage === "ranked").at(-1);
  const scores = (ranked?.alternatives ?? [])
    .filter(item => item.concept_id === conceptId && Number.isFinite(item.confidence)
      && item.confidence >= 0 && item.confidence <= 1)
    .map(item => item.confidence);
  return scores.length ? Math.max(...scores) : undefined;
}

// A displayed 100% must really meet a 100% approval threshold.
function confidenceLabel(confidence: number): string {
  return `${Math.floor(confidence * 10000) / 100}%`;
}

/** Deduplicate candidates across strategy events into a single list. */
function mergeCandidates(searches: CandidateActivity[], ranked?: CandidateActivity, winner?: SuggestCandidate | null): MergedCandidate[] {
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
  if (winner && !byId.has(winner.concept_id)) {
    byId.set(winner.concept_id, { ...winner, strategies: new Set() });
  }
  return [...byId.values()];
}

export default function SuggestCandidates({ activity, finished, onSaved, canApprove = false, vocabularies = [], domains = [] }: Props) {
  const [editing, setEditing] = useState<number | null>(null);
  const [minting, setMinting] = useState<number | null>(null);
  const [needsAttentionOnly, setNeedsAttentionOnly] = useState(false);
  const [saving, setSaving] = useState<number | null>(null);
  const [choices, setChoices] = useState<Record<number, SavedChoice>>({});
  const [threshold, setThreshold] = useState("100");
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchProgress, setBatchProgress] = useState<{ done: number; total: number; approved: number } | null>(null);
  const writing = useRef(false);
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const groups = new Map<number, CandidateActivity[]>();
  for (const event of activity) {
    if (event.mapping_id == null) continue;
    const events = groups.get(event.mapping_id) ?? [];
    events.push(event);
    groups.set(event.mapping_id, events);
  }
  const remember = (mappingId: number, concept: PickedConcept, approved: boolean) => {
    setChoices(current => ({ ...current, [mappingId]: { concept, approved } }));
  };
  const expectedDestination = (mappingId: number) => choices[mappingId]?.concept.concept_id
    ?? groups.get(mappingId)?.filter(event => event.stage === "result").at(-1)?.suggested?.concept_id;
  const saveChoice = async (mappingId: number, candidate: PickedConcept, approve = false, minted = false) => {
    if (writing.current || (approve && !canApprove)) return;
    writing.current = true;
    setSaving(mappingId);
    setError("");
    try {
      await saveMappingDestination(mappingId, candidate.concept_id, approve, expectedDestination(mappingId));
      remember(mappingId, candidate, approve);
      onSaved?.();
    } catch (failure) {
      const message = minted ? "Concept minted, but could not save the mapping."
        : approve ? "Could not approve." : "Could not save the alternative candidate.";
      setError(`${message} ${destinationError(failure)}`);
    } finally {
      setSaving(null);
      writing.current = false;
    }
  };

  const approveMatching = async () => {
    if (!canApprove || writing.current || minimumConfidence === null || !matchingWinners.length) return;
    // Capture this click's matches. Newly arriving results need another click.
    const pending = [...matchingWinners];
    writing.current = true;
    setBatchRunning(true);
    setBatchProgress({ done: 0, total: pending.length, approved: 0 });
    setError("");
    let approvedCount = 0;
    const failures: string[] = [];
    try {
      for (const [index, { mappingId, winner, sourceCode }] of pending.entries()) {
        setSaving(mappingId);
        try {
          await saveMappingDestination(mappingId, winner.concept_id, true, winner.concept_id, { requireProposed: true });
          remember(mappingId, winner, true);
          approvedCount += 1;
        } catch (failure) {
          failures.push(`${sourceCode}: ${destinationError(failure)}`);
        }
        setBatchProgress({ done: index + 1, total: pending.length, approved: approvedCount });
      }
      if (failures.length) setError(`Could not approve ${failures.length} mapping(s). ${failures.join(" ")}`);
      if (approvedCount) onSaved?.();
    } finally {
      setSaving(null);
      setBatchRunning(false);
      writing.current = false;
    }
  };

  const mintAndSave = async (mappingId: number, concept: { concept_id: number; concept_name: string }) => {
    setMinting(null);
    await saveChoice(mappingId, concept, false, true);
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

  const needsAttention = (mappingId: number, events: CandidateActivity[]) => {
    if (choices[mappingId]) return false;
    const ranked = events.filter(event => event.stage === "ranked").at(-1);
    const result = events.filter(event => event.stage === "result").at(-1);
    const winner = (result ?? ranked)?.suggested;
    if (!winner) return true;
    // Use the ranker's existing "low" band, below 40%. Unknown confidence
    // does not mean low confidence; runs from older versions may omit it.
    const confidence = winnerConfidence(events, winner.concept_id);
    return confidence !== undefined && confidence < 0.4;
  };
  const attentionCount = [...groups].filter(([id, events]) => needsAttention(id, events)).length;
  const visibleGroups = [...groups].filter(([id, events]) => !needsAttentionOnly || needsAttention(id, events));
  const thresholdNumber = Number(threshold);
  const minimumConfidence = threshold.trim() !== "" && Number.isFinite(thresholdNumber)
    && thresholdNumber >= 0 && thresholdNumber <= 100 ? thresholdNumber / 100 : null;
  const matchingWinners = visibleGroups.flatMap(([mappingId, events]) => {
    const result = events.filter(event => event.stage === "result").at(-1);
    const winner = result?.suggested;
    const choice = choices[mappingId];
    if (minimumConfidence === null || !result?.updated || result.dry_run || !winner || choice?.approved
      || (choice && choice.concept.concept_id !== winner.concept_id)) return [];
    const confidence = winnerConfidence(events, winner.concept_id);
    return confidence !== undefined && confidence >= minimumConfidence
      ? [{ mappingId, winner, sourceCode: events[0].source_code || String(mappingId) }] : [];
  });
  const busy = saving !== null || batchRunning || editing !== null || minting !== null;
  const hasHkVocabularies = vocabularies.some(v => v.vocabulary_id.startsWith("HK-"));

  return <section aria-label="Suggestion candidates" className="mb-4 space-y-3 rounded-md border border-slate-200 bg-white p-4">
    <button type="button" className="flex w-full items-center justify-between text-left"
      onClick={() => setCollapsed(c => !c)} aria-expanded={!collapsed}>
      <h2 className="font-semibold">Suggestion candidates</h2>
      <span className="text-slate-400">{collapsed ? "+" : "\u2212"}</span>
    </button>
    {!collapsed && <>
      <p className="text-sm text-slate-600">Candidates appear as each search finishes. Once a mapping's result is saved, use or approve a candidate here while the rest of the batch continues.</p>
      {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
      {!groups.size && <p role="status" className="text-sm">{finished ? "No candidates recorded for this run." : "Waiting for candidates\u2026"}</p>}
      {finished && <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
        <input type="checkbox" checked={needsAttentionOnly} disabled={busy} onChange={event => { setNeedsAttentionOnly(event.target.checked); setEditing(null); }} />
        Needs attention ({attentionCount})
        <span className="font-normal text-xs text-slate-500">No destination or confidence below 40% in this run</span>
      </label>}
      {canApprove && groups.size > 0 && <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm font-medium text-slate-700">
            Minimum winner confidence (%)
            <input type="number" min="0" max="100" step="any" value={threshold} disabled={busy}
              onChange={event => setThreshold(event.target.value)}
              aria-invalid={minimumConfidence === null}
              className="ml-2 w-24 rounded border border-slate-300 bg-white px-2 py-1" />
          </label>
          <button type="button" disabled={busy || !matchingWinners.length || minimumConfidence === null}
            onClick={() => void approveMatching()}
            className="rounded border border-green-300 bg-green-50 px-3 py-1 text-sm font-medium text-green-800 disabled:opacity-50">
            Approve Matching Winners ({matchingWinners.length})
          </button>
        </div>
        <p className="text-xs text-slate-600">Approves displayed, saved Winners at or above this threshold. Uses the highest reported ranker confidence; unknown confidence and manually changed destinations are excluded. Individual approvals do not use this threshold.</p>
        {minimumConfidence === null && <p className="text-xs text-rose-700">Enter a confidence from 0 to 100.</p>}
        {batchProgress && <p role="status" className="text-sm text-slate-700">
          {batchRunning ? `Processing ${batchProgress.done} of ${batchProgress.total}. ` : ""}
          Approved {batchProgress.approved} of {batchProgress.total} matching Winners.
        </p>}
      </div>}
      {needsAttentionOnly && !visibleGroups.length && <p role="status" className="text-sm text-slate-600">No remaining mappings need attention in this run.</p>}
      {visibleGroups.map(([mappingId, events]) => {
        const source = events[0];
        const description = sourceDescription(events);
        const result = events.filter(event => event.stage === "result").at(-1);
        const ranked = events.filter(event => event.stage === "ranked").at(-1);
        const winner = (result ?? ranked)?.suggested;
        const searches = events.filter(event => event.stage === "candidates");
        const candidates = mergeCandidates(searches, ranked, winner);
        const confidences = confidenceLookup(events);
        const allStandard = candidates.every(c => c.standard_concept === undefined || c.standard_concept === "S");
        const choice = choices[mappingId];
        const canAct = !choice?.approved && !result?.dry_run && (result?.updated || !!choice);
        const canSearch = !choice?.approved && !result?.dry_run && (finished || !!result);
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
              const isWinner = candidate.concept_id === winner?.concept_id;
              const isSelected = choice?.concept.concept_id === candidate.concept_id;
              const isApproved = isSelected && choice?.approved;
              const stdBadge = !allStandard && candidate.standard_concept !== "S" && candidate.standard_concept !== undefined ? (STANDARD_BADGE[candidate.standard_concept ?? ""] ?? { label: "Non-std", className: "bg-amber-100 text-amber-700" }) : null;
              return <tr key={candidate.concept_id} className={`border-b border-slate-100 ${isWinner ? "bg-sky-50" : ""}`}>
                <td className="py-1.5 pr-2">
                  <span>{candidate.vocabulary_id}:{candidate.concept_code}</span>
                  <span className="ml-1 text-slate-600">— {candidate.concept_name}</span>
                  <span className="ml-1 text-xs text-slate-400">OMOP {candidate.concept_id}</span>
                  {stdBadge && <span className={`ml-1 rounded px-1 py-0.5 text-xs ${stdBadge.className}`}>{stdBadge.label}</span>}
                </td>
                <td className="py-1.5 px-2">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-mono" title={[...candidate.strategies].join(", ")}>{strategies}</span>
                  {candidate.vector_distance != null && <span className="ml-1 text-xs text-slate-500" title="Cosine distance">{candidate.vector_distance.toFixed(3)}</span>}
                </td>
                <td className="py-1.5 px-2">
                  {confs.map((conf, ci) => <span key={ci} className={`mr-1 rounded px-1.5 py-0.5 text-xs font-medium ${RANKER_STYLE[conf.ranker ?? ""] ?? "bg-slate-100 text-slate-700"}`}
                    title={conf.ranker ? `${conf.ranker} confidence` : "confidence"}>{confidenceLabel(conf.confidence)}{conf.ranker ? ` ${conf.ranker[0].toUpperCase()}` : ""}</span>)}
                </td>
                <td className="py-1.5 pl-2 whitespace-nowrap">
                  {isApproved
                    ? <span role="status" className="text-xs font-medium text-green-700">Approved</span>
                    : <>
                        {isWinner && <span className="font-semibold text-sky-700">Winner</span>}
                        {isSelected && !isWinner && <span role="status" className="text-xs">Selected</span>}
                        {canAct && !isWinner && !isSelected && <button
                          type="button" disabled={busy} className="rounded border px-2 py-0.5 text-xs disabled:opacity-50"
                          aria-label={`Use ${candidate.concept_name} for ${source.source_code}`}
                          onClick={() => void saveChoice(mappingId, candidate)}>{saving === mappingId ? "Saving\u2026" : "Use"}</button>}
                        {canApprove && canAct && <button
                          type="button" disabled={busy}
                          className="ml-1 rounded border border-green-300 bg-green-50 px-2 py-0.5 text-xs font-medium text-green-700 hover:bg-green-100 disabled:opacity-50"
                          aria-label={`Approve ${candidate.concept_name} for ${source.source_code}`}
                          onClick={() => void saveChoice(mappingId, candidate, true)}>{saving === mappingId ? "Saving\u2026" : "Approve"}</button>}
                      </>}
                </td>
              </tr>;
            })}</tbody>
          </table>}
          {ranked && <p className="mt-2 text-sm font-medium">{winner ? `Winner: ${winner.concept_name}` : "No destination proposed"}</p>}
          {(result ?? ranked)?.note && <p className="mt-1 text-sm text-slate-600">{(result ?? ranked)?.note}</p>}
          {choice && <div className="mt-2 flex items-center gap-2 text-sm font-medium text-green-800">
            <p role="status">Saved: {choice.concept.concept_name}{choice.approved ? " — Approved" : " — Proposed"}</p>
            {canApprove && !choice.approved && !candidates.some(candidate => candidate.concept_id === choice.concept.concept_id) && <button
              type="button" disabled={busy} aria-label={`Approve ${choice.concept.concept_name} for ${source.source_code}`}
              onClick={() => void saveChoice(mappingId, choice.concept, true)}
              className="rounded border border-green-300 bg-green-50 px-2 py-0.5 text-xs disabled:opacity-50">Approve</button>}
          </div>}
          {canSearch && <div className="mt-3 flex items-center gap-2">
            {editing === mappingId ? <InlineDestinationPicker key={mappingId} mappingId={mappingId}
              sourceLabel={`${source.source_vocabulary_id || "Uncoded"}:${source.source_code}${description ? ` — ${description}` : ""}`}
              canApprove={canApprove} vocabularies={vocabularies}
              onCancel={() => setEditing(null)} onSaved={(saved, concept) => {
                remember(mappingId, concept, saved.status === "approved");
                setEditing(null);
                onSaved?.();
              }} /> : <>
                <button type="button" disabled={busy}
                  aria-label={`Search destination for ${source.source_code}`}
                  onClick={() => setEditing(mappingId)} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
                  Search for another destination
                </button>
                {hasHkVocabularies && domains.length > 0 && <button type="button" disabled={busy}
                  aria-label={`Mint new concept for ${source.source_code}`}
                  onClick={() => setMinting(mappingId)} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
                  Mint new concept
                </button>}
              </>}
          </div>}
          {ranked?.ranking_timings && <p className="mt-1 text-xs text-slate-500">
            Ranking: {Object.entries(ranked.ranking_timings).map(([k, v]) =>
              `${k.replace(/_ms$/, "")} ${(v / 1000).toFixed(1)}s`
            ).join(" · ")}
          </p>}
          {minting === mappingId && <MintConceptDialog
            vocabularies={vocabularies} domains={domains}
            initialDomain="" initialName={description || source.source_code || ""}
            sourceCode={source.source_code || ""} sourceVocabulary={source.source_vocabulary_id || ""}
            onClose={() => setMinting(null)}
            onSelect={concept => void mintAndSave(mappingId, concept)} />}
        </article>;
      })}
    </>}
  </section>;
}
