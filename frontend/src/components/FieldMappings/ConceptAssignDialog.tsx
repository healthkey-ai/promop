import { useCallback, useEffect, useRef, useState } from "react";
import { Search, Sparkles, X } from "lucide-react";
import api from "@/api/axios";

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
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

export function ConceptAssignDialog({
  fieldName, fieldType, onClose, onSaved,
  initialConceptCode, initialVocabularyId, initialUnit, initialOmopTable,
  existingMappingId, initialConceptId, initialConceptName, initialStatus, initialNotes, commonUnits,
  choices, onEditChoices, canApprove = true,
}: Props) {
  const isEditing = !!existingMappingId;
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
          standard_concept: null,
        }
      : null
  ));
  const [unit, setUnit] = useState(initialUnit || "");
  const [customUnit, setCustomUnit] = useState("");
  const [omopTable, setOmopTable] = useState(initialOmopTable || "Measurement");
  const [notes, setNotes] = useState(initialNotes || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

  const unitChoices = commonUnits || [];
  const hasCommonUnits = unitChoices.length > 0;
  const isCustomUnit = hasCommonUnits && unit !== "" && !unitChoices.includes(unit);
  const mappingState = initialStatus || (selected ? "proposed" : "unmapped");

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
      const mappingPayload = {
        concept: selected?.concept_id ?? null,
        vocabulary_id: selected?.vocabulary_id ?? "",
        concept_code: selected?.concept_code ?? "",
        unit: effectiveUnit,
        omop_table: omopTable,
        notes,
        // Clearing a mapping returns it to review rather than leaving an
        // approved row with no concept in the Mapped list.
        // Non-staff users can only propose, not approve.
        status: selected ? (canApprove ? "approved" : "proposed") : "proposed",
      };
      if (isEditing) {
        // An existing field already owns this mapping.  Update that row in
        // place and approve it; never send a create-style field_name payload
        // that could be interpreted as a duplicate mapping request.
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
      <div className="relative w-full max-w-2xl rounded-lg border bg-white p-6 shadow-xl">
        <button
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:text-gray-600"
        >
          <X size={16} />
        </button>

        <h2 className="mb-1 text-lg font-semibold">
          {isEditing ? "Edit Concept Mapping" : "Assign Concept"}
        </h2>
        <p className="mb-4 text-sm text-gray-500">
          Field: <span className="font-mono">{fieldName}</span> ({fieldType})
        </p>

        {/* Mapping summary comes before search so the current decision is clear. */}
        <div className="mb-3">
          <div className="mb-1 flex items-center justify-between text-xs font-medium text-gray-600">
            <span>Mapped Concept</span>
            <span className={`rounded-full px-2 py-0.5 text-[10px] capitalize ${
              mappingState === "approved" ? "bg-green-100 text-green-800" :
              mappingState === "proposed" ? "bg-amber-100 text-amber-800" :
              "bg-gray-100 text-gray-600"
            }`}>
              {mappingState}
            </span>
          </div>
          <div className="flex items-center gap-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm">
            {selected ? (
              <>
                <span className="font-mono font-medium">{selected.vocabulary_id}:{selected.concept_code}</span>
                <span className="min-w-0 break-words">{selected.concept_name || "Unnamed concept"}</span>
                <button onClick={() => setSelected(null)} className="rounded p-0.5 text-gray-400 hover:text-gray-600" title="Clear selection">
                  <X size={14} />
                </button>
              </>
            ) : <span className="text-gray-400">No concept selected</span>}
            <select
              aria-label="OMOP Table"
              value={omopTable}
              onChange={(e) => setOmopTable(e.target.value)}
              className="ml-auto h-8 max-w-48 rounded border border-gray-300 bg-white px-2 text-sm"
            >
              {OMOP_TABLES.map((table) => <option key={table} value={table}>{table}</option>)}
            </select>
          </div>
        </div>

        {/* Concept search */}
        <div className="mb-2 flex justify-end">
          <button
            type="button"
            onClick={handleSuggest}
            className="inline-flex items-center gap-1.5 rounded border border-gray-300 px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50"
          >
            <Sparkles size={13} />
            Suggest
          </button>
        </div>
        <div className="mb-3 flex gap-2">
          <div className="relative flex-1">
            <Search size={14} className="absolute left-2.5 top-2.5 text-gray-400" />
            <input
              type="text"
              placeholder="Search concepts..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-9 w-full rounded border border-gray-300 pl-8 pr-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              autoFocus
            />
          </div>
          <select
            value={vocabFilter}
            onChange={(e) => setVocabFilter(e.target.value)}
            className="h-9 rounded border border-gray-300 px-2 text-sm"
          >
            <option value="">All vocabularies</option>
            <option value="LOINC">LOINC</option>
            <option value="SNOMED">SNOMED</option>
            <option value="RxNorm">RxNorm</option>
            <option value="HemOnc">HemOnc</option>
          </select>
        </div>

        {/* Results */}
        <div className="mb-4 max-h-48 overflow-y-auto rounded border border-gray-200">
          {searching && (
            <div className="p-3 text-center text-sm text-gray-400">Searching...</div>
          )}
          {!searching && results.length === 0 && searchQuery.length >= 3 && (
            <div className="p-3 text-center text-sm text-gray-400">No results found.</div>
          )}
          {!searching && results.length > 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-gray-50 text-left text-[10px] uppercase text-gray-500">
                  <th className="px-2 py-1.5">Code</th>
                  <th className="px-2 py-1.5">Name</th>
                  <th className="px-2 py-1.5">Vocab</th>
                  <th className="px-2 py-1.5">Domain</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {results.slice(0, 50).map((c) => (
                  <tr
                    key={c.concept_id}
                    onClick={() => {
                      setSelected(c);
                      if (c.suggested_unit && !unit) {
                        setUnit(c.suggested_unit);
                      }
                    }}
                    className={`cursor-pointer hover:bg-blue-50 ${
                      selected?.concept_id === c.concept_id ? "bg-blue-100" : ""
                    }`}
                  >
                    <td className="px-2 py-1.5 font-mono">{c.concept_code}</td>
                    <td className="px-2 py-1.5">{c.concept_name}</td>
                    <td className="px-2 py-1.5">{c.vocabulary_id}</td>
                    <td className="px-2 py-1.5">{c.domain_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Additional fields */}
        <div className="mb-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600">
              Unit
              {selected?.suggested_unit && unit === selected.suggested_unit && (
                <span className="ml-1 text-[10px] font-normal text-gray-400">(suggested)</span>
              )}
            </label>
            {hasCommonUnits ? (
              <div className="space-y-1.5">
                <select
                  value={isCustomUnit ? "__custom__" : unit}
                  onChange={(e) => handleUnitDropdownChange(e.target.value)}
                  className="h-8 w-full rounded border border-gray-300 px-2 text-sm"
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
                    className="h-8 w-full rounded border border-gray-300 px-2 text-sm"
                  />
                )}
              </div>
            ) : (
              <input
                type="text"
                value={unit}
                onChange={(e) => setUnit(e.target.value)}
                placeholder="e.g. mg/dL"
                className="h-8 w-full rounded border border-gray-300 px-2 text-sm"
              />
            )}
          </div>
        </div>
        <div className="mb-4">
          <label className="mb-1 block text-xs font-medium text-gray-600">Notes</label>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className="w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            placeholder="Rationale for this mapping..."
          />
        </div>

        {/* Field Choices */}
        {choices !== undefined && (
          <div className="mb-4">
            <div className="mb-1 flex items-center gap-2">
              <label className="text-xs font-medium text-gray-600">
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
              <div className="max-h-28 overflow-y-auto rounded border border-gray-200 text-xs">
                <table className="w-full">
                  <tbody className="divide-y divide-gray-100">
                    {choices.map((ch) => (
                      <tr key={ch.id} className="hover:bg-gray-50">
                        <td className="px-2 py-1">{ch.display}</td>
                        <td className="px-2 py-1 text-gray-400">
                          {ch.codes.map((c) => `${c.vocabulary_id}:${c.code}`).join(", ") || "no codes"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="rounded border border-dashed border-gray-300 px-3 py-1.5 text-xs text-gray-400">
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

        {/* Footer */}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={(!selected && !isEditing) || saving}
            className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            title={canApprove ? undefined : "Only org admins and staff can approve mappings. This will be saved as a proposal for review."}
          >
            {saving ? "Saving..." : canApprove ? (isEditing ? "Update/Approve Mapping" : "Save Mapping") : (isEditing ? "Update Proposal" : "Save as Proposal")}
          </button>
        </div>
      </div>
    </div>
  );
}
