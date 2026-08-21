import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, ChevronDown, ChevronRight, Search, Plus } from "lucide-react";
import api from "@/api/axios";
import { ConceptAssignDialog } from "./ConceptAssignDialog";

interface FieldDescriptor {
  field_name: string;
  field_type: string;
  category: string;
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
    vocabulary_id: string;
    concept_code: string;
    unit: string;
    omop_table: string;
    status: string;
    reviewer: string | null;
    reviewed_at: string | null;
    notes: string;
  } | null;
}

const CATEGORY_LABELS: Record<string, string> = {
  "needs-concept-set": "Needs Concept Assignment",
  editable: "Mapped & Editable (LOINC)",
  "therapy-inference": "Therapy Inference",
  computed: "Computed / Wearable",
  alias: "Legacy Aliases",
  unit: "Unit Fields",
  profile: "Person / Profile",
  location: "Location",
  "wearable-metadata": "Wearable Metadata",
  internal: "Internal / Structural",
  other: "Other",
};

const STATUS_BADGE: Record<string, string> = {
  proposed: "bg-yellow-100 text-yellow-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
};

export default function FieldMappingPage() {
  const navigate = useNavigate();
  const [descriptors, setDescriptors] = useState<FieldDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(
    new Set(["editable", "alias", "unit", "profile", "location", "internal", "wearable-metadata", "other", "computed"])
  );
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedField, setSelectedField] = useState<FieldDescriptor | null>(null);

  const fetchDescriptors = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const resp = await api.get("/v1/field-mappings/");
      setDescriptors(resp.data);
    } catch {
      setError("Failed to load field mappings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDescriptors();
  }, [fetchDescriptors]);

  const filtered = useMemo(() => {
    let items = descriptors;
    if (categoryFilter) {
      items = items.filter((d) => d.category === categoryFilter);
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      items = items.filter((d) => d.field_name.toLowerCase().includes(q));
    }
    return items;
  }, [descriptors, categoryFilter, searchQuery]);

  const grouped = useMemo(() => {
    const groups: Record<string, FieldDescriptor[]> = {};
    for (const d of filtered) {
      const cat = d.category;
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(d);
    }
    return groups;
  }, [filtered]);

  const stats = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const d of descriptors) {
      counts[d.category] = (counts[d.category] || 0) + 1;
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

  const handleAssignClick = (field: FieldDescriptor) => {
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

  const handleApproveMapping = async (mappingId: number) => {
    try {
      await api.patch(`/v1/field-mappings/${mappingId}/`, { status: "approved" });
      fetchDescriptors();
    } catch {
      setError("Failed to approve mapping.");
    }
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

  if (error) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <div className="rounded border border-red-300 bg-red-50 p-4 text-red-700">
          {error}
          <button onClick={fetchDescriptors} className="ml-3 underline">Retry</button>
        </div>
      </div>
    );
  }

  const categoryOrder = Object.keys(CATEGORY_LABELS);

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
            placeholder="Search fields..."
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
          {categoryOrder.map((cat) => (
            <option key={cat} value={cat}>
              {CATEGORY_LABELS[cat] || cat} ({stats[cat] || 0})
            </option>
          ))}
        </select>
      </div>

      {/* Grouped sections */}
      {categoryOrder
        .filter((cat) => grouped[cat]?.length)
        .map((cat) => {
          const isCollapsed = collapsedSections.has(cat);
          const fields = grouped[cat];
          return (
            <div key={cat} className="mb-3">
              <button
                onClick={() => toggleSection(cat)}
                className="flex w-full items-center gap-2 rounded bg-gray-50 px-3 py-2 text-left text-sm font-medium text-gray-700 hover:bg-gray-100"
              >
                {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                {CATEGORY_LABELS[cat] || cat}
                <span className="ml-1 text-xs text-gray-400">({fields.length})</span>
              </button>
              {!isCollapsed && (
                <div className="overflow-x-auto rounded-b-md border border-t-0 border-gray-200">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-left text-[11px] uppercase text-gray-500">
                        <th className="px-3 py-2">Field Name</th>
                        <th className="px-3 py-2">Type</th>
                        <th className="px-3 py-2">Concept / Status</th>
                        <th className="px-3 py-2">Provenance</th>
                        <th className="px-3 py-2 text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {fields.map((f) => (
                        <tr key={f.field_name} className="hover:bg-gray-50/50">
                          <td className="px-3 py-2 font-mono text-xs">{f.field_name}</td>
                          <td className="px-3 py-2 text-gray-500">{f.field_type}</td>
                          <td className="px-3 py-2">
                            {f.mapping ? (
                              <span className="inline-flex items-center gap-1.5">
                                <span className="font-mono text-xs">
                                  {f.mapping.vocabulary_id}:{f.mapping.concept_code}
                                </span>
                                <span
                                  className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                                    STATUS_BADGE[f.mapping.status] || "bg-gray-100"
                                  }`}
                                >
                                  {f.mapping.status}
                                </span>
                              </span>
                            ) : f.provenance?.concept_codes?.length ? (
                              <span className="font-mono text-xs text-gray-500">
                                {f.provenance.concept_codes.join(", ")}
                              </span>
                            ) : (
                              <span className="text-xs text-gray-400">—</span>
                            )}
                          </td>
                          <td className="px-3 py-2 text-xs text-gray-500">
                            {f.provenance ? (
                              <span title={f.provenance.description}>
                                {f.provenance.lookup_strategy} / {f.provenance.omop_table}
                              </span>
                            ) : (
                              "—"
                            )}
                          </td>
                          <td className="px-3 py-2 text-right">
                            <div className="flex items-center justify-end gap-1">
                              {f.mapping ? (
                                <>
                                  {f.mapping.status === "proposed" && (
                                    <button
                                      onClick={() => handleApproveMapping(f.mapping!.id)}
                                      className="rounded px-2 py-0.5 text-xs text-green-700 hover:bg-green-50"
                                    >
                                      Approve
                                    </button>
                                  )}
                                  <button
                                    onClick={() => handleDeleteMapping(f.mapping!.id)}
                                    className="rounded px-2 py-0.5 text-xs text-red-600 hover:bg-red-50"
                                  >
                                    Remove
                                  </button>
                                </>
                              ) : cat === "needs-concept-set" || cat === "other" ? (
                                <button
                                  onClick={() => handleAssignClick(f)}
                                  className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs text-primary hover:bg-primary/10"
                                >
                                  <Plus size={12} />
                                  Assign
                                </button>
                              ) : null}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}

      {/* Dialog */}
      {dialogOpen && selectedField && (
        <ConceptAssignDialog
          fieldName={selectedField.field_name}
          fieldType={selectedField.field_type}
          onClose={() => {
            setDialogOpen(false);
            setSelectedField(null);
          }}
          onSaved={handleMappingSaved}
        />
      )}
    </div>
  );
}
