/**
 * What a source code means in SNOMED terms: its reference concept(s), the
 * hierarchy and laterality the source settles and why, and the reference's
 * defining attributes, plus the curator's entries for each.
 * Backed by /v1/code-mappings/<id>/snomed-attributes/.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import api from "@/api/axios";
import { destinationError, searchDestinationConcepts, type DestinationConcept } from "./destinationSearch";
import {
  REFERENCE_BASIS_LABELS, SNOMED_HELP, basisLabel, sameSnomedSettings,
  type SnomedAttributesPayload, type SnomedLaterality, type SnomedSettings,
} from "./snomed";

type Props = {
  mappingId: number;
  canEdit: boolean;
  /** Called with the current, possibly unsaved, settings whenever they change. */
  onSettingsChange?: (settings: SnomedSettings) => void;
  /** Bump to re-read the settings. */
  reloadToken?: number;
};

const BUTTON = "rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40";
const SELECT = "h-6 rounded border border-slate-300 bg-white px-1 text-xs";
const INFERRED = "__inferred__";
const NONE = "__none__";

export default function SnomedEvidencePanel({ mappingId, canEdit, onSettingsChange, reloadToken = 0 }: Props) {
  const [payload, setPayload] = useState<SnomedAttributesPayload | null>(null);
  const [settings, setSettings] = useState<SnomedSettings>({});
  const [picked, setPicked] = useState<DestinationConcept | null>(null);
  const [searching, setSearching] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const onChangeRef = useRef(onSettingsChange);
  useEffect(() => { onChangeRef.current = onSettingsChange; });

  const accept = useCallback((data: SnomedAttributesPayload) => {
    setPayload(data);
    const saved = data.snomed_attributes || {};
    setSettings(saved);
    setPicked(null);
    // Only a code SNOMED judging applies to has settings for Suggest to send.
    if (data.applies) onChangeRef.current?.(saved);
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const { data } = await api.get<SnomedAttributesPayload>(`/v1/code-mappings/${mappingId}/snomed-attributes/`);
        if (active) { accept(data); setLoadError(""); }
      } catch {
        if (active) setLoadError("Could not load the SNOMED evidence for this code.");
      }
    })();
    return () => { active = false; };
  }, [mappingId, reloadToken, accept]);

  if (loadError) return <p className="text-xs text-rose-700">{loadError}</p>;
  if (!payload || !payload.applies) return null;

  const saved = payload.snomed_attributes || {};
  const source = payload.source;
  const dirty = !sameSnomedSettings(settings, saved);
  const hierarchies = Object.entries(payload.hierarchies || {});
  const lateralities = payload.lateralities?.length ? payload.lateralities : ["Left", "Right", "Right and left"] as SnomedLaterality[];

  const change = (next: SnomedSettings) => {
    setSettings(next);
    setMessage("");
    onSettingsChange?.(next);
  };
  const setKey = <K extends keyof SnomedSettings>(key: K, value: SnomedSettings[K] | undefined) => {
    const next = { ...settings };
    if (value === undefined) delete next[key];
    else next[key] = value;
    change(next);
  };
  const selectValue = (key: "hierarchy" | "laterality") =>
    !(key in settings) ? INFERRED : settings[key] === null ? NONE : settings[key] as string;
  const fromSelect = (value: string): string | null | undefined =>
    value === INFERRED ? undefined : value === NONE ? null : value;

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const { data } = await api.put<SnomedAttributesPayload>(`/v1/code-mappings/${mappingId}/snomed-attributes/`, { snomed_attributes: settings });
      accept(data);
      setMessage("SNOMED evidence saved.");
    } catch (failure) {
      setError(`Could not save the SNOMED evidence. ${destinationError(failure)}`);
    } finally {
      setSaving(false);
    }
  };

  const unsaved = (key: keyof SnomedSettings) => (key in settings) !== (key in saved) || settings[key] !== saved[key];
  const why = (key: "hierarchy" | "laterality"): string => {
    if (key in settings) {
      if (settings[key] === null) return unsaved(key) ? "Cleared by curator (unsaved)" : "Cleared by curator";
      return unsaved(key) ? "Set by curator (unsaved)" : "Set by curator";
    }
    if (unsaved(key)) return "Inferred (curator entry removed, unsaved)";
    const inferred = source?.[key];
    if (!inferred) return "Not settled";
    const basis = basisLabel(inferred.basis);
    return inferred.evidence && inferred.basis !== "curator" ? `${basis}: ${inferred.evidence}` : basis;
  };
  const hierarchyText = (value: string) => {
    const tag = payload.hierarchies?.[value];
    return tag ? `${value} (${tag})` : value;
  };
  const display = (key: "hierarchy" | "laterality"): string => {
    if (key in settings) {
      const value = settings[key];
      if (value === null) return key === "hierarchy" ? "No constraint" : "No side";
      return key === "hierarchy" ? hierarchyText(value as string) : value as string;
    }
    const inferred = source?.[key]?.value;
    if (!inferred) return "—";
    return key === "hierarchy" ? hierarchyText(inferred) : inferred;
  };

  const referenceUnsaved = unsaved("reference_concept_id");
  const references = source?.references ?? [];

  return <section aria-label="SNOMED evidence" className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs">
    <h3 className="text-sm font-semibold text-slate-900">SNOMED evidence</h3>
    <p className="mt-0.5 text-slate-500">{SNOMED_HELP}</p>
    {error && <p role="alert" className="mt-1 text-rose-700">{error}</p>}
    {message && <p role="status" className="mt-1 text-green-800">{message}</p>}
    <table className="mt-2 w-full text-left">
      <tbody>
        <tr className="border-b border-slate-100 align-top">
          <td className="py-1 pr-2 font-medium text-slate-700">Reference concept</td>
          <td className="py-1 px-2" data-testid="snomed-reference">
            {referenceUnsaved && "reference_concept_id" in settings
              ? <span>{picked ? <>{picked.concept_name} <span className="text-slate-500">SNOMED:{picked.concept_code}</span></> : `OMOP ${settings.reference_concept_id}`}
                  <span className="ml-1 text-slate-500">Chosen by curator (unsaved)</span></span>
              : referenceUnsaved
                ? <span className="text-slate-500">Inferred (curator choice removed, unsaved)</span>
                : references.length
                  ? <ul className="space-y-0.5">{references.map(reference => <li key={reference.concept_id}>
                      {reference.concept_name} <span className="text-slate-500">SNOMED:{reference.concept_code}</span>
                      {reference.hierarchy && <span className="ml-1 text-slate-500">{reference.hierarchy}</span>}
                    </li>)}
                    {source?.reference_basis && <li className="text-slate-500" data-testid="snomed-reference-basis">
                      {REFERENCE_BASIS_LABELS[source.reference_basis] ?? source.reference_basis}</li>}
                  </ul>
                  : <span className="text-slate-500">None: the source has no SNOMED reference</span>}
          </td>
          {canEdit && <td className="py-1 pl-2">
            <div className="flex flex-wrap gap-1">
              <button type="button" className={BUTTON} disabled={saving} onClick={() => setSearching(value => !value)}>Set reference concept</button>
              {"reference_concept_id" in settings && <button type="button" className={BUTTON} disabled={saving}
                aria-label="Use inferred reference concept" onClick={() => { setPicked(null); setKey("reference_concept_id", undefined); }}>Inferred</button>}
            </div>
          </td>}
        </tr>
        <tr className="border-b border-slate-100 align-top">
          <td className="py-1 pr-2 font-medium text-slate-700">Hierarchy</td>
          <td className="py-1 px-2">
            <span data-testid="snomed-hierarchy">{display("hierarchy")}</span>
            <span className="ml-1 text-slate-500" data-testid="snomed-hierarchy-why">{why("hierarchy")}</span>
          </td>
          {canEdit && <td className="py-1 pl-2">
            <select aria-label="Hierarchy" className={SELECT} disabled={saving} value={selectValue("hierarchy")}
              onChange={event => setKey("hierarchy", fromSelect(event.target.value))}>
              <option value={INFERRED}>Inferred</option>
              <option value={NONE}>None</option>
              {hierarchies.map(([name, tag]) => <option key={name} value={name}>{tag}</option>)}
            </select>
          </td>}
        </tr>
        <tr className="border-b border-slate-100 align-top">
          <td className="py-1 pr-2 font-medium text-slate-700">Laterality</td>
          <td className="py-1 px-2">
            <span data-testid="snomed-laterality">{display("laterality")}</span>
            <span className="ml-1 text-slate-500" data-testid="snomed-laterality-why">{why("laterality")}</span>
          </td>
          {canEdit && <td className="py-1 pl-2">
            <select aria-label="Laterality" className={SELECT} disabled={saving} value={selectValue("laterality")}
              onChange={event => setKey("laterality", fromSelect(event.target.value) as SnomedLaterality | null | undefined)}>
              <option value={INFERRED}>Inferred</option>
              <option value={NONE}>None</option>
              {lateralities.map(side => <option key={side} value={side}>{side}</option>)}
            </select>
          </td>}
        </tr>
      </tbody>
    </table>
    {canEdit && searching && <ReferenceSearch disabled={saving} onCancel={() => setSearching(false)}
      onChoose={concept => { setPicked(concept); setSearching(false); setKey("reference_concept_id", concept.concept_id); }} />}
    {(source?.attributes?.length ?? 0) > 0 && <div className="mt-2">
      <h4 className="font-medium text-slate-700">Defining attributes of the reference</h4>
      <ul aria-label="Reference attributes" className="mt-0.5 space-y-0.5 text-slate-700">
        {source!.attributes.map(attribute => <li key={attribute.relationship_id}>
          <span className="font-medium">{attribute.label}:</span> {attribute.values.join(", ") || "—"}
        </li>)}
      </ul>
    </div>}
    {canEdit && <div className="mt-2 flex items-center gap-2">
      <button type="button" disabled={!dirty || saving} onClick={() => void save()}
        className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-40">
        {saving ? "Saving…" : "Save SNOMED evidence"}
      </button>
      {dirty && <span className="text-slate-500">Unsaved settings still apply to Suggest from this dialog.</span>}
    </div>}
  </section>;
}

