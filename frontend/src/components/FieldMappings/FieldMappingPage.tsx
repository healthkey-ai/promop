import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, ChevronDown, ChevronRight, Search, BookOpen, Check, X, Pencil } from "lucide-react";
import api from "@/api/axios";
import { ConceptAssignDialog } from "./ConceptAssignDialog";
import { SynonymDialog } from "./SynonymDialog";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { FormulaEditDialog } from "./FormulaEditDialog";
import { DerivationInfoDialog } from "./DerivationInfoDialog";

interface FieldDescriptor {
  field_name: string;
  field_type: string;
  category: string;
  tab: string;
  provenance: {
    omop_table: string;
    lookup_strategy: string;
    concept_codes: string[] | null;
    source_values: string[] | null;
    extractor: string;
    selection_rule: string;
    description: string;
  } | null;
  mapping: {
    id: number;
    concept_id: number | null;
    concept_name: string;
    vocabulary_id: string;
    concept_code: string;
    unit: string;
    omop_table: string;
    status: string;
    reviewer: string | null;
    reviewed_at: string | null;
    notes: string;
  } | null;
  suggestion: {
    concept_code: string;
    vocabulary_id: string | null;
    unit: string | null;
    omop_table: string;
    common_units: string[];
  } | null;
  mappable: boolean;
  locked_table: string | null;
  choices: {
    id: number;
    display: string;
    sort_order: number;
    codes: { code: string; vocabulary_id: string; display: string; is_primary: boolean }[];
  }[];
  formula: {
    id: number;
    expression: string;
    is_active: boolean;
  } | null;
  derivation_error: string | null;
}

const CATEGORY_LABELS: Record<string, string> = {
  "needs-concept-set": "Needs Concept Assignment",
  editable: "Mapped",
  "therapy-inference": "Therapy Inference",
  computed: "Computed",
  alias: "Legacy Aliases",
  unit: "Unit Fields",
  profile: "Person",
  location: "Location",
  other: "Other",
};

const TAB_LABELS: Record<string, string> = {
  general: "General",
  disease: "Disease",
  treatment: "Treatment",
  blood: "Blood",
  labs: "Labs",
  behavior: "Behavior",
  other: "Other",
};

const TAB_ORDER = ["general", "disease", "treatment", "blood", "labs", "behavior", "other"];

const STATUS_BADGE: Record<string, string> = {
  proposed: "bg-yellow-100 text-yellow-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
};

/** Display category: approved mappings move from their backend category to "editable" (Mapped). */
const getDisplayCategory = (d: FieldDescriptor): string => {
  if (d.category === "computed") return "computed";
  if (d.mapping?.status === "approved" && d.category !== "editable") return "editable";
  return d.category;
};

