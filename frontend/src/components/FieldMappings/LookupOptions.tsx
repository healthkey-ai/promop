import { useCallback, useEffect, useState } from "react";
import api from "@/api/axios";
import { AnswerConceptPanel } from "./AnswerConceptPanel";
import { entryStatus, errorText, lookupCoverage, type ValueMapping } from "./answerConcept";

interface LookupSource {
  kind: "vocabulary";
  /** The slug of `/vocabularies/<name>/`. */
  name: string;
  /** The lookup table. */
  label: string;
  /** The field's approved mapping names it, so only these entries are offered. */
  enforced: boolean;
  source_name: string;
  source_url: string;
}

interface Entry {
  code: string | number;
  title: string;
  /** The row that holds decisions about this entry; null until one is made. */
  choice_id: number | null;
  value_mappings: ValueMapping[];
}

interface LookupAnswer {
  source: LookupSource | null;
  options: Entry[];
  /** Decisions about an entry the lookup no longer has. */
  orphaned: { choice_id: number; title: string; value_mappings: ValueMapping[] }[];
}

interface Props {
  fieldName: string;
  /** How many answers have been added to this field in the editor, retired ones included. */
  answers: number;
  /** Org admins and staff approve or reject a proposed concept. */
  canApprove?: boolean;
}

/** The controlled lookup a field's dropdown is drawn from, and the concept each
 *  entry stands for (#1225).
 *
 *  The entries are listed and never copied: they are managed where the lookup is
 *  loaded. A concept decision about one is held by a row made when a curator
 *  first decides something for the entry, which is not an answer and is never
 *  offered; proposing and approving use the same panel as an answer's. */
export function LookupOptions({ fieldName, answers, canApprove = false }: Props) {
  const [data, setData] = useState<LookupAnswer | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  // Entries whose concept panel is open, by code, with the row once it exists.
  const [panels, setPanels] = useState<Record<string, number | "making">>({});
  const [entryError, setEntryError] = useState("");

  const load = useCallback(async (): Promise<void> => {
    try {
      const resp = await api.get("/v1/field-choices/lookup/", { params: { field_name: fieldName } });
      setData(resp.data);
    } catch (e) {
      setError(errorText(e, "The lookup could not be loaded."));
    }
  }, [fieldName]);

  useEffect(() => {
    (async () => {
      setError("");
      setData(null);
      setPanels({});
      await load();
    })();
  }, [load]);

  const togglePanel = async (entry: Entry) => {
    const key = String(entry.code);
    if (panels[key] !== undefined) {
      setPanels(({ [key]: _closed, ...rest }) => rest);
      return;
    }
    setEntryError("");
    if (entry.choice_id !== null) {
      setPanels((p) => ({ ...p, [key]: entry.choice_id as number }));
      return;
    }
    setPanels((p) => ({ ...p, [key]: "making" }));
    try {
      const resp = await api.post("/v1/field-choices/lookup/answer/", { field_name: fieldName, code: String(entry.code) });
      setPanels((p) => ({ ...p, [key]: resp.data.choice_id }));
      await load();
    } catch (e) {
      setPanels(({ [key]: _failed, ...rest }) => rest);
      setEntryError(errorText(e, "Could not start a decision for this entry."));
    }
  };

  if (error) return <div role="alert" className="mb-3 text-xs text-red-600">{error}</div>;
  if (!data?.source) return null;
  const { source } = data;
  const options = data.options ?? [];
  const orphaned = data.orphaned ?? [];
  const coverage = lookupCoverage(options);

  return (
    <section aria-label="Lookup options" className="mb-3 rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-600">
      <div>
        This field's options come from the <span className="font-mono">{source.label}</span> lookup
        ({options.length} {options.length === 1 ? "entry" : "entries"}). It is managed where it is loaded and is
        shown here read only; what each entry means as a concept can be decided here.
        {source.source_url && (
          <>{" "}Source: <a href={source.source_url} target="_blank" rel="noreferrer" className="underline">{source.source_name || source.source_url}</a>.</>
        )}
      </div>
      <div className={`mt-1 ${source.enforced ? "text-amber-700" : ""}`}>
        {source.enforced
          ? "The field's approved mapping names this lookup, so only these entries are offered and answers added below are not."
          : "The tab draws its dropdown from this lookup. Once the field has an approved concept mapping and answers below, those are offered instead of these."}
        {!source.enforced && answers > 0 && " This field has answers below."}
      </div>
      {options.length > 0 && (
        <div role="status" className="mt-1">
          {coverage.decided} of {options.length} {options.length === 1 ? "entry has" : "entries have"} a concept decided
          {coverage.proposed > 0 && `, ${coverage.proposed} waiting for approval`}
          {coverage.attention > 0 && `, ${coverage.attention} with an approved concept that cannot be used`}.
        </div>
      )}
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="mt-1 rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700">
        {open ? "Hide entries" : "Show entries"}
      </button>
      {open && (
        <>
          {entryError && <div role="alert" className="mt-1 text-red-600">{entryError}</div>}
          <ul className="mt-1 max-h-96 divide-y overflow-y-auto rounded border border-gray-200 bg-white">
            {options.map((o) => {
              const key = String(o.code);
              const panel = panels[key];
              return (
                <li key={key} className="px-2 py-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0">
                      <span className="font-medium">{o.title}</span>{" "}
                      <span className="font-mono text-gray-400">{key}</span>
                      <span className="block text-gray-500">{entryStatus(o.value_mappings)}</span>
                    </span>
                    <button type="button" aria-expanded={panel !== undefined} disabled={panel === "making"}
                      onClick={() => togglePanel(o)} aria-label={`Concept for ${o.title}`}
                      className="shrink-0 rounded border border-gray-300 px-2 py-0.5 text-gray-700 disabled:opacity-50">
                      {panel === undefined ? "Concept" : panel === "making" ? "Starting..." : "Close"}
                    </button>
                  </div>
                  {typeof panel === "number" && (
                    <AnswerConceptPanel
                      choiceId={panel}
                      choiceLabel={o.title}
                      mappings={o.value_mappings}
                      canApprove={canApprove}
                      onChanged={load}
                    />
                  )}
                </li>
              );
            })}
            {options.length === 0 && <li className="px-2 py-1 text-gray-500">The lookup has no entries loaded.</li>}
          </ul>
          {orphaned.length > 0 && (
            <div className="mt-1">
              <div className="font-medium text-amber-700">No longer in the lookup</div>
              <ul>
                {orphaned.map((o) => (
                  <li key={o.choice_id}>{o.title}: {entryStatus(o.value_mappings)}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}
