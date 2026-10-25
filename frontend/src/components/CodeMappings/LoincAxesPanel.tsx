/**
 * The six LOINC axes a source code settles, why, and the curator's entries,
 * plus its axis variants (one source code whose units imply different axes).
 * Backed by /v1/code-mappings/<id>/loinc-axes/ and .../axis-variants/.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import api from "@/api/axios";
import { destinationError, searchDestinationConcepts, type DestinationConcept } from "./destinationSearch";
import {
  AXIS_BASIS_LABELS, LOINC_AXES, axisLabel, axisValueText, describeAxisValues,
  type AxisVariant, type LoincAxesPayload, type LoincAxis, type LoincAxisOverrides,
} from "./loincAxes";

type Props = {
  mappingId: number;
  canEdit: boolean;
  canApprove: boolean;
  /** Called with the current, possibly unsaved, overrides whenever they change. */
  onAxesChange?: (overrides: LoincAxisOverrides) => void;
  onSuggestVariant?: (axisKey: string) => void;
  /** Called with how many axis variants the code has (0 when fewer than two), so
   * the dialog's Suggest can fill them in (#2012). */
  onVariantsChange?: (count: number) => void;
  /** Bump to re-read the axes and variants (e.g. after a variant was set elsewhere). */
  reloadToken?: number;
};

const AXIS_VALUE_DEBOUNCE_MS = 250;
const BUTTON = "rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 disabled:opacity-40";

function sameOverrides(a: LoincAxisOverrides, b: LoincAxisOverrides): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<LoincAxis>;
  return [...keys].every(key => (key in a) === (key in b) && a[key] === b[key]);
}

