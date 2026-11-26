import { useState } from "react";
import api from "@/api/axios";
import { answerState, errorText, type ConceptBrief, type ValueMapping } from "./answerConcept";

interface Rules {
  required_domain: string | null;
  excluded_domains: string[];
  excluded_vocabularies: string[];
}

interface Props {
  choiceId: number;
  choiceLabel: string;
  mappings: ValueMapping[] | undefined;
  canApprove: boolean;
  /** Called after any change, so the list is read again. */
  onChanged: () => Promise<void> | void;
}

const BASE = (choiceId: number) => `/v1/field-choices/${choiceId}/value-mappings/`;

/** The concept an answer stands for: the decision in force, what is waiting to
 *  replace it, and the controls to propose, approve or reject (#1225).
 *
 *  What may be proposed is whatever the server's search lists, so a curator is
 *  never offered a concept the proposal would refuse; approving is for org
 *  admins and staff. */
export function AnswerConceptPanel({ choiceId, choiceLabel, mappings, canApprove, onChanged }: Props) {
  const state = answerState(mappings);
  const [mode, setMode] = useState<"" | "search" | "none">("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ConceptBrief[] | null>(null);
  const [rules, setRules] = useState<Rules | null>(null);
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const reset = () => {
    setMode("");
    setQuery("");
    setResults(null);
    setRules(null);
    setRationale("");
  };

  // A decision that fails may have lost a race with another curator: read the
  // list again so the buttons and summary are not the stale ones.
  const run = async (action: () => Promise<unknown>, failure: string, refreshOnFailure = false) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await action();
      reset();
      await onChanged();
    } catch (e) {
      setError(errorText(e, failure));
      if (refreshOnFailure) {
        try { await onChanged(); } catch { /* the error above stands */ }
      }
    } finally {
      setBusy(false);
    }
  };

  const [total, setTotal] = useState(0);

  const search = async () => {
    if (busy) return;
    if (query.trim().length < 3) {
      setError("Type at least 3 characters of a name, a code or a concept id.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const resp = await api.get(`${BASE(choiceId)}targets/`, {
        params: { q: query.trim(), role: "value_as_concept" },
      });
      setResults(resp.data.results);
      setTotal(resp.data.count ?? resp.data.results.length);
      setRules(resp.data.rules);
    } catch (e) {
      setError(errorText(e, "The concept search failed."));
    } finally {
      setBusy(false);
    }
  };

  const propose = (concept: ConceptBrief) =>
    run(
      () => api.post(BASE(choiceId), { role: "value_as_concept", concept_id: concept.concept_id, rationale }),
      "The proposal was not saved.",
    );

  const proposeNone = () =>
    run(
      () => api.post(BASE(choiceId), { role: "value_as_concept", outcome: "no_equivalent", rationale }),
      "The proposal was not saved.",
    );

  const decide = (mapping: ValueMapping, action: "approve" | "reject") =>
    run(() => api.post(`${BASE(choiceId)}${mapping.id}/${action}/`, {}), `The ${action} was not saved.`, true);

  const tone =
    state.kind === "approved" || state.kind === "approved_no_equivalent"
      ? "text-green-700"
      : state.kind === "approved_invalid"
        ? "text-red-700"
        : state.kind === "proposed"
          ? "text-amber-700"
          : "text-gray-500";

  return (
    <div role="group" className="mt-2 rounded bg-gray-50 p-2 text-xs" aria-label={`Concept for ${choiceLabel}`}>
      <div className={tone}>{state.summary}</div>
      {state.warning && <div className="mt-0.5 text-red-600">{state.warning}</div>}
      {state.approved?.outcome === "no_equivalent" && state.approved.rationale && (
        <div className="mt-0.5 text-gray-500">Reason: {state.approved.rationale}</div>
      )}
      {state.approved && state.proposed && state.kind !== "proposed" && (
        <div className="mt-0.5 text-amber-700">
          Waiting to replace it: {state.proposed.target
            ? `${state.proposed.target.vocabulary_id} ${state.proposed.target.concept_code} ${state.proposed.target.concept_name}`
            : "no standard concept"}
        </div>
      )}
      {error && <div role="alert" className="mt-1 text-red-600">{error}</div>}

      <div className="mt-1 flex flex-wrap gap-1">
        {state.proposed && canApprove && (
          <>
            <button type="button"
              disabled={busy || (state.proposed.outcome === "mapped" && !state.proposed.target)}
              onClick={() => decide(state.proposed!, "approve")}
              aria-label={`Approve proposal for ${choiceLabel}`}
              className="rounded bg-primary px-2 py-0.5 text-primary-foreground disabled:opacity-50">
              Approve
            </button>
            <button type="button" disabled={busy} onClick={() => decide(state.proposed!, "reject")}
              aria-label={`Reject proposal for ${choiceLabel}`}
              className="rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
              Reject
            </button>
          </>
        )}
        {state.approved && canApprove && (
          <button type="button" disabled={busy}
            onClick={() => {
              if (window.confirm(`Withdraw the approved concept for "${choiceLabel}"? Its value will be saved without a concept.`)) {
                void decide(state.approved!, "reject");
              }
            }}
            aria-label={`Withdraw approval for ${choiceLabel}`}
            className="rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
            Withdraw approval
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => setMode(mode === "search" ? "" : "search")}
          aria-label={`Map ${choiceLabel} to a concept`}
          className="rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
          {state.approved || state.proposed ? "Propose another concept" : "Map to a concept"}
        </button>
        <button type="button" disabled={busy} onClick={() => setMode(mode === "none" ? "" : "none")}
          aria-label={`Record no equivalent for ${choiceLabel}`}
          className="rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
          No standard equivalent
        </button>
      </div>

      {mode !== "" && (
        <div className="mt-2 space-y-1">
          <input
            type="text"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder={mode === "none" ? "Why no standard concept fits (required)" : "Why this concept (optional)"}
            aria-label="Rationale"
            className="h-7 w-full rounded border border-gray-300 px-2"
          />
          {mode === "none" && (
            <button type="button" disabled={busy || !rationale.trim()} onClick={proposeNone}
              className="rounded bg-primary px-2 py-0.5 text-primary-foreground disabled:opacity-50">
              Propose no equivalent
            </button>
          )}
          {mode === "search" && (
            <>
              <div className="flex gap-1">
                <input
                  type="text"
                  value={query}
                  onChange={(e) => { setQuery(e.target.value); setResults(null); }}
                  onKeyDown={(e) => { if (e.key === "Enter") void search(); }}
                  placeholder="Concept name, code or id"
                  aria-label="Concept search"
                  className="h-7 flex-1 rounded border border-gray-300 px-2"
                />
                <button type="button" disabled={busy} onClick={search}
                  className="rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
                  Search
                </button>
              </div>
              {rules && (
                <div className="text-[10px] text-gray-400">
                  Only current standard concepts are listed
                  {rules.required_domain ? `, in the ${rules.required_domain} domain` : ""}.
                </div>
              )}
              {results && results.length === 0 && (
                <div role="status" className="text-gray-500">No concept that can be proposed matches.</div>
              )}
              {results && results.length > 0 && (
                <>
                {total > results.length && (
                  <div className="text-[10px] text-gray-400">
                    Showing the first {results.length} of {total}. Refine your search to narrow them.
                  </div>
                )}
                <ul aria-live="polite" className="max-h-40 divide-y overflow-y-auto rounded border border-gray-200 bg-white">
                  {results.map((c) => (
                    <li key={c.concept_id} className="flex items-center justify-between gap-2 px-2 py-1">
                      <span className="min-w-0">
                        <span className="font-medium">{c.concept_name}</span>{" "}
                        <span className="text-gray-500">{c.vocabulary_id} {c.concept_code} · {c.domain_id}</span>
                      </span>
                      <button type="button" disabled={busy} onClick={() => propose(c)}
                        aria-label={`Propose ${c.concept_name} for ${choiceLabel}`}
                        className="shrink-0 rounded bg-primary px-2 py-0.5 text-primary-foreground disabled:opacity-50">
                        Propose
                      </button>
                    </li>
                  ))}
                </ul>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