export default function FieldMappingPage() {
  const navigate = useNavigate();
  const [descriptors, setDescriptors] = useState<FieldDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [activeTab, setActiveTab] = useState("general");
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(
    new Set(["alias", "unit", "other", "computed"])
  );
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedField, setSelectedField] = useState<FieldDescriptor | null>(null);
  const [synonymDialogField, setSynonymDialogField] = useState<string | null>(null);
  const [batchSynonyms, setBatchSynonyms] = useState<Record<string, string[]>>({});
  const [choiceEditorField, setChoiceEditorField] = useState<FieldDescriptor | null>(null);
  const [formulaEditorField, setFormulaEditorField] = useState<FieldDescriptor | null>(null);
  const [derivationInfoField, setDerivationInfoField] = useState<FieldDescriptor | null>(null);

  const fetchDescriptors = useCallback(async (autoPropose = false) => {
    setLoading(true);
    setError("");
    try {
      const resp = await api.get("/v1/field-mappings/");
      setDescriptors(resp.data);

      // Auto-propose mappings only on initial mount, not on every refetch.
      if (autoPropose) {
        const hasUnmapped = resp.data.some(
          (d: FieldDescriptor) => d.mappable && !d.mapping && d.suggestion
        );
        if (hasUnmapped) {
          try {
            const proposeResp = await api.post("/v1/field-mappings/propose-all/");
            if (proposeResp.data.created > 0) {
              // Re-fetch to pick up newly created proposed mappings.
              const refreshed = await api.get("/v1/field-mappings/");
              setDescriptors(refreshed.data);
            }
          } catch {
            // Non-critical — proposed mappings are a convenience, not required.
          }
        }
      }
    } catch {
      setError("Failed to load field mappings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await fetchDescriptors(true);
    })();
  }, [fetchDescriptors]);

  const fetchBatchSynonyms = useCallback(async (fieldNames?: string[]) => {
    const names = fieldNames || descriptors
      .filter((d) => d.tab === activeTab)
      .map((d) => d.field_name);
    if (!names.length) return;
    try {
      const resp = await api.get(`/v1/field-synonyms/batch/?fields=${names.join(",")}`);
      setBatchSynonyms((prev) => ({ ...prev, ...resp.data }));
    } catch {
      // Silently fail — synonyms are non-critical.
    }
  }, [descriptors, activeTab]);

  // Load batch synonyms for current tab's fields.
  useEffect(() => {
    if (!descriptors.length) return;
    (async () => {
      await fetchBatchSynonyms();
    })();
  }, [descriptors, activeTab, fetchBatchSynonyms]);

  // When searching, show across all tabs; otherwise filter by active tab.
  const filtered = useMemo(() => {
    let items = descriptors;
    if (!searchQuery) {
      items = items.filter((d) => d.tab === activeTab);
    } else {
      const q = searchQuery.toLowerCase();
      items = items.filter((d) => d.field_name.toLowerCase().includes(q));
    }
    if (categoryFilter) {
      items = items.filter((d) => d.category === categoryFilter);
    }
    return items;
  }, [descriptors, categoryFilter, searchQuery, activeTab]);

  // Split into mappable and computed groups.
  const { mappableGroups, computedFields } = useMemo(() => {
    const mappable: Record<string, FieldDescriptor[]> = {};
    const computed: FieldDescriptor[] = [];
    for (const d of filtered) {
      const displayCat = getDisplayCategory(d);
      if (displayCat === "computed") {
        computed.push(d);
      } else {
        if (!mappable[displayCat]) mappable[displayCat] = [];
        mappable[displayCat].push(d);
      }
    }
    return { mappableGroups: mappable, computedFields: computed };
  }, [filtered]);

  const stats = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of descriptors) {
      const displayCat = getDisplayCategory(d);
      counts[displayCat] = (counts[displayCat] || 0) + 1;
    }
    return counts;
  }, [descriptors]);

  const tabCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of descriptors) {
      counts[d.tab] = (counts[d.tab] || 0) + 1;
    }
    return counts;
  }, [descriptors]);

  const toggleSection = (cat: string) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  const handleCellClick = (field: FieldDescriptor) => {
    if (!field.mappable) return;
    setSelectedField(field);
    setDialogOpen(true);
  };

  const handleMappingSaved = () => {
    setDialogOpen(false);
    setSelectedField(null);
    fetchDescriptors();
  };

  const handleDeleteMapping = async (mappingId: number) => {
    if (!window.confirm("Remove this concept mapping? This cannot be undone.")) return;
    try {
      await api.delete(`/v1/field-mappings/${mappingId}/`);
      fetchDescriptors();
    } catch {
      setError("Failed to remove mapping.");
    }
  };

  const handleConfirm = async (field: FieldDescriptor) => {
    try {
      if (field.mapping) {
        // This is a state toggle, not a one-way confirmation.  An approved
        // mapping is still retained as a proposal when a reviewer unchecks it.
        const status = field.mapping.status === "approved" ? "proposed" : "approved";
        await api.patch(`/v1/field-mappings/${field.mapping.id}/`, { status });
        await fetchDescriptors();
      } else if (field.suggestion) {
        // Suggestion has no resolved concept FK — open the dialog so the user
        // can search, select a concept, and create a complete mapping.
        handleCellClick(field);
      }
    } catch {
      setError("Failed to confirm mapping.");
    }
  };

  /** Render the concept cell content (code + status badge). */
  const renderConceptCell = (f: FieldDescriptor) => {
    if (f.mapping) {
      return (
        <span className="inline-flex items-center gap-1.5">
          <span className={`font-mono text-xs ${f.mapping.status === "proposed" ? "font-bold" : ""}`}>
            {f.mapping.concept_code}
          </span>
          <span
            className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
              STATUS_BADGE[f.mapping.status] || "bg-gray-100"
            }`}
          >
            {f.mapping.status}
          </span>
        </span>
      );
    }
    if (f.suggestion) {
      return (
        <span className="font-mono text-xs font-bold text-gray-700">
          {f.suggestion.concept_code}
        </span>
      );
    }
    return (
      <span className="text-xs text-gray-400 group-hover:text-gray-500">
        click to map
      </span>
    );
  };

  /** Render coding (vocabulary_id) cell. */
  const renderCodingCell = (f: FieldDescriptor) => {
    if (f.mapping?.vocabulary_id) {
      return <span className="text-xs">{f.mapping.vocabulary_id}</span>;
    }
    return <span className="text-xs text-gray-400">&mdash;</span>;
  };

  /** Render table (omop_table) cell. */
  const renderTableCell = (f: FieldDescriptor) => {
    if (f.locked_table) {
      return (
        <span className="text-xs text-gray-400" title={
          f.category === "location"
            ? "Location is an independent table — no OMOP mapping needed"
            : "Person table (locked)"
        }>
          {f.locked_table}
        </span>
      );
    }
    if (f.mapping?.omop_table) {
      return <span className="text-xs">{f.mapping.omop_table}</span>;
    }
    return <span className="text-xs text-gray-400">&mdash;</span>;
  };

  /** Render inline synonyms. */
  const renderSynonyms = (f: FieldDescriptor) => {
    const syns = batchSynonyms[f.field_name] || [];
    return (
      <button
        onClick={() => setSynonymDialogField(f.field_name)}
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-gray-600 hover:bg-gray-100"
        title="Manage synonyms"
      >
        <BookOpen size={12} />
        {syns.length > 0 ? (
          <span className="max-w-[150px] truncate text-gray-500">
            {syns.slice(0, 3).join(", ")}
            {syns.length > 3 && <span className="ml-1 text-gray-400">+{syns.length - 3}</span>}
          </span>
        ) : (
          <span className="text-gray-400">none</span>
        )}
      </button>
    );
  };

  /** Render table for a category section (mappable fields). */
  const renderMappableTable = (fields: FieldDescriptor[]) => (
    <div className="overflow-x-auto rounded-b-md border border-t-0 border-gray-200">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
            <th className="px-3 py-2">Field Name</th>
            <th className="px-3 py-2">
              <span className="inline-flex items-center gap-1">
                Concept
                <span className="text-[9px] normal-case text-gray-400">Confirm</span>
              </span>
            </th>
            <th className="px-3 py-2">Coding</th>
            <th className="px-3 py-2">Table</th>
            <th className="px-3 py-2">Synonyms</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {fields.map((f) => (
            <tr key={f.field_name} className="group hover:bg-gray-50/50">
              <td className="px-3 py-2 font-mono text-xs">{f.field_name}</td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-1.5">
                  {f.mappable && (
                    <button
                      onClick={() => handleConfirm(f)}
                      className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                        f.mapping?.status === "approved"
                          ? "border-green-500 bg-green-500 text-white"
                          : "border-gray-300 hover:border-primary"
                      }`}
                      title={f.mapping?.status === "approved" ? "Mark mapping as proposed" : "Approve mapping"}
                      disabled={!f.mapping && !f.suggestion}
                    >
                      {f.mapping?.status === "approved" && <Check size={10} />}
                    </button>
                  )}
                  <button
                    onClick={() => f.mappable && handleCellClick(f)}
                    className={`inline-flex items-center gap-1 ${f.mappable ? "cursor-pointer hover:text-primary hover:underline" : ""}`}
                    disabled={!f.mappable}
                    title={f.mappable ? "Click to assign or edit concept" : undefined}
                  >
                    {renderConceptCell(f)}
                    {f.mappable && (
                      <Pencil size={10} className="opacity-0 group-hover:opacity-100 text-gray-400" />
                    )}
                  </button>
                  {f.mapping && (
                    <button
                      onClick={() => handleDeleteMapping(f.mapping!.id)}
                      className="ml-1 rounded p-0.5 text-gray-400 hover:bg-red-50 hover:text-red-500"
                      title="Remove mapping"
                    >
                      <X size={10} />
                    </button>
                  )}
                </div>
              </td>
              <td className="px-3 py-2">
                <button
                  onClick={() => f.mappable && handleCellClick(f)}
                  className={`${f.mappable ? "cursor-pointer hover:text-primary" : ""}`}
                  disabled={!f.mappable}
                >
                  {renderCodingCell(f)}
                </button>
              </td>
              <td className="px-3 py-2">
                {f.locked_table ? (
                  renderTableCell(f)
                ) : (
                  <button
                    onClick={() => f.mappable && handleCellClick(f)}
                    className={`${f.mappable ? "cursor-pointer hover:text-primary" : ""}`}
                    disabled={!f.mappable}
                  >
                    {renderTableCell(f)}
                  </button>
                )}
              </td>
              <td className="px-3 py-2">{renderSynonyms(f)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  /** Render computed fields section (read-only, at bottom). */
  const renderComputedSection = () => {
    if (computedFields.length === 0) return null;
    const isCollapsed = collapsedSections.has("computed");
    return (
      <div className="mb-3">
        <button
          onClick={() => toggleSection("computed")}
          className="flex w-full items-center gap-2 rounded bg-gray-50 px-3 py-2 text-left text-sm font-medium text-gray-500 hover:bg-gray-100"
        >
          {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          Computed
          <span className="ml-1 text-xs text-gray-400">({computedFields.length})</span>
          <span className="ml-2 text-[10px] font-normal italic text-gray-400">
            read-only — computed by application code
          </span>
        </button>
        {!isCollapsed && (
          <div className="overflow-x-auto rounded-b-md border border-t-0 border-gray-200">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
                  <th className="px-3 py-2">Field Name</th>
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Formula</th>
                  <th className="px-3 py-2">Provenance</th>
                  <th className="px-3 py-2">Derivation status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {computedFields.map((f) => (
                  <tr key={f.field_name} className="text-gray-400 italic">
                    <td className="px-3 py-2 font-mono text-xs">{f.field_name}</td>
                    <td className="px-3 py-2 text-xs">{f.field_type}</td>
                    <td className="px-3 py-2 text-xs">
                      <button
                        onClick={() => setFormulaEditorField(f)}
                        className="not-italic hover:text-primary hover:underline"
                      >
                        {f.formula ? (
                          <span className="font-mono">{f.formula.expression}</span>
                        ) : (
                          <span className="text-gray-400">none</span>
                        )}
                      </button>
                    </td>
                    <td className="px-3 py-2 text-xs">
                      <button
                        onClick={() => setDerivationInfoField(f)}
                        className="not-italic text-left hover:text-primary hover:underline"
                        title="View read-only derivation details"
                      >
                        {f.provenance ? (
                          <span title={f.provenance.description}>
                          {f.provenance.lookup_strategy} / {f.provenance.omop_table}
                          </span>
                        ) : "application code"}
                      </button>
                    </td>
                    <td className="px-3 py-2 text-xs not-italic">
                      {f.derivation_error ? (
                        <span className="font-medium text-red-600" title={f.derivation_error}>
                          Error in derivation
                        </span>
                      ) : (
                        <span className="text-gray-400">OK</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  };

  if (loading) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-8 animate-pulse rounded bg-gray-200" />
          ))}
        </div>
      </div>
    );
  }

  if (error && !descriptors.length) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <div className="rounded border border-red-300 bg-red-50 p-4 text-red-700">
          {error}
          <button onClick={fetchDescriptors} className="ml-3 underline">Retry</button>
        </div>
      </div>
    );
  }

  const categoryOrder = Object.keys(CATEGORY_LABELS).filter((c) => c !== "computed");

  return (
    <div className="mx-auto max-w-7xl p-6">
      {/* Header */}
      <div className="mb-6 flex items-center gap-4">
        <button
          onClick={() => navigate("/")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft size={14} />
          Back
        </button>
        <h1 className="text-xl font-semibold">Field Concept Mappings</h1>
      </div>

      {/* Tab bar */}
      <div className="mb-4 flex gap-0 overflow-x-auto border-b border-gray-200">
        {TAB_ORDER.map((tab) => {
          const count = tabCounts[tab] || 0;
          if (!count) return null;
          return (
            <button
              key={tab}
              onClick={() => { setActiveTab(tab); setSearchQuery(""); }}
              className={`whitespace-nowrap border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
                activeTab === tab && !searchQuery
                  ? "border-primary text-primary"
                  : "border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700"
              }`}
            >
              {TAB_LABELS[tab] || tab}
              <span className="ml-1.5 text-xs text-gray-400">({count})</span>
            </button>
          );
        })}
      </div>

      {/* Stats bar */}
      <div className="mb-4 flex flex-wrap gap-2 text-xs">
        {categoryOrder.map((cat) =>
          stats[cat] ? (
            <span
              key={cat}
              className={`rounded-full px-2.5 py-1 font-medium ${
                cat === "needs-concept-set"
                  ? "bg-amber-100 text-amber-800"
                  : "bg-gray-100 text-gray-600"
              }`}
            >
              {CATEGORY_LABELS[cat] || cat}: {stats[cat]}
            </span>
          ) : null
        )}
        {stats["computed"] ? (
          <span className="rounded-full bg-gray-100 px-2.5 py-1 font-medium text-gray-500 italic">
            Computed: {stats["computed"]}
          </span>
        ) : null}
        <span className="rounded-full bg-blue-100 px-2.5 py-1 font-medium text-blue-800">
          Total: {descriptors.length}
        </span>
      </div>

      {/* Filter bar */}
      <div className="mb-4 flex flex-wrap gap-3">
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-2.5 text-gray-400" />
          <input
            type="text"
            placeholder="Search fields (all tabs)..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-9 w-64 rounded border border-gray-300 pl-8 pr-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="h-9 rounded border border-gray-300 px-3 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="">All categories</option>
          {Object.entries(CATEGORY_LABELS).map(([cat, label]) => (
            <option key={cat} value={cat}>
              {label} ({stats[cat] || 0})
            </option>
          ))}
        </select>
      </div>

      {/* Error banner (non-fatal) */}
      {error && (
        <div className="mb-4 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Search mode indicator */}
      {searchQuery && (
        <div className="mb-3 text-sm text-gray-500">
          Showing results across all tabs for &quot;{searchQuery}&quot; ({filtered.length} fields)
        </div>
      )}

      {/* Mappable category sections */}
      {categoryOrder
        .filter((cat) => mappableGroups[cat]?.length)
        .map((cat) => {
          const isCollapsed = collapsedSections.has(cat);
          const fields = mappableGroups[cat];
          return (
            <div key={cat} className="mb-3">
              <button
                onClick={() => toggleSection(cat)}
                className={`flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm font-medium hover:bg-gray-100 ${
                  cat === "needs-concept-set"
                    ? "bg-amber-50 text-amber-800"
                    : "bg-gray-50 text-gray-700"
                }`}
              >
                {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                {CATEGORY_LABELS[cat] || cat}
                <span className="ml-1 text-xs text-gray-400">({fields.length})</span>
              </button>
              {!isCollapsed && renderMappableTable(fields)}
            </div>
          );
        })}

      {/* Computed section at bottom */}
      {renderComputedSection()}

      {/* No results */}
      {Object.keys(mappableGroups).length === 0 && computedFields.length === 0 && (
        <div className="py-8 text-center text-sm text-gray-400">
          No fields match the current filters.
        </div>
      )}

      {/* Concept Assign Dialog */}
      {dialogOpen && selectedField && (
        <ConceptAssignDialog
          fieldName={selectedField.field_name}
          fieldType={selectedField.field_type}
          initialConceptCode={selectedField.mapping?.concept_code || selectedField.suggestion?.concept_code}
          initialVocabularyId={selectedField.mapping?.vocabulary_id || (selectedField.suggestion?.vocabulary_id ?? undefined)}
          initialUnit={selectedField.mapping?.unit || (selectedField.suggestion?.unit ?? undefined)}
          initialOmopTable={selectedField.locked_table || selectedField.mapping?.omop_table || selectedField.suggestion?.omop_table || undefined}
          existingMappingId={selectedField.mapping?.id}
          initialConceptId={selectedField.mapping?.concept_id}
          initialConceptName={selectedField.mapping?.concept_name}
          initialNotes={selectedField.mapping?.notes}
          commonUnits={selectedField.suggestion?.common_units}
          choices={selectedField.choices}
          onEditChoices={() => {
            const fieldToEdit = selectedField;
            setDialogOpen(false);
            setSelectedField(null);
            setChoiceEditorField(fieldToEdit);
          }}
          onClose={() => {
            setDialogOpen(false);
            setSelectedField(null);
          }}
          onSaved={handleMappingSaved}
        />
      )}

      {/* Synonym Dialog */}
      {synonymDialogField && (
        <SynonymDialog
          fieldName={synonymDialogField}
          onClose={() => {
            const fieldName = synonymDialogField;
            setSynonymDialogField(null);
            fetchBatchSynonyms([fieldName]);
          }}
        />
      )}

      {/* Field Choice Editor */}
      {choiceEditorField && (
        <FieldChoiceEditor
          fieldName={choiceEditorField.field_name}
          onClose={() => {
            setChoiceEditorField(null);
            fetchDescriptors();
          }}
        />
      )}

      {/* Formula Edit Dialog */}
      {formulaEditorField && (
        <FormulaEditDialog
          fieldName={formulaEditorField.field_name}
          fieldType={formulaEditorField.field_type}
          existingFormula={formulaEditorField.formula}
          onClose={() => {
            setFormulaEditorField(null);
            fetchDescriptors();
          }}
        />
      )}

      {derivationInfoField && (
        <DerivationInfoDialog
          fieldName={derivationInfoField.field_name}
          provenance={derivationInfoField.provenance}
          onClose={() => setDerivationInfoField(null)}
        />
      )}
    </div>
  );
}