export default function LoincAxesPanel({ mappingId, canEdit, canApprove, onAxesChange, onSuggestVariant, onVariantsChange, reloadToken = 0 }: Props) {
  const listPrefix = useId();
  const [payload, setPayload] = useState<LoincAxesPayload | null>(null);
  const [overrides, setOverrides] = useState<LoincAxisOverrides>({});
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [activeAxis, setActiveAxis] = useState<LoincAxis | null>(null);
  const [axisValues, setAxisValues] = useState<Partial<Record<LoincAxis, { value: string; codes: number }[]>>>({});
  const [variantBusy, setVariantBusy] = useState<string | null>(null);
  const [pickingVariant, setPickingVariant] = useState<string | null>(null);
  const onAxesChangeRef = useRef(onAxesChange);
  const onVariantsChangeRef = useRef(onVariantsChange);
  useEffect(() => { onAxesChangeRef.current = onAxesChange; onVariantsChangeRef.current = onVariantsChange; });

  const accept = useCallback((data: LoincAxesPayload) => {
    setPayload(data);
    const saved = data.loinc_axes || {};
    setOverrides(saved);
    // Only a code with LOINC axes has overrides for Suggest to send.
    if (data.applies) onAxesChangeRef.current?.(saved);
    const count = data.applies ? (data.variants ?? []).length : 0;
    onVariantsChangeRef.current?.(count >= 2 ? count : 0);
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const { data } = await api.get<LoincAxesPayload>(`/v1/code-mappings/${mappingId}/loinc-axes/`);
        if (active) { accept(data); setLoadError(""); }
      } catch {
        if (active) setLoadError("Could not load the LOINC axes for this code.");
      }
    })();
    return () => { active = false; };
  }, [mappingId, reloadToken, accept]);

  // Autocomplete for the axis being edited, debounced.
  const query = activeAxis ? overrides[activeAxis] ?? "" : "";
  useEffect(() => {
    if (!activeAxis) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const { data } = await api.get<{ axis: string; values: { value: string; codes: number }[] }>(
          "/v1/loinc/axis-values/", { params: { axis: activeAxis, q: query }, signal: controller.signal });
        setAxisValues(current => ({ ...current, [activeAxis]: data.values || [] }));
      } catch {
        // Autocomplete is a convenience; free text still works.
      }
    }, AXIS_VALUE_DEBOUNCE_MS);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [activeAxis, query]);

  if (loadError) return <p className="text-xs text-rose-700">{loadError}</p>;
  if (!payload || !payload.applies) return null;

  const labels = payload.axis_labels;
  const saved = payload.loinc_axes || {};
  const dirty = !sameOverrides(overrides, saved);

  const change = (next: LoincAxisOverrides) => {
    setOverrides(next);
    setMessage("");
    onAxesChange?.(next);
  };
  const setAxis = (axis: LoincAxis, value: string | null | undefined) => {
    const next = { ...overrides };
    if (value === undefined) delete next[axis];
    else next[axis] = value;
    change(next);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const { data } = await api.put<LoincAxesPayload>(`/v1/code-mappings/${mappingId}/loinc-axes/`, { loinc_axes: overrides });
      accept(data);
      setMessage("LOINC axes saved.");
    } catch (failure) {
      setError(`Could not save the LOINC axes. ${destinationError(failure)}`);
    } finally {
      setSaving(false);
    }
  };

  const postVariant = async (variant: AxisVariant, targetConceptId: number | null, approve: boolean, reopen = false) => {
    setVariantBusy(variant.axis_key);
    setError("");
    setMessage("");
    try {
      const { data } = await api.post<LoincAxesPayload>(`/v1/code-mappings/${mappingId}/axis-variants/`, {
        axis_key: variant.axis_key, target_concept_id: targetConceptId, approve, ...(reopen ? { reopen: true } : {}),
      });
      setPayload(data);
      setPickingVariant(null);
      // Say what the server did, not what was asked: reopening also moves rows.
      const what = data.variant_status === "approved" ? "Variant approved"
        : reopen ? "Variant reopened" : "Variant destination saved";
      setMessage(data.repoint
        ? `${what}: moved ${data.repoint.rows_updated} row(s), ${data.repoint.persons_marked_stale} patient record(s) marked for refresh.`
        : `${what}.`);
    } catch (failure) {
      setError(`Could not update the variant. ${destinationError(failure)}`);
    } finally {
      setVariantBusy(null);
    }
  };

  const displayValue = (axis: LoincAxis): string => {
    if (axis in overrides) {
      const value = overrides[axis];
      if (value === null) return "No constraint";
      if (value === "" && axis === "method_type") return "Methodless";
      if (value) return value;
    }
    const inferred = payload.source_axes?.axes?.[axis];
    if (inferred) return axisValueText(inferred.value, { empty: axis === "method_type" ? "Methodless" : "(none)" });
    return "—";
  };
  /** The whole set, for a title, where the cell shows the first few. */
  const fullValue = (axis: LoincAxis): string | undefined => {
    const inferred = payload.source_axes?.axes?.[axis];
    return !(axis in overrides) && Array.isArray(inferred?.value) ? axisValueText(inferred.value, { full: true }) : undefined;
  };
  const why = (axis: LoincAxis): string => {
    const unsaved = overrides[axis] !== saved[axis] || (axis in overrides) !== (axis in saved);
    if (axis in overrides) {
      if (overrides[axis] === null) return unsaved ? "Cleared by curator (unsaved)" : "Cleared by curator";
      if (overrides[axis] !== "" || axis === "method_type") return unsaved ? "Set by curator (unsaved)" : "Set by curator";
    }
    if (unsaved) return "Inferred (curator entry removed, unsaved)";
    const inferred = payload.source_axes?.axes?.[axis];
    if (!inferred) return "Not settled";
    const basis = AXIS_BASIS_LABELS[inferred.basis] ?? inferred.basis;
    if (inferred.basis === "curator") return basis;
    return inferred.evidence ? `${basis}: ${inferred.evidence}` : basis;
  };

  const hints = LOINC_AXES.filter(axis => payload.source_axes?.hints?.[axis]);
  const variants = payload.variants ?? [];

  const axesTable = <>
    <table className="mt-2 w-full text-left">
      <thead><tr className="border-b text-slate-500">
        <th className="pb-1 pr-2 font-medium">Axis</th>
        <th className="pb-1 px-2 font-medium">Value</th>
        <th className="pb-1 px-2 font-medium">Why</th>
        {canEdit && <th className="pb-1 pl-2 font-medium">Edit</th>}
      </tr></thead>
      <tbody>{LOINC_AXES.map(axis => {
        const label = axisLabel(axis, labels);
        const listId = `${listPrefix}-${axis}`;
        const text = typeof overrides[axis] === "string" ? overrides[axis] as string : "";
        return <tr key={axis} className="border-b border-slate-100 align-top">
          <td className="py-1 pr-2 font-medium text-slate-700">{label}</td>
          <td className="py-1 px-2 font-mono text-slate-900" data-testid={`axis-value-${axis}`} title={fullValue(axis)}>{displayValue(axis)}</td>
          <td className="py-1 px-2 text-slate-600" data-testid={`axis-why-${axis}`}>{why(axis)}</td>
          {canEdit && <td className="py-1 pl-2">
            <div className="flex flex-wrap items-center gap-1">
              <input aria-label={`${label} value`} list={listId} value={text} disabled={saving}
                placeholder={axisValueText(payload.source_axes?.axes?.[axis]?.value, { blank: "any", empty: "any" })}
                onFocus={() => setActiveAxis(axis)}
                onChange={event => setAxis(axis, event.target.value === "" ? undefined : event.target.value)}
                className="h-6 w-28 rounded border border-slate-300 bg-white px-1 font-mono" />
              <datalist id={listId}>
                {(axisValues[axis] ?? []).map(item => <option key={item.value} value={item.value}>{`${item.codes} codes`}</option>)}
              </datalist>
              <button type="button" className={BUTTON} disabled={saving || overrides[axis] === null}
                aria-label={`Clear ${label}`} title="Lift this axis: no constraint"
                onClick={() => setAxis(axis, null)}>Clear</button>
              {axis === "method_type" && <button type="button" className={BUTTON} disabled={saving || overrides[axis] === ""}
                aria-label="Methodless" title="The LOINC must have no method"
                onClick={() => setAxis(axis, "")}>Methodless</button>}
              {axis in overrides && <button type="button" className={BUTTON} disabled={saving}
                aria-label={`Use inferred ${label}`} title="Remove the curator entry and use what the source says"
                onClick={() => setAxis(axis, undefined)}>Inferred</button>}
            </div>
          </td>}
        </tr>;
      })}</tbody>
    </table>
    {hints.length > 0 && <ul aria-label="Axis hints" className="mt-2 space-y-0.5 text-slate-600">
      {hints.map(axis => <li key={axis}>Metadata says: {axisLabel(axis, labels)} — {payload.source_axes.hints[axis]}</li>)}
    </ul>}
  </>;

  return <section aria-label="LOINC axes" className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs">
    <h3 className="text-sm font-semibold text-slate-900">LOINC axes</h3>
    <p className="mt-0.5 text-slate-500">{variants.length >= 2
      ? "This code's units imply more than one LOINC axis set. Each is a variant with its own destination; approve one to route its units."
      : "Settled axes are constraints: a LOINC that differs on any of them is a different test and is not suggested."}</p>
    {error && <p role="alert" className="mt-1 text-rose-700">{error}</p>}
    {message && <p role="status" className="mt-1 text-green-800">{message}</p>}
    {variants.length >= 2
      ? <>
        {/* A code with variants is curated per variant: the axis sets, their
            units and destinations lead, and the axes every variant shares fold
            under them (#2014). The Save bar stays outside the fold, so an
            unsaved shared axis is never out of sight. */}
        <div className="mt-2" data-testid="axis-variants">
      <h4 className="text-sm font-semibold text-blue-700">{variants.length} axis values — this code may need one LOINC per variant</h4>
      <p className="mt-0.5 text-slate-500">Suggest fills in a proposed LOINC for each variant without one; approve each to route its units.</p>
      {payload.variant_notes?.length > 0 && <div className="mt-1 rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-800">
        {payload.variant_notes.map(note => <p key={note}>{note}</p>)}
      </div>}
      <table className="mt-2 w-full text-left" aria-label="Axis variants">
        <thead><tr className="border-b text-slate-500">
          <th className="pb-1 pr-2 font-medium">Axis values</th>
          <th className="pb-1 px-2 font-medium">Units</th>
          <th className="pb-1 px-2 text-right font-medium">Records</th>
          <th className="pb-1 px-2 font-medium">Destination</th>
          <th className="pb-1 px-2 font-medium">Status</th>
          <th className="pb-1 pl-2 font-medium">Actions</th>
        </tr></thead>
        <tbody>{variants.map(variant => {
          const description = describeAxisValues(variant.axis_values, labels);
          const busy = variantBusy !== null || saving;
          return <tr key={variant.axis_key} className="border-b border-slate-100 align-top">
            <td className="py-1 pr-2" title={describeAxisValues(variant.axis_values, labels, { full: true })}>{description}{variant.seen === false && <span className="ml-1 text-slate-400">(not in current evidence)</span>}</td>
            <td className="py-1 px-2 font-mono">{variant.units?.join(", ") || "—"}</td>
            <td className="py-1 px-2 text-right tabular-nums">{variant.occurrence_count ?? 0}</td>
            <td className="py-1 px-2">{variant.target_concept
              ? <><span>{variant.target_concept.concept_name}</span>
                  <span className="ml-1 text-slate-500">{variant.target_concept.vocabulary_id}:{variant.target_concept.concept_code}</span></>
              : "—"}</td>
            <td className="py-1 px-2"><span className={variant.status === "approved" ? "font-medium text-green-800" : "text-slate-700"}>{variant.status}</span></td>
            <td className="py-1 pl-2">
              <div className="flex flex-wrap gap-1">
                {onSuggestVariant && <button type="button" className={BUTTON} disabled={busy}
                  aria-label={`Suggest for ${description}`} onClick={() => onSuggestVariant(variant.axis_key)}>Suggest</button>}
                {canEdit && (variant.status !== "approved" || canApprove) && <button type="button" className={BUTTON} disabled={busy}
                  aria-label={`Set destination for ${description}`}
                  onClick={() => setPickingVariant(current => current === variant.axis_key ? null : variant.axis_key)}>Set destination</button>}
                {canEdit && canApprove && variant.status !== "approved" && <button type="button"
                  className="rounded border border-green-300 bg-green-50 px-1.5 py-0.5 text-[11px] font-medium text-green-800 hover:bg-green-100 disabled:opacity-40"
                  disabled={busy || !variant.target_concept} aria-label={`Approve ${description}`}
                  title={variant.target_concept ? "Approve and re-point this variant's stored rows" : "Set a destination first"}
                  onClick={() => void postVariant(variant, variant.target_concept!.concept_id, true)}>
                  {variantBusy === variant.axis_key ? "Saving…" : "Approve"}
                </button>}
                {canEdit && canApprove && variant.status === "approved" && <button type="button" className={BUTTON}
                  disabled={busy} aria-label={`Reopen ${description}`}
                  title="Stop routing to this variant and move its stored rows back to the code's destination"
                  onClick={() => void postVariant(variant, variant.target_concept?.concept_id ?? null, false, true)}>
                  Reopen
                </button>}
              </div>
              {pickingVariant === variant.axis_key && <VariantDestinationSearch label={description} disabled={busy}
                onCancel={() => setPickingVariant(null)}
                onChoose={concept => void postVariant(variant, concept.concept_id, variant.status === "approved")} />}
            </td>
          </tr>;
        })}</tbody>
      </table>
    </div>
        <details className="mt-3 border-t border-slate-200 pt-2">
          <summary className="cursor-pointer text-sm font-semibold text-slate-900">Axes shared by every variant
            {dirty && <span className="ml-2 font-normal text-amber-800">(unsaved changes)</span>}</summary>
          <p className="mt-0.5 text-slate-500">Constraints on all of the variants; each variant adds its own axis values on top.</p>
          {axesTable}
        </details>
      </>
      : axesTable}
    {canEdit && <div className="mt-2 flex items-center gap-2">
      <button type="button" disabled={!dirty || saving} onClick={() => void save()}
        className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-40">
        {saving ? "Saving…" : "Save axes"}
      </button>
      {dirty && <span className="text-slate-500">Unsaved axes still apply to Suggest from this dialog.</span>}
    </div>}
  </section>;
}

