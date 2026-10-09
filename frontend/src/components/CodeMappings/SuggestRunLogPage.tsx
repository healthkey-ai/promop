import PageTitle from '@/components/Branding/PageTitle';
import type { SuggestCandidate } from "./SuggestCandidates";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import api from "@/api/axios";

type Source = {
  mapping_id: number;
  source_code: string;
  source_vocabulary_id: string;
  source_description?: string;
  source_code_description?: string;
  occurrences: number;
  omop_table?: string;
};
type Activity = Partial<Source> & {
  at: string;
  stage: string;
  sources?: Source[];
  note?: string;
  suggested?: { concept_id: number; concept_name: string; vocabulary_id: string; concept_code: string } | null;
  candidates_considered?: number;
  candidates?: SuggestCandidate[];
  strategy?: string;
  strategy_used?: string | null;
  vector_reranked?: boolean;
  umls_cui?: string | null;
  updated?: boolean;
  dry_run?: boolean;
};
type RunLog = {
  run_id: string;
  state: "queued" | "running" | "success" | "failure";
  done: number;
  total: number;
  error: string;
  selection: {
    order?: string;
    eligibility?: string;
    limit?: number;
    strategies?: string[];
    resuggest?: boolean;
    dry_run?: boolean;
    model_version?: string;
    source_vocabulary_id?: string | null;
  };
  activity: Activity[];
};

const labels: Record<string, string> = {
  selected: "Selected codes, in processing order",
  retrieving: "Searching for candidates",
  retrieved: "Candidate search complete",
  candidates: "Retrieved candidates",
  ranking: "Ranking candidates",
  ranked: "Ranking decision",
  result: "Result",
  completed: "Done",
  failure: "Failed",
};

export default function SuggestRunLogPage() {
  const { runId } = useParams();
  const [run, setRun] = useState<RunLog | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const { data } = await api.get<RunLog>(
          `/v1/code-mappings/suggest-runs/${runId}/`, { params: { include_activity: "1" } },
        );
        if (cancelled) return;
        setRun(data);
        setError("");
        if (data.state === "queued" || data.state === "running") timer = setTimeout(poll, 1000);
      } catch {
        if (!cancelled) setError("Could not refresh the run log. Your last loaded entries are still shown.");
      }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [runId, retry]);

  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-5xl">
    <Link to="/code-mappings" className="text-sm text-slate-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Suggestion run log</PageTitle>
    <p className="mt-1 break-all text-xs text-slate-500">Run {runId}</p>
    {error && <div role="alert" className="mt-4 text-sm text-rose-700">{error} <button
      className="underline" onClick={() => setRetry(value => value + 1)}>Retry</button></div>}
    {!run && !error && <p className="mt-4 text-sm">Loading run log…</p>}
    {run && <>
      <section className="mt-5 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">How the first {run.selection?.limit ?? run.total} codes are chosen</h2>
        <p className="mt-2 text-sm">{run.selection?.order || "Selection order was not recorded for this older run."}</p>
        {run.selection?.eligibility && <p className="mt-2 text-sm text-slate-600">{run.selection.eligibility}</p>}
        {run.selection?.strategies && <p className="mt-2 text-sm text-slate-600">
          Source: {run.selection.source_vocabulary_id ?? "All vocabularies"}{run.selection.source_vocabulary_id === "" && "Uncoded"}
          {" · "}Using {run.selection.strategies.join(", ")}{" · "}Model {run.selection.model_version}
          {" · "}{run.selection.resuggest ? "Replace current suggestions enabled" : "Only codes without destinations"}
          {run.selection.dry_run && " · Preview only; no mappings will be written"}
        </p>}
      </section>
      <p role="status" className="my-4 text-sm font-medium">
        {run.state === "success" ? "Done" : run.state === "failure" ? "Failed" : run.state === "queued" ? "Queued" : "Running"}
        {" · "}{run.done}/{run.total} codes processed
        {(run.state === "queued" || run.state === "running") && " · Updates automatically"}
      </p>
      {run.error && <p className="mb-4 text-sm text-rose-700">{run.error}</p>}
      {!run.activity?.length && <p className="text-sm text-slate-600">
        {run.state === "queued" ? "Waiting for the worker to select codes." : "No detailed activity was recorded for this run."}
      </p>}
      <ol className="space-y-3">
        {(run.activity || []).map((event, index) => <li key={index} className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <div className="flex justify-between gap-4"><h2 className="font-semibold">{labels[event.stage] || event.stage}</h2>
            <time dateTime={event.at} className="shrink-0 text-xs text-slate-500">{new Date(event.at).toLocaleTimeString()}</time></div>
          {event.sources && <ol className="mt-2 list-inside list-decimal space-y-1">
            {event.sources.map(source => <li key={source.mapping_id}>
              {source.source_vocabulary_id || "Uncoded"}:{source.source_code}{" · "}Seen {source.occurrences}
              {source.omop_table && ` · ${source.omop_table}`}
              {source.source_description && ` — ${source.source_description}`}
            </li>)}
          </ol>}
          {event.source_code && <p className="mt-2 font-medium">{event.source_vocabulary_id || "Uncoded"}:{event.source_code}
            {" · "}Seen {event.occurrences}
            {(event.source_description || event.source_code_description) && ` — ${event.source_description || event.source_code_description}`}</p>}
          {event.candidates && <div className="mt-2">
            {event.strategy && <p className="font-medium">{event.strategy === "umls" ? "UMLS" : event.strategy}</p>}
            {!event.candidates.length && <p>No matches</p>}
            <ul className="list-inside list-disc">{event.candidates.map(candidate => <li key={candidate.concept_id}>
              {candidate.vocabulary_id}:{candidate.concept_code} — {candidate.concept_name} (OMOP {candidate.concept_id})
              {candidate.vector_distance != null && ` · Vector distance ${candidate.vector_distance.toFixed(4)}`}
            </li>)}</ul>
          </div>}
          {event.candidates_considered !== undefined && <p className="mt-1 text-slate-600">{event.candidates_considered} candidate(s)
            {event.umls_cui && ` · UMLS ${event.umls_cui}`}{event.vector_reranked && " · Vector reranked"}</p>}
          {(event.stage === "ranked" || event.stage === "result") && <p className="mt-2">
            {event.suggested
              ? `Chosen destination: ${event.suggested.vocabulary_id}:${event.suggested.concept_code} — ${event.suggested.concept_name} (OMOP ${event.suggested.concept_id})`
              : "No destination proposed"}
            {event.strategy_used && ` · ${event.strategy_used}`}
          </p>}
          {event.note && <p className="mt-1 whitespace-pre-wrap text-slate-600">{event.note}</p>}
          {event.stage === "result" && <p className="mt-1 text-xs text-slate-500">
            {event.dry_run ? "Preview only — not saved" : event.updated ? (event.suggested ? "Destination saved" : "Attempt recorded; destination unchanged") : "No change saved"}
          </p>}
        </li>)}
      </ol>
    </>}
  </main></div>;
}