/** SNOMED concept search for the reference concept. */
function ReferenceSearch({ disabled, onChoose, onCancel }: {
  disabled: boolean;
  onChoose: (concept: DestinationConcept) => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<DestinationConcept[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (query.trim().length < 3) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const matches = await searchDestinationConcepts(query, "SNOMED", controller.signal);
        setResults(matches);
        setError("");
      } catch {
        if (!controller.signal.aborted) setError("Could not search SNOMED.");
      }
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query]);

  const shown = query.trim().length >= 3 ? results : [];
  return <div className="mt-1 space-y-1 rounded border border-slate-300 bg-white p-2">
    <div className="flex items-center gap-1">
      <input autoFocus aria-label="Search reference concept" value={query} disabled={disabled}
        placeholder="Search SNOMED by name or code" onChange={event => setQuery(event.target.value)}
        onKeyDown={event => { if (event.key === "Escape") onCancel(); }}
        className="h-6 flex-1 rounded border border-slate-300 px-1" />
      <button type="button" className={BUTTON} onClick={onCancel}>Cancel</button>
    </div>
    {error && <p className="text-rose-700">{error}</p>}
    {shown.length > 0 && <ul role="listbox" aria-label="Reference concepts" className="max-h-40 overflow-y-auto">
      {shown.map(concept => <li key={concept.concept_id} role="none">
        <button type="button" role="option" aria-selected={false} disabled={disabled} onClick={() => onChoose(concept)}
          className="block w-full px-1 py-0.5 text-left hover:bg-slate-100">
          {concept.concept_name} <span className="text-slate-500">{concept.vocabulary_id}:{concept.concept_code}</span>
        </button>
      </li>)}
    </ul>}
  </div>;
}
