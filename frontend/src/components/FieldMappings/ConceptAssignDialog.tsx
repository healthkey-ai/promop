import { useCallback, useEffect, useRef, useState } from "react";
import { Search, Sparkles, X } from "lucide-react";
import api from "@/api/axios";
import { HelpTip, Field, ReadOnlyField, INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
  suggested_unit?: string;
}

interface FieldChoiceInfo {
  id: number;
  display: string;
  sort_order: number;
  codes: { code: string; vocabulary_id: string; display: string; is_primary: boolean }[];
}

interface Props {
  fieldName: string;
  fieldType: string;
  onClose: () => void;
  onSaved: () => void;
  initialConceptCode?: string;
  initialVocabularyId?: string;
  initialUnit?: string;
  initialOmopTable?: string;
  existingMappingId?: number;
  initialConceptId?: number | null;
  initialConceptName?: string;
  initialStatus?: "proposed" | "approved" | "rejected";
  initialNotes?: string;
  commonUnits?: string[];
  choices?: FieldChoiceInfo[];
  onEditChoices?: () => void;
  canApprove?: boolean;
}

/** Tooltip copy for each field in the dialog. */
const TIP = {
  source_concept_id:
    "The OMOP concept ID assigned to this patient-record field. Chosen from the search above.",
  source_concept_name:
    "Name of the concept in the OMOP vocabulary.",
  source_concept_code:
    "The concept's own code in its vocabulary, e.g. 16112-5 for a LOINC test.",
  source_concept_class:
    "The concept's class within its vocabulary, e.g. Clinical Finding, Lab Test.",
  source_vocabulary_id:
    "Vocabulary the concept belongs to — LOINC, SNOMED, HemOnc, etc.",
  standard_concept:
    "'S' means a standard Athena concept. Blank or null means non-standard or locally minted.",
  source_table:
    "The OMOP clinical table this field's data is stored in. Measurement for labs, Observation for clinical findings, etc.",
  search:
    "Search OMOP concepts by name or code. Suggest seeds the search from the field name.",
  unit:
    "The unit of measurement for this field, e.g. mg/dL, cells/uL.",
  notes:
    "Why this mapping was chosen, for the next curator who reviews it.",
  status:
    "Proposed is awaiting review. Approved activates the mapping. Rejected hides it. Only org admins and staff can approve.",
  status_new:
    "A new mapping always starts as Proposed. Only org admins and staff can approve it once reviewed.",
} as const;

