import { useCallback, useEffect, useRef, useState } from "react";
import { Search, Sparkles, X } from "lucide-react";
import api from "@/api/axios";
import ConceptInputDetails from "@/components/UI/ConceptInputDetails";
import { HelpTip, Field, ReadOnlyField, INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";
import type { DiseaseCategoryOption, FieldScope } from "@/types/fieldScope";
import { FieldScopeControl } from "./FieldScopeControl";

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
  suggested_unit?: string;
  measurement_type?: "qualitative" | "quantitative";
}

interface FieldChoiceInfo {
  id: number;
  display: string;
  sort_order: number;
  /** Stable identity and scope (#1224); the dialog only reads display and codes. */
  code?: string;
  context_key?: string;
  retired?: boolean;
  aliases?: string[];
  diseases?: string[];
  codes: { code: string; vocabulary_id: string; display: string; is_primary: boolean }[];
}

interface Props {
  fieldName: string;
  fieldType: string;
  /** Search text for Suggest; falls back to the field name. */
  suggestQuery?: string;
  /** Open Code Mapping's source codes for a concept; no button without it. */
  onShowSourceCodes?: (conceptId: number) => void;
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
  /**
   * The unit and table the mapping row holds, when the dialog opens on
   * something else (a suggestion's, or the default table). What the dialog
   * shows and the row lacks then counts as a change to save.
   */
  storedUnit?: string;
  storedOmopTable?: string;
  /**
   * The field's scope, for a caller who may change it (#2332): the Disease
   * Category codes it is scoped to (none is the base record) and the
   * categories a scope may name. Without it the dialog offers no scope
   * control and no save carries a scope, which is what a caller without
   * mapping write access, or a field that cannot have a scope, gets.
   */
  fieldScope?: {
    diseases: string[];
    options: DiseaseCategoryOption[];
    /** The categories could not be loaded; `options` are those already in use. */
    optionsIncomplete?: boolean;
    /** Shown, not editable: a scope takes effect at once, so it is for approvers. */
    readOnly?: boolean;
  };
  /**
   * Create a field instead of mapping one (#721): the dialog also asks for
   * the field's definition and the tab it renders on, and saving the chosen
   * concept creates the field with its approved mapping in one request.
   * `fieldName` and `fieldType` are not read. Only someone who may approve
   * mappings is offered this; the server refuses anyone else.
   */
  create?: {
    /** The tab the new field starts on. */
    tab: string;
    tabOptions: { value: string; label: string }[];
  };
}

type ValueType = "text" | "number" | "date" | "boolean";
type FieldMode = "editable" | "computed";

/** A display name as the field name it suggests: lower_snake_case. */
const nameFromLabel = (label: string) => (
  label.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
);

const NO_DISEASES: string[] = [];

