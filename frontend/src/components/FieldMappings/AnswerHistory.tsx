import { useEffect, useRef, useState } from "react";
import api from "@/api/axios";
import {
  actionText, errorText, newestFirst, revisionConcept,
  type HistoryEntry, type MappingRevision, type ValueMapping,
} from "./answerConcept";

interface Props {
  choiceId: number;
  choiceLabel: string;
  /** Changes when the answer's decisions do, so an open history is read again. */
  signature?: string;
}

const BASE = (choiceId: number) => `/v1/field-choices/${choiceId}/value-mappings/`;

/** Every proposal, approval and rejection of this answer's concept, newest first.
 *
 *  Read only and loaded when asked for: the list carries the decision in force,
 *  and the trail is a request per decision, so it is not fetched for every
 *  answer of a field. */
export function AnswerHistory({ choiceId, choiceLabel, signature = "" }: Props) {
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = async (force = false) => {
    if (busy && !force) return;
    setBusy(true);
    setError("");
    try {
      const all: ValueMapping[] = (await api.get(BASE(choiceId), { params: { history: 1 } })).data;
      const own = all.filter((m) => m.role === "value_as_concept");
      const lists = await Promise.all(own.map(async (m) => {
        const rows: MappingRevision[] = (await api.get(`${BASE(choiceId)}${m.id}/revisions/`)).data;
        return rows.map((r) => ({ ...r, mapping_id: m.id }));
      }));
      setEntries(newestFirst(lists.flat()));
    } catch (e) {
      setError(errorText(e, "The history could not be loaded."));
    } finally {
      setBusy(false);
    }
  };

  // A proposal, approval or rejection made while the history is open adds a row.
  const seen = useRef(signature);
  useEffect(() => {
    if (seen.current === signature) return;
    seen.current = signature;
    if (entries === null) return;
    (async () => {
      await load(true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  if (entries === null) {
    return (
      <div className="mt-1">
        <button type="button" disabled={busy} onClick={() => load()}
          aria-label={`Show the history of ${choiceLabel}'s concept`}
          className="rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700 disabled:opacity-50">
          {busy ? "Loading..." : "History"}
        </button>
        {error && <div role="alert" className="text-red-600">{error}</div>}
      </div>
    );
  }
  return (
    <div className="mt-1" role="region" aria-label={`History of ${choiceLabel}'s concept`}>
      <div className="flex items-center gap-2">
        <span className="font-medium text-gray-600">History</span>
        <button type="button" onClick={() => setEntries(null)}
          className="rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700">
          Hide
        </button>
      </div>
      {entries.length === 0 ? (
        <div className="text-gray-500">Nothing has been proposed for this answer.</div>
      ) : (
        <ol className="mt-0.5 space-y-0.5">
          {entries.map((r) => (
            <li key={`${r.mapping_id}-${r.revision}`} className="text-gray-600">
              <span className="text-gray-400">{r.created_at.slice(0, 10)}</span>{" "}
              <span className="font-medium">{actionText(r.action)}</span>{" "}
              {revisionConcept(r)}
              {r.changed_by ? ` by ${r.changed_by}` : ""}
              {r.vocabulary_release ? ` (vocabulary ${r.vocabulary_release})` : ""}
              {r.rationale ? <span className="text-gray-500"> — {r.rationale}</span> : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
