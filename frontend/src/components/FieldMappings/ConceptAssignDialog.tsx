import { useCallback, useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import api from "@/api/axios";

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  standard_concept: string | null;
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
}

export function ConceptAssignDialog({
  fieldName, fieldType, onClose, onSaved,
  initialConceptCode, initialVocabularyId, initialUnit, initialOmopTable,
}: Props) {
  const [searchQuery, setSearchQuery] = useState(initialConceptCode || "");
  const [vocabFilter, setVocabFilter] = useState(initialVocabularyId || "");
  const [results, setResults] = useState<ConceptResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<ConceptResult | null>(null);
  const [unit, setUnit] = useState(initialUnit || "");
  const [omopTable, setOmopTable] = useState(initialOmopTable || "Measurement");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const overlayRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(searchQuery), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [searchQuery, doSearch]);

  const handleSubmit = async () => {
    if (!selected) return;
    setSaving(true);
    setError("");
    try {
      await api.post("/v1/field-mappings/", {
        field_name: fieldName,
        concept: selected.concept_id,
        vocabulary_id: selected.vocabulary_id,
        concept_code: selected.concept_code,
        unit,
        omop_table: omopTable,
        notes,
        status: "proposed",
      });
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

  // Close on Escape key
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  // Close on overlay click
  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
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

        <h2 className="mb-1 text-lg font-semibold">Assign Concept</h2>
        <p className="mb-4 text-sm text-gray-500">
          Field: <span className="font-mono">{fieldName}</span> ({fieldType})
        </p>

        {/* Concept search */}
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
                    onClick={() => setSelected(c)}
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

        {/* Selected concept */}
        {selected && (
          <div className="mb-4 rounded bg-blue-50 px-3 py-2 text-sm">
            Selected:{" "}
            <span className="font-medium">
              {selected.vocabulary_id}:{selected.concept_code}
            </span>{" "}
            — {selected.concept_name}
          </div>
        )}

        {/* Additional fields */}
        <div className="mb-4 grid grid-cols-2 gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600">Unit</label>
            <input
              type="text"
              value={unit}
              onChange={(e) => setUnit(e.target.value)}
              placeholder="e.g. mg/dL"
              className="h-8 w-full rounded border border-gray-300 px-2 text-sm"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600">OMOP Table</label>
            <select
              value={omopTable}
              onChange={(e) => setOmopTable(e.target.value)}
              className="h-8 w-full rounded border border-gray-300 px-2 text-sm"
            >
              <option value="Measurement">Measurement</option>
              <option value="Observation">Observation</option>
              <option value="ConditionOccurrence">ConditionOccurrence</option>
              <option value="ProcedureOccurrence">ProcedureOccurrence</option>
            </select>
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
            disabled={!selected || saving}
            className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Mapping"}
          </button>
        </div>
      </div>
    </div>
  );
}