/** A DRF error for one key as text: a string, a list, or a list per index. */
function errorText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(errorText).filter(Boolean).join(" ");
  if (value && typeof value === "object") return Object.values(value).map(errorText).filter(Boolean).join(" ");
  return "";
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
  display_name:
    "The label the field is shown under on the patient tab.",
  field_name:
    "The field's key in PatientRecord: lower_snake_case, unique, and not changed after the field is created.",
  value_type:
    "What kind of value the field holds.",
  field_mode:
    "Editable: the value is entered on the patient tab, or read from the patient's OMOP facts for the concept. Computed: the value comes from a formula over other fields and is read-only.",
  formula:
    "An expression over other PatientRecord fields, e.g. weight / (height / 100) ^ 2.",
  tab:
    "The patient tab the field renders on, after the tab's other fields.",
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
  fieldName, fieldType, suggestQuery, onShowSourceCodes, onClose, onSaved,
  initialConceptCode, initialVocabularyId, initialUnit, initialOmopTable,
  existingMappingId, initialConceptId, initialConceptName, initialStatus, initialNotes, commonUnits,
  choices, onEditChoices, canApprove = true,
  storedUnit, storedOmopTable, fieldScope, create,
}: Props) {
  const creating = create !== undefined;
  const canEditScope = fieldScope !== undefined && !fieldScope.readOnly;
  const initialDiseases = fieldScope?.diseases ?? NO_DISEASES;
  const initialScope: FieldScope = initialDiseases.length ? "disease" : "base";
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
  const [scope, setScope] = useState<FieldScope>(initialScope);
  const [diseases, setDiseases] = useState<string[]>(initialDiseases);
  const [scopeError, setScopeError] = useState("");
  // The definition of a field being created; unused when mapping one.
  const [displayName, setDisplayName] = useState("");
  const [newFieldName, setNewFieldName] = useState("");
  const [valueType, setValueType] = useState<ValueType>("text");
  const [fieldMode, setFieldMode] = useState<FieldMode>("editable");
  const [formula, setFormula] = useState("");
  const [tab, setTab] = useState(create?.tab ?? "");
  const [confirmed, setConfirmed] = useState(false);
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
  // Seeded mappings use database names (measurement, condition_occurrence),
  // while curator-created mappings can use model names (Measurement, etc.).
  // Match either spelling for display, but preserve the stored value on save:
  // merely opening the dialog must not count as a recipe/provenance change.
  const selectedTableOption = OMOP_TABLES.find(
    (table) => table.toLowerCase() === omopTable.trim().replace(/_/g, "").toLowerCase(),
  );

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
    setSearchQuery(
      creating
        ? displayName.trim() || newFieldName.replace(/_/g, " ")
        : suggestQuery || fieldName.replace(/_/g, " "),
    );
  };

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(searchQuery), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [searchQuery, doSearch]);

  const effectiveUnit = isCustomUnit ? customUnit || unit : unit;

  // The scope to save: ticked categories count only while the choice is
  // disease-specific, and the base record is no categories at all.
  const scopedDiseases = scope === "disease" ? [...diseases].sort() : NO_DISEASES;
  // Choosing disease-specific is a change even before a category is ticked,
  // so that Save can say one is needed.
  const scopeDirty = canEditScope && (
    scope !== initialScope
    || JSON.stringify(scopedDiseases) !== JSON.stringify([...initialDiseases].sort())
  );
  // Whether anything but the scope differs from what the mapping row holds.
  const mappingDirty = (selected?.concept_id ?? null) !== (initialConceptId ?? null)
    || effectiveUnit !== (storedUnit ?? initialUnit ?? "")
    || omopTable !== (storedOmopTable ?? initialOmopTable ?? "Measurement")
    || notes !== (initialNotes || "")
    || mappingStatus !== (initialStatus || "proposed");
  // A field with no mapping row and no concept chosen: only its scope can be saved.
  const scopeOnly = !selected && !isEditing;
  // A save that changes the scope and nothing else is a membership edit by
  // the field's name. It creates no empty mapping row, leaves an existing one
  // untouched (so nothing is re-projected over the patient records), and no
  // rule about mappings can refuse it.
  const scopeSave = scopeDirty && (scopeOnly || !mappingDirty);

  // What a new field needs before it can be created: its definition, a
  // concept, and the confirmation the create endpoint requires.
  const createMissing = [
    !displayName.trim() && "a display name",
    !newFieldName.trim() && "a field name",
    fieldMode === "computed" && !formula.trim() && "a formula",
    !selected && "a concept",
    !confirmed && "the confirmation",
  ].filter(Boolean).join(", ");
  const createReady = !createMissing;

  const handleSubmit = async () => {
    if (creating ? !createReady : scopeOnly && !scopeDirty) return;
    // Disease-specific with no category is not an answer: saved as sent it
    // would make the field base, which is the other choice.
    if ((creating || scopeDirty) && scope === "disease" && scopedDiseases.length === 0) {
      setScopeError("Choose at least one Disease Category, or make the field part of the base record.");
      return;
    }
    setSaving(true);
    setError("");
    setScopeError("");
    try {
      if (creating) {
        // The definition, its approved mapping, its tab and its scope, in
        // one transaction on the server.
        await api.post("/v1/custom-patient-fields/", {
          confirm_patient_record: true,
          display_name: displayName.trim(),
          field_name: newFieldName.trim(),
          field_type: valueType,
          mode: fieldMode,
          formula: fieldMode === "computed" ? formula.trim() : undefined,
          tab,
          concept: selected?.concept_id,
          omop_table: omopTable,
          unit: effectiveUnit,
          notes,
          diseases: scopedDiseases,
        });
        onSaved();
        return;
      }
      if (scopeSave) {
        await api.put(`/v1/field-mappings/${encodeURIComponent(fieldName)}/diseases/`, {
          diseases: scopedDiseases,
        });
        onSaved();
        return;
      }
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
      // Leaving `diseases` out changes nothing, so an edit that did not touch
      // the scope cannot undo another curator's.
      const scopePayload = scopeDirty ? { diseases: scopedDiseases } : {};
      if (isEditing) {
        await api.patch(`/v1/field-mappings/${existingMappingId}/`, { ...mappingPayload, ...scopePayload });
      } else {
        await api.post("/v1/field-mappings/", { field_name: fieldName, ...mappingPayload, ...scopePayload });
      }
      onSaved();
    } catch (err: unknown) {
      const response = err && typeof err === "object" && "response" in err
        ? (err as { response?: { status?: number; data?: unknown } }).response
        : undefined;
      const data = response?.data;
      if (scopeSave && response?.status === 404) {
        // The by-name endpoint knows no such field: one the server gives no scope.
        setScopeError("This field cannot have a scope.");
      } else if (data === undefined) {
        setError("Failed to save mapping.");
      } else if (data && typeof data === "object" && !Array.isArray(data) && "diseases" in data) {
        // A scope the server refused (an unknown category, or Other) is said
        // under the scope control; anything else it refused stays below.
        const { diseases: refused, ...rest } = data;
        setScopeError(errorText(refused) || "This scope was refused.");
        if (Object.keys(rest).length) setError(JSON.stringify(rest));
      } else if (creating && data && typeof data === "object" && !Array.isArray(data)) {
        // A refused definition names the input it is about.
        setError(Object.entries(data).map(([key, value]) => (
          key === "detail" || key === "non_field_errors" ? errorText(value) : `${key}: ${errorText(value)}`
        )).join(" "));
      } else {
        setError(JSON.stringify(data));
      }
    } finally {
      setSaving(false);
    }
  };

  // A create request cannot be taken back once sent: closing then would
  // read as cancelling a field that is added all the same, so the dialog
  // stays until the server answers.
  const closeBlocked = creating && saving;

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape" && !closeBlocked) onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose, closeBlocked]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    // A stray click beside a half-filled definition must not discard it.
    if (!creating && e.target === overlayRef.current) onClose();
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

  const handleSelectCandidate = (c: ConceptResult) => {
    setSelected(c);
    if (c.suggested_unit && !unit) {
      setUnit(c.suggested_unit);
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
            disabled={closeBlocked}
            className="absolute right-3 top-3 rounded p-1 text-slate-400 hover:text-slate-600 disabled:opacity-50"
          >
            <X size={16} />
          </button>

          <h2 id="concept-assign-dialog-title" className="text-lg font-semibold text-slate-900">
            {creating ? "Add Field" : isEditing ? "Edit Concept Mapping" : "Assign Concept"}
          </h2>
          {creating ? (
            <p className="mt-1 text-sm text-slate-500">
              A new PatientRecord field. Choosing its concept and saving creates the field with an approved
              mapping. It can then be filled in on the patient tab; a value already in a patient's OMOP data
              appears once their record is next derived.
            </p>
          ) : (
            <p className="mt-1 text-sm text-slate-500">
              Field: <span className="font-mono">{fieldName}</span> ({fieldType})
            </p>
          )}
        </div>

        {/* Body */}
        <div className="max-h-[calc(100vh-12rem)] overflow-y-auto px-5 py-4">
          {/* ── NEW FIELD ──────────────────────────────────────────── */}
          {create && (
            <fieldset className="mb-4 rounded-md border border-slate-200 p-4">
              <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                New field — what is added to PatientRecord
              </legend>
              <div className="grid gap-4 md:grid-cols-2">
                <Field id="new_field_display_name" label="Display name" tip={TIP.display_name}>
                  <input
                    id="new_field_display_name"
                    type="text"
                    value={displayName}
                    onChange={(e) => {
                      // The field name follows the label until it is typed over.
                      if (newFieldName === nameFromLabel(displayName)) setNewFieldName(nameFromLabel(e.target.value));
                      setDisplayName(e.target.value);
                    }}
                    className={`${INPUT_CLASS} w-full`}
                    autoFocus
                  />
                </Field>
                <Field id="new_field_name" label="Field name" tip={TIP.field_name}>
                  <input
                    id="new_field_name"
                    type="text"
                    value={newFieldName}
                    onChange={(e) => setNewFieldName(e.target.value)}
                    placeholder="lower_snake_case"
                    className={`${INPUT_CLASS} w-full font-mono`}
                  />
                </Field>
                <Field id="new_field_type" label="Value type" tip={TIP.value_type}>
                  <select
                    id="new_field_type"
                    value={valueType}
                    onChange={(e) => setValueType(e.target.value as ValueType)}
                    className={`${INPUT_CLASS} w-full`}
                  >
                    <option value="text">Text</option>
                    <option value="number">Number</option>
                    <option value="date">Date</option>
                    <option value="boolean">Boolean</option>
                  </select>
                </Field>
                <Field id="new_field_tab" label="Tab" tip={TIP.tab}>
                  <select
                    id="new_field_tab"
                    value={tab}
                    onChange={(e) => setTab(e.target.value)}
                    className={`${INPUT_CLASS} w-full`}
                  >
                    {create.tabOptions.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </Field>
                <Field id="new_field_mode" label="Field mode" tip={TIP.field_mode}>
                  <select
                    id="new_field_mode"
                    value={fieldMode}
                    onChange={(e) => setFieldMode(e.target.value as FieldMode)}
                    className={`${INPUT_CLASS} w-full`}
                  >
                    <option value="editable">Editable</option>
                    <option value="computed">Computed from a formula</option>
                  </select>
                </Field>
                {fieldMode === "computed" && (
                  <Field id="new_field_formula" label="Formula" tip={TIP.formula}>
                    <textarea
                      id="new_field_formula"
                      value={formula}
                      onChange={(e) => setFormula(e.target.value)}
                      rows={2}
                      placeholder="e.g. weight / (height / 100) ^ 2"
                      className="w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm font-normal text-slate-950"
                    />
                  </Field>
                )}
              </div>
            </fieldset>
          )}

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
                    autoFocus={!creating}
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
                  <option value="CDISC">CDISC</option>
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
                    onClick={() => handleSelectCandidate(c)}
                    className={`grid w-full grid-cols-[8rem_1fr_6rem] gap-2 border-b border-slate-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-slate-50 ${
                      selected?.concept_id === c.concept_id ? "bg-blue-50" : ""
                    }`}
                  >
                    <span className="font-mono text-slate-700">{c.concept_code}</span>
                    <span className="text-slate-900">
                      {c.concept_name}
                      <ConceptInputDetails {...c} />
                    </span>
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
                {onShowSourceCodes && (
                  <button
                    type="button"
                    onClick={() => onShowSourceCodes(selected.concept_id)}
                    className="ml-auto rounded border border-slate-300 bg-white px-1.5 py-0.5 font-mono text-xs text-slate-600 hover:bg-slate-100"
                    aria-label={`Source codes for ${selected.concept_name || selected.concept_code}`}
                    title="Suggest source codes for this concept"
                  >
                    {"{codes}"}
                  </button>
                )}
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
                  value={selectedTableOption ?? omopTable}
                  onChange={(e) => setOmopTable(e.target.value)}
                  className={`${INPUT_CLASS} w-full`}
                >
                  {selectedTableOption === undefined && <option value={omopTable}>{omopTable}</option>}
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

          {/* Scope */}
          {fieldScope && (
            <FieldScopeControl
              scope={scope}
              diseases={diseases}
              options={fieldScope.options}
              optionsIncomplete={fieldScope.optionsIncomplete}
              error={scopeError}
              disabled={saving || fieldScope.readOnly}
              onChange={(nextScope, nextDiseases) => {
                setScope(nextScope);
                setDiseases(nextDiseases);
                setScopeError("");
              }}
            />
          )}

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

          {creating && (
            <label className="mb-4 flex items-start gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={saving}
                onChange={(e) => setConfirmed(e.target.checked)}
                className="mt-0.5"
              />
              I confirm that I want to add this field to PatientRecord.
            </label>
          )}

          {error && <div role={creating ? "alert" : undefined} className="mb-3 text-sm text-red-600">{error}</div>}
        </div>

        {/* Footer — status at bottom-left, buttons at bottom-right */}
        <div className="flex items-center justify-between gap-2 border-t border-slate-200 px-5 py-4">
          {creating ? (
            <p id="add-field-status" className="text-sm text-slate-600">
              {createMissing
                ? `Still needed: ${createMissing}.`
                : <>Status: <span className="font-medium text-slate-900">Approved</span> when the field is added</>}
            </p>
          ) : (
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
          )}
          <div className="flex gap-2">
            <button
              onClick={onClose}
              disabled={closeBlocked}
              className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={(creating ? !createReady : scopeOnly && !scopeDirty) || saving}
              aria-describedby={creating ? "add-field-status" : undefined}
              className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              title={creating || canApprove ? undefined : "Only org admins and staff can approve mappings. This will be saved as a proposal for review."}
            >
              {saving
                ? "Saving..."
                : creating
                  ? "Add Field"
                  : scopeSave
                  ? "Save Scope"
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
