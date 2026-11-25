import { useEffect, useState } from "react";
import api from "@/api/axios";
import { errorText } from "./answerConcept";

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

interface LookupAnswer {
  source: LookupSource | null;
  options: { code: string | number; title: string }[];
}

interface Props {
  fieldName: string;
  /** How many answers have been added to this field in the editor, retired ones included. */
  answers: number;
}

/** The controlled lookup a field's dropdown is drawn from, shown read only (#1225).
 *
 *  The editor lists it and never copies it: the entries are managed where the
 *  lookup is loaded, so there is no second list to drift from the first. What
 *  matters to a curator is what the answers below do beside it. */
export function LookupOptions({ fieldName, answers }: Props) {
  const [data, setData] = useState<LookupAnswer | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setError("");
      setData(null);
      try {
        const resp = await api.get("/v1/field-choices/lookup/", { params: { field_name: fieldName } });
        if (!cancelled) setData(resp.data);
      } catch (e) {
        if (!cancelled) setError(errorText(e, "The lookup could not be loaded."));
      }
    })();
    return () => { cancelled = true; };
  }, [fieldName]);

  if (error) return <div role="alert" className="mb-3 text-xs text-red-600">{error}</div>;
  if (!data?.source) return null;
  const { source, options } = data;

  return (
    <section aria-label="Lookup options" className="mb-3 rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-600">
      <div>
        This field's options come from the <span className="font-mono">{source.label}</span> lookup
        ({options.length} {options.length === 1 ? "entry" : "entries"}). It is managed where it is loaded and is
        shown here read only.
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
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}
        className="mt-1 rounded px-1 py-0.5 text-gray-500 underline hover:text-gray-700">
        {open ? "Hide entries" : "Show entries"}
      </button>
      {open && (
        <ul className="mt-1 max-h-32 overflow-y-auto rounded border border-gray-200 bg-white">
          {options.map((o) => (
            <li key={String(o.code)} className="flex justify-between gap-2 px-2 py-0.5">
              <span>{o.title}</span>
              <span className="font-mono text-gray-400">{String(o.code)}</span>
            </li>
          ))}
          {options.length === 0 && <li className="px-2 py-1 text-gray-500">The lookup has no entries loaded.</li>}
        </ul>
      )}
    </section>
  );
}