/** Destination search for one variant, using the queue's concept search. */
function VariantDestinationSearch({ label, disabled, onChoose, onCancel }: {
  label: string;
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
        const matches = await searchDestinationConcepts(query, "LOINC", controller.signal);
        setResults(matches);
        setError("");
      } catch {
        if (!controller.signal.aborted) setError("Could not search destinations.");
      }
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [query]);

  const shown = query.trim().length >= 3 ? results : [];
  return <div className="mt-1 space-y-1 rounded border border-slate-300 bg-white p-2">
    <div className="flex items-center gap-1">
      <input autoFocus aria-label={`Search destination for ${label}`} value={query} disabled={disabled}
        placeholder="Search LOINC by name or code" onChange={event => setQuery(event.target.value)}
        onKeyDown={event => { if (event.key === "Escape") onCancel(); }}
        className="h-6 flex-1 rounded border border-slate-300 px-1" />
      <button type="button" className={BUTTON} onClick={onCancel}>Cancel</button>
    </div>
    {error && <p className="text-rose-700">{error}</p>}
    {shown.length > 0 && <ul role="listbox" aria-label={`Destinations for ${label}`} className="max-h-40 overflow-y-auto">
      {shown.map(concept => <li key={concept.concept_id} role="none">
        <button type="button" role="option" aria-selected={false} disabled={disabled} onClick={() => onChoose(concept)}
          className="block w-full px-1 py-0.5 text-left hover:bg-slate-100">
          {concept.concept_name} <span className="text-slate-500">{concept.vocabulary_id}:{concept.concept_code}</span>
        </button>
      </li>)}
    </ul>}
  </div>;
}
