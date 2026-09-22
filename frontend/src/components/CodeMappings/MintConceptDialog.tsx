import { useEffect, useRef, useState } from "react";
import api from "@/api/axios";
import { searchDestinationConcepts, type DestinationConcept } from "./destinationSearch";

interface Concept {
  concept_id: number; concept_name: string; concept_code: string;
  vocabulary_id: string; domain_id: string; concept_class_id: string;
  standard_concept: string | null;
}
interface Props {
  vocabularies: { vocabulary_id: string; vocabulary_name: string }[];
  domains: { domain_id: string; label: string }[];
  initialDomain: string;
  initialName: string;
  initialVocabulary?: string;
  initialCode?: string;
  sourceCode: string;
  sourceVocabulary: string;
  onSelect: (concept: Concept) => void;
  onClose: () => void;
}

const PARENT_SEARCH_DEBOUNCE_MS = 300;

export default function MintConceptDialog(props: Props) {
  const [fields, setFields] = useState({ vocabulary_id: props.initialVocabulary || "", concept_name: props.initialName,
    concept_code: props.initialCode || "", domain_id: props.initialDomain });
  const [review, setReview] = useState<{ candidates: Concept[]; review_token: string } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Parent concept search state
  const [parentQuery, setParentQuery] = useState("");
  const [parentResults, setParentResults] = useState<DestinationConcept[]>([]);
  const [parentConcept, setParentConcept] = useState<DestinationConcept | null>(null);
  const [searchingParent, setSearchingParent] = useState(false);
  const parentTimerRef = useRef<number>(0);

  const groups = props.vocabularies.filter(v => v.vocabulary_id.startsWith("HK-"));
  const update = (key: keyof typeof fields, value: string) => {
    setFields(prev => ({ ...prev, [key]: value }));
    setReview(null); setConfirmed(false); setError("");
    if (key === "domain_id") { setParentConcept(null); setParentQuery(""); setParentResults([]); }
  };

  // Debounced parent concept search — the early-return branch clears results
  // via the timeout path to satisfy react-hooks/set-state-in-effect.
  useEffect(() => {
    const controller = new AbortController();
    window.clearTimeout(parentTimerRef.current);
    if (parentQuery.length < 3 || parentConcept) {
      parentTimerRef.current = window.setTimeout(() => {
        setParentResults([]);
      }, 0);
      return () => { window.clearTimeout(parentTimerRef.current); };
    }
    parentTimerRef.current = window.setTimeout(() => {
      (async () => {
        setSearchingParent(true);
        try {
          const matches = await searchDestinationConcepts(parentQuery, "", controller.signal, { retired: false, nonStandard: false });
          setParentResults(matches);
        } catch {
          setParentResults([]);
        } finally {
          setSearchingParent(false);
        }
      })();
    }, PARENT_SEARCH_DEBOUNCE_MS);
    return () => { window.clearTimeout(parentTimerRef.current); controller.abort(); };
  }, [parentQuery, parentConcept]);

  const selectParent = (c: DestinationConcept) => {
    setParentConcept(c);
    setParentQuery("");
    setParentResults([]);
    setReview(null); setConfirmed(false); setError("");
  };
  const clearParent = () => {
    setParentConcept(null);
    setParentQuery("");
    setParentResults([]);
    setReview(null); setConfirmed(false); setError("");
  };

  const submit = async () => {
    setBusy(true); setError("");
    try {
      const { data } = await api.post("/v1/code-mappings/mint-destination/", {
        ...fields, source_code: props.sourceCode, source_vocabulary_id: props.sourceVocabulary,
        parent_concept_id: parentConcept?.concept_id ?? null,
        action: review ? "mint" : "review", review_token: review?.review_token, none_match: confirmed,
      });
      if (review) props.onSelect(data);
      else setReview(data);
    } catch (err: unknown) {
      setReview(null); setConfirmed(false);
      const response = (err as { response?: { data?: { detail?: string } } }).response;
      setError(response?.data?.detail || "Unable to complete the check or mint. Check your entries and try again.");
    } finally { setBusy(false); }
  };
  const inputClass = "mt-1 block w-full rounded border border-slate-300 p-2";
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/50 p-4">
      <form role="dialog" aria-modal="true" aria-label="Mint new concept"
        className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded bg-white p-5 shadow-xl"
        onSubmit={e => { e.preventDefault(); void submit(); }}>
        <h2 className="text-lg font-semibold">Mint new concept</h2>
        <fieldset disabled={busy} className="mt-4 grid gap-3">
          <label>Custom vocabulary group
            <select autoFocus className={inputClass} value={fields.vocabulary_id} required onChange={e => update("vocabulary_id", e.target.value)}>
              <option value="">Choose an existing HK-* group</option>
              {groups.map(v => <option key={v.vocabulary_id} value={v.vocabulary_id}>{v.vocabulary_id} — {v.vocabulary_name}</option>)}
            </select>
          </label>
          {groups.length === 0 && <p>No custom vocabulary groups are available.</p>}
          <label>Concept name<input className={inputClass} required minLength={3} maxLength={255} value={fields.concept_name} onChange={e => update("concept_name", e.target.value)} /></label>
          <label>Concept code<input className={inputClass} required maxLength={50} value={fields.concept_code} onChange={e => update("concept_code", e.target.value)} /></label>
          <label>Concept domain<select className={inputClass} required value={fields.domain_id} onChange={e => update("domain_id", e.target.value)}>
            <option value="">Choose a domain</option>
            {props.domains.map(d => <option key={d.domain_id} value={d.domain_id}>{d.label}</option>)}
          </select></label>
          <div>
            <label htmlFor="parent-concept-search">Parent concept <span className="text-sm text-slate-500">(optional)</span></label>
            {parentConcept ? (
              <div className="mt-1 flex items-center gap-2 rounded border border-slate-300 bg-slate-50 p-2 text-sm">
                <span className="flex-1">{parentConcept.vocabulary_id}:{parentConcept.concept_code} — {parentConcept.concept_name} (ID {parentConcept.concept_id})</span>
                <button type="button" onClick={clearParent} className="text-slate-500 hover:text-red-600" aria-label="Clear parent concept">&#x2715;</button>
              </div>
            ) : (
              <div className="relative">
                <input id="parent-concept-search" className={inputClass} placeholder="Search for a broader concept..."
                  value={parentQuery} onChange={e => { setParentQuery(e.target.value); setReview(null); setConfirmed(false); setError(""); }} />
                {searchingParent && <p className="mt-1 text-xs text-slate-500">Searching...</p>}
                {parentResults.length > 0 && (
                  <div className="absolute z-10 mt-1 max-h-48 w-full overflow-y-auto rounded border border-slate-200 bg-white shadow-lg">
                    {parentResults.map(c => (
                      <button type="button" key={c.concept_id} onClick={() => selectParent(c)}
                        className="block w-full border-b p-2 text-left text-sm hover:bg-sky-50">
                        {c.concept_name} · {c.vocabulary_id}:{c.concept_code} · ID {c.concept_id}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </fieldset>
        {review && <section className="mt-4">
          <h3 className="font-semibold">Review candidate destinations</h3>
          <p className="text-sm">Choose an existing destination if it matches.</p>
          {review.candidates.length === 0 && <p className="mt-2 text-sm">No plausible candidates were found.</p>}
          <div className="mt-2 max-h-52 overflow-y-auto">
            {review.candidates.map(c => <button type="button" key={c.concept_id} disabled={busy}
              onClick={() => props.onSelect(c)} className="block w-full border-b p-2 text-left text-sm hover:bg-sky-50">
              {c.concept_name} · {c.vocabulary_id}:{c.concept_code} · ID {c.concept_id}
            </button>)}
          </div>
          <label className="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" disabled={busy} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />None of these match — mint a new concept.</label>
        </section>}
        {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" disabled={busy} onClick={props.onClose}>Cancel</button>
          <button type="submit" disabled={busy || groups.length === 0 || (!!review && !confirmed)} className="rounded bg-sky-700 px-3 py-2 text-white disabled:opacity-50">
            {busy ? "Working…" : review ? "Mint concept" : "Check existing destinations"}
          </button>
        </div>
      </form>
    </div>
  );
}