export function ConceptAssignDialog({
  fieldName, fieldType, onClose, onSaved,
  initialConceptCode, initialVocabularyId, initialUnit, initialOmopTable,
  existingMappingId, initialConceptId, initialConceptName, initialStatus, initialNotes, commonUnits,
  choices, onEditChoices, canApprove = true,
}: Props) {
  const isEditing = !!existingMappingId;
  const isNewMapping = !isEditing;
  const [searchQuery, setSearchQuery] = useState("");
  const [vocabFilter, setVocabFilter] = useState("");
  const [results, setResults] = useState<ConceptResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<ConceptResult | null>(() => (
    initialConceptId != null
      ? {
          concept_id: initialConceptId,
          concept_code: initialConceptCode || "",
          concept_name: initialConceptName || "",
          vocabulary_id: initialVocabularyId || "",
          domain_id: "",
          concept_class_id: "",
          standard_concept: null,
        }
      : null
  ));
  const [unit, setUnit] = useState(initialUnit || "");
  const [customUnit, setCustomUnit] = useState("");
  const [omopTable, setOmopTable] = useState(initialOmopTable || "Measurement");
  const [mappingStatus, setMappingStatus] = useState<"proposed" | "approved" | "rejected">(
    initialStatus || "proposed"
  );
  const [notes, setNotes] = useState(initialNotes || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const unitChoices = commonUnits || [];
  const hasCommonUnits = unitChoices.length > 0;
  const isCustomUnit = hasCommonUnits && unit !== "" && !unitChoices.includes(unit);

  // These are the OMOP CDM clinical and reference tables implemented by this
  // application.  A mapping can be advisory for tables that are not currently
  // writable; limiting the curator UI to the write pipeline hid valid mappings.
  const OMOP_TABLES = [
    "Person", "Location", "CareSite", "Provider", "ObservationPeriod",
    "VisitOccurrence", "ConditionOccurrence", "DrugExposure",
    "ProcedureOccurrence", "Measurement", "Observation", "Death",
    "Specimen", "Note", "NoteNlp",
  ];

  const doSearch = useCallback(async (q: string) => {
    if (q.length < 3) {
      setResults([]);
      return;
    }
    setSearching(true);
    try {
      const params: Record<string, string> = { q, limit: "50" };
      if (vocabFilter) params.vocabulary_id = vocabFilter;
      const resp = await api.get("/v1/concepts/search/", { params });
      setResults(resp.data.results || resp.data || []);
    } catch (err: unknown) {
      if (err && typeof err === "object" && "response" in err) {
        const status = (err as { response: { status: number } }).response?.status;
        if (status === 401 || status === 403) throw err;
      }
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, [vocabFilter]);

  const handleSuggest = () => {
    setSearchQuery(fieldName.replace(/_/g, " "));
  };

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(searchQuery), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [searchQuery, doSearch]);

  const effectiveUnit = isCustomUnit ? customUnit || unit : unit;

  const handleSubmit = async () => {
    if (!selected && !isEditing) return;
    setSaving(true);
    setError("");
    try {
      const effectiveStatus = isNewMapping
        ? "proposed"
        : canApprove
          ? mappingStatus
          : (mappingStatus === "approved" || mappingStatus === "rejected" ? "proposed" : mappingStatus);
      const mappingPayload = {
        concept: selected?.concept_id ?? null,
        vocabulary_id: selected?.vocabulary_id ?? "",
        concept_code: selected?.concept_code ?? "",
        unit: effectiveUnit,
        omop_table: omopTable,
        notes,
        status: effectiveStatus,
      };
      if (isEditing) {
        await api.patch(`/v1/field-mappings/${existingMappingId}/`, mappingPayload);
      } else {
        await api.post("/v1/field-mappings/", { field_name: fieldName, ...mappingPayload });
      }
      onSaved();
    } catch (err: unknown) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? JSON.stringify((err as { response: { data: unknown } }).response.data)
          : "Failed to save mapping.";
      setError(msg);
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
  };

  const handleUnitDropdownChange = (value: string) => {
    if (value === "__custom__") {
      setCustomUnit(unit);
      setUnit(value);
    } else {
      setUnit(value);
      setCustomUnit("");
    }
  };

  return (
    <div
      ref={overlayRef}
      onClick={handleOverlayClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="concept-assign-dialog-title"
        className="relative flex w-full max-w-2xl flex-col rounded-lg border bg-white shadow-xl"
      >
        {/* Header */}
        <div className="border-b border-slate-200 px-5 py-4">
          <button
            onClick={onClose}
            className="absolute right-3 top-3 rounded p-1 text-slate-400 hover:text-slate-600"
          >
            <X size={16} />
          </button>

          <h2 id="concept-assign-dialog-title" className="text-lg font-semibold text-slate-900">
            {isEditing ? "Edit Concept Mapping" : "Assign Concept"}
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            Field: <span className="font-mono">{fieldName}</span> ({fieldType})
          </p>
        </div>

        {/* Body */}
        <div className="max-h-[calc(100vh-12rem)] overflow-y-auto px-5 py-4">
          {/* ── SOURCE CONCEPT ─────────────────────────────────────── */}
          <fieldset className="mb-4 rounded-md border border-slate-200 p-4">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Source Concept — the OMOP concept this field maps to
            </legend>

            {/* Concept search */}
            <div className="mb-4">
              <div className="mb-2 flex items-end justify-between gap-3">
                <div className="flex items-center gap-1">
                  <label className="text-sm font-medium text-slate-700" htmlFor="field-mapping-concept-search">
                    Search concepts
                  </label>
                  <HelpTip tip={TIP.search} />
                </div>
                <button
                  type="button"
                  onClick={handleSuggest}
                  className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                >
                  <Sparkles size={13} />
                  Suggest
                </button>
              </div>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={15} />
                  <input
                    id="field-mapping-concept-search"
                    title={TIP.search}
                    type="text"
                    placeholder="Search concepts..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
                    autoFocus
                  />
                </div>
                <select
                  value={vocabFilter}
                  onChange={(e) => setVocabFilter(e.target.value)}
                  className="h-10 w-40 shrink-0 rounded-md border border-slate-300 px-2 text-sm text-slate-950"
                >
                  <option value="">All vocabularies</option>
                  <option value="LOINC">LOINC</option>
                  <option value="SNOMED">SNOMED</option>
                  <option value="RxNorm">RxNorm</option>
                  <option value="HemOnc">HemOnc</option>
                </select>
              </div>
              <div className="mt-2 max-h-40 overflow-y-auto rounded-md border border-slate-200">
                {searching && <div className="px-3 py-2 text-sm text-slate-500">Searching...</div>}
                {!searching && results.length === 0 && searchQuery.length >= 3 && (
                  <div className="px-3 py-2 text-sm text-slate-500">No results found.</div>
                )}
                {!searching && results.length > 0 && results.slice(0, 50).map((c) => (
                  <button
                    key={c.concept_id}
                    type="button"
                    onClick={() => {
                      setSelected(c);
                      if (c.suggested_unit && !unit) {
                        setUnit(c.suggested_unit);
                      }
                    }}
                    className={`grid w-full grid-cols-[8rem_1fr_6rem] gap-2 border-b border-slate-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-slate-50 ${
                      selected?.concept_id === c.concept_id ? "bg-blue-50" : ""
                    }`}
                  >
                    <span className="font-mono text-slate-700">{c.concept_code}</span>
                    <span className="text-slate-900">{c.concept_name}</span>
                    <span className="font-mono text-slate-500">{c.vocabulary_id}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* Clear selection */}
            {selected && (
              <div className="mb-3 flex items-center justify-between rounded-md bg-slate-50 px-3 py-2">
                <span className="text-sm text-slate-700">
                  Selected: <span className="font-mono font-medium">{selected.vocabulary_id}:{selected.concept_code}</span>{" "}
                  {selected.concept_name}
                </span>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  className="ml-2 rounded p-0.5 text-slate-400 hover:text-slate-600"
                  title="Clear selection"
                >
                  <X size={14} />
                </button>
              </div>
            )}

            {/* Read-only concept detail fields, matching Code Mapping layout */}
            <div className="grid gap-4 md:grid-cols-2">
              <ReadOnlyField
                id="source_concept_id"
                label="Source Concept ID"
                tip={TIP.source_concept_id}
                value={selected ? String(selected.concept_id) : ""}
                fullWidth
              />
              <ReadOnlyField
                id="source_concept_name"
                label="Source Concept Name"
                tip={TIP.source_concept_name}
                value={selected?.concept_name || ""}
                fullWidth
              />
              <ReadOnlyField
                id="source_concept_code"
                label="Source Concept Code"
                tip={TIP.source_concept_code}
                value={selected?.concept_code || ""}
                fullWidth
              />
              <ReadOnlyField
                id="source_vocabulary_id"
                label="Source Vocabulary ID"
                tip={TIP.source_vocabulary_id}
                value={selected?.vocabulary_id || ""}
                fullWidth
              />
              <ReadOnlyField
                id="source_concept_class"
                label="Source Concept Class"
                tip={TIP.source_concept_class}
                value={selected?.concept_class_id || ""}
                fullWidth
              />
              <ReadOnlyField
                id="standard_concept"
                label="Standard Concept"
                tip={TIP.standard_concept}
                value={selected?.standard_concept || ""}
                fullWidth
              />
              <Field id="source_table" label="Source Table" tip={TIP.source_table}>
                <select
                  id="source_table"
                  title={TIP.source_table}
                  value={omopTable}
                  onChange={(e) => setOmopTable(e.target.value)}
                  className={`${INPUT_CLASS} w-full`}
                >
                  {OMOP_TABLES.map((table) => <option key={table} value={table}>{table}</option>)}
                </select>
              </Field>
            </div>
          </fieldset>

          {/* Unit */}
          <div className="mb-4">
            <Field id="unit" label="Unit" tip={TIP.unit}>
              {selected?.suggested_unit && unit === selected.suggested_unit && (
                <span className="text-[10px] text-slate-400">(suggested)</span>
              )}
              {hasCommonUnits ? (
                <div className="space-y-1.5">
                  <select
                    id="unit"
                    value={isCustomUnit ? "__custom__" : unit}
                    onChange={(e) => handleUnitDropdownChange(e.target.value)}
                    className={`${INPUT_CLASS} w-full`}
                  >
                    <option value="">Select unit...</option>
                    {unitChoices.map((u) => (
                      <option key={u} value={u}>{u}</option>
                    ))}
                    <option value="__custom__">Other...</option>
                  </select>
                  {isCustomUnit && (
                    <input
                      type="text"
                      value={customUnit}
                      onChange={(e) => setCustomUnit(e.target.value)}
                      placeholder="Enter custom unit..."
                      className={`${INPUT_CLASS} w-full`}
                    />
                  )}
                </div>
              ) : (
                <input
                  id="unit"
                  type="text"
                  value={unit}
                  onChange={(e) => setUnit(e.target.value)}
                  placeholder="e.g. mg/dL"
                  className={`${INPUT_CLASS} w-full`}
                />
              )}
            </Field>
          </div>

          {/* Notes */}
          <div className="mb-4">
            <Field id="notes" label="Notes" tip={TIP.notes}>
              <textarea
                id="notes"
                title={TIP.notes}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={2}
                className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm font-normal text-slate-950"
                placeholder="Rationale for this mapping..."
              />
            </Field>
          </div>

          {/* Field Choices */}
          {choices !== undefined && (
            <div className="mb-4">
              <div className="mb-1 flex items-center gap-2">
                <label className="text-xs font-medium text-slate-600">
                  Field Choices (Value Set)
                </label>
                {onEditChoices && (
                  <button
                    onClick={onEditChoices}
                    className="text-[10px] text-primary hover:underline"
                  >
                    Edit choices
                  </button>
                )}
              </div>
              {choices.length > 0 ? (
                <div className="max-h-28 overflow-y-auto rounded-md border border-slate-200 text-xs">
                  <table className="w-full">
                    <tbody className="divide-y divide-slate-100">
                      {choices.map((ch) => (
                        <tr key={ch.id} className="hover:bg-slate-50">
                          <td className="px-2 py-1">{ch.display}</td>
                          <td className="px-2 py-1 text-slate-400">
                            {ch.codes.map((c) => `${c.vocabulary_id}:${c.code}`).join(", ") || "no codes"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="rounded-md border border-dashed border-slate-300 px-3 py-1.5 text-xs text-slate-400">
                  No choices defined
                  {onEditChoices && (
                    <button onClick={onEditChoices} className="ml-1 text-primary hover:underline">
                      — add some
                    </button>
                  )}
                </div>
              )}
            </div>
          )}

          {error && <div className="mb-3 text-sm text-red-600">{error}</div>}
        </div>

        {/* Footer — status at bottom-left, buttons at bottom-right */}
        <div className="flex items-center justify-between gap-2 border-t border-slate-200 px-5 py-4">
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium text-slate-700" htmlFor="mapping-status">Status</label>
            <HelpTip tip={isNewMapping ? TIP.status_new : TIP.status} />
            <select
              id="mapping-status"
              title={isNewMapping ? TIP.status_new : TIP.status}
              value={isNewMapping ? "proposed" : mappingStatus}
              disabled={isNewMapping}
              onChange={(e) => setMappingStatus(e.target.value as "proposed" | "approved" | "rejected")}
              className="h-9 rounded-md border border-slate-300 px-2 text-sm font-normal text-slate-950 disabled:bg-slate-100 disabled:text-slate-500"
            >
              <option value="proposed">Proposed</option>
              <option value="approved" disabled={!canApprove}>
                {canApprove ? "Approved" : "Approved (admin only)"}
              </option>
              <option value="rejected" disabled={!canApprove}>
                {canApprove ? "Rejected" : "Rejected (admin only)"}
              </option>
            </select>
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={(!selected && !isEditing) || saving}
              className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              title={canApprove ? undefined : "Only org admins and staff can approve mappings. This will be saved as a proposal for review."}
            >
              {saving
                ? "Saving..."
                : isEditing
                  ? "Update Mapping"
                  : "Save Mapping"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
