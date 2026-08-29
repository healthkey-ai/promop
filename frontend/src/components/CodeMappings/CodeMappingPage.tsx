import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, Pencil, Plus, Search, Sparkles, X } from "lucide-react";
import api from "@/api/axios";

interface CodeMappingRow {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  concept_vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  mapping_id: number | null;
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  source: string;
  status: "proposed" | "approved" | "rejected" | "unmapped";
  notes: string;
  has_mapping: boolean;
}

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  standard_concept: string | null;
}

interface MappingForm {
  source_vocabulary_id: string;
  source_code: string;
  target_concept_id: string;
  target_concept_name: string;
  status: "proposed" | "approved" | "rejected";
  notes: string;
}

const emptyForm: MappingForm = {
  source_vocabulary_id: "",
  source_code: "",
  target_concept_id: "",
  target_concept_name: "",
  status: "proposed",
  notes: "",
};

const statusClass: Record<CodeMappingRow["status"], string> = {
  proposed: "bg-amber-100 text-amber-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
  unmapped: "bg-amber-100 text-amber-800",
};

const statusRank: Record<CodeMappingRow["status"], number> = {
  unmapped: 0,
  proposed: 1,
  approved: 2,
  rejected: 3,
};

function buildEditForm(row: CodeMappingRow): MappingForm {
  return {
    source_vocabulary_id: row.source_vocabulary_id || row.concept_vocabulary_id,
    source_code: row.source_code,
    target_concept_id: String(row.concept_id),
    target_concept_name: row.concept_name,
    status: row.status === "unmapped" ? "proposed" : row.status,
    notes: row.notes || "",
  };
}

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [activeVocabulary, setActiveVocabulary] = useState("");
  const [dialogMode, setDialogMode] = useState<"new" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [form, setForm] = useState<MappingForm>(emptyForm);
  const [conceptSearchQuery, setConceptSearchQuery] = useState("");
  const [conceptResults, setConceptResults] = useState<ConceptResult[]>([]);
  const [searchingConcepts, setSearchingConcepts] = useState(false);

  const fetchRows = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const rowResp = await api.get<CodeMappingRow[]>("/v1/code-mappings/");
      setRows(rowResp.data);
    } catch {
      setError("Failed to load code mappings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await fetchRows();
    })();
  }, [fetchRows]);

  const vocabularyStats = useMemo(() => {
    const counts: Record<string, { total: number; unmapped: number }> = {};
    rows.forEach((row) => {
      const key = row.source_vocabulary_id || row.concept_vocabulary_id;
      if (!key) return;
      if (!counts[key]) counts[key] = { total: 0, unmapped: 0 };
      counts[key].total += 1;
      if (!row.has_mapping) counts[key].unmapped += 1;
    });
    return counts;
  }, [rows]);

  const vocabularyOptions = useMemo(() => {
    const values = new Set<string>();
    rows.forEach((row) => {
      if (row.source_vocabulary_id) values.add(row.source_vocabulary_id);
    });
    return [...values].sort((left, right) => {
      const leftCount = vocabularyStats[left]?.total ?? 0;
      const rightCount = vocabularyStats[right]?.total ?? 0;
      if (leftCount !== rightCount) return rightCount - leftCount;
      return left.localeCompare(right);
    });
  }, [rows, vocabularyStats]);

  const selectedVocabulary = activeVocabulary || vocabularyOptions[0] || "";
  const selectedVocabularyStats = vocabularyStats[selectedVocabulary] || { total: 0, unmapped: 0 };

  const filteredRows = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((row) => {
      if (
        selectedVocabulary
        && row.source_vocabulary_id !== selectedVocabulary
      ) {
        return false;
      }
      if (!q) return true;
      return [
        row.source_code,
        row.source_vocabulary_id,
        row.concept_name,
        row.concept_code,
        row.concept_vocabulary_id,
        String(row.concept_id),
      ].some((value) => value.toLowerCase().includes(q));
    }).sort((left, right) => {
      const statusDelta = statusRank[left.status] - statusRank[right.status];
      if (statusDelta !== 0) return statusDelta;
      return (left.source_code || left.concept_code || left.concept_name)
        .localeCompare(right.source_code || right.concept_code || right.concept_name);
    });
  }, [rows, searchQuery, selectedVocabulary]);

  const stats = useMemo(() => ({
    total: rows.length,
    proposed: rows.filter((row) => row.status === "proposed").length,
    approved: rows.filter((row) => row.status === "approved").length,
  }), [rows]);

  const openNewDialog = () => {
    setSelectedRow(null);
    setForm(emptyForm);
    setConceptSearchQuery("");
    setConceptResults([]);
    setDialogMode("new");
  };

  const openEditDialog = (row: CodeMappingRow) => {
    setSelectedRow(row);
    setForm(buildEditForm(row));
    setConceptSearchQuery("");
    setConceptResults([]);
    setDialogMode("edit");
  };

  const closeDialog = () => {
    setDialogMode(null);
    setSelectedRow(null);
    setSaving(false);
  };

  const submitForm = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const payload = {
        ...form,
        status: form.status,
        target_concept_id: Number(form.target_concept_id),
        mapping_id: selectedRow?.mapping_id,
      };
      if (dialogMode === "edit" && selectedRow) {
        await api.patch(`/v1/code-mappings/${selectedRow.concept_id}/`, payload);
      } else {
        await api.post("/v1/code-mappings/", payload);
      }
      closeDialog();
      await fetchRows();
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string } | Record<string, string[]> } }).response?.data
          : undefined;
      const message =
        detail && typeof detail === "object" && !Array.isArray(detail)
          ? Object.entries(detail).map(([field, value]) => `${field}: ${Array.isArray(value) ? value.join(", ") : value}`).join(" ")
          : "";
      setError(message || "Failed to save code mapping.");
    } finally {
      setSaving(false);
    }
  };

  const setField = (field: keyof MappingForm, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  const searchConcepts = async (query: string) => {
    const q = query.trim();
    setConceptSearchQuery(q);
    if (q.length < 3) {
      setConceptResults([]);
      return;
    }
    setSearchingConcepts(true);
    try {
      const resp = await api.get("/v1/concepts/search/", { params: { q, limit: "25" } });
      setConceptResults(resp.data.results || resp.data || []);
    } catch {
      setConceptResults([]);
    } finally {
      setSearchingConcepts(false);
    }
  };

  const suggestCurrentCode = () => {
    const query = form.target_concept_name || form.source_code || selectedRow?.concept_name || "";
    void searchConcepts(query.replace(/[-_]/g, " "));
  };

  const applyConcept = (concept: ConceptResult) => {
    setForm((prev) => ({
      ...prev,
      target_concept_id: String(concept.concept_id),
      target_concept_name: concept.concept_name,
    }));
  };

  const toggleApproval = async (row: CodeMappingRow) => {
    if (!row.has_mapping) {
      openEditDialog(row);
      return;
    }
    const nextStatus = row.status === "approved" ? "proposed" : "approved";
    setError("");
    try {
      await api.patch(`/v1/code-mappings/${row.concept_id}/`, {
        mapping_id: row.mapping_id,
        source_vocabulary_id: row.source_vocabulary_id,
        source_code: row.source_code,
        status: nextStatus,
        notes: row.notes,
      });
      await fetchRows();
    } catch {
      setError("Failed to update code mapping status.");
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-[400px] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50 p-6">
      <div className="mx-auto max-w-7xl">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate("/")}
              className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-100"
              aria-label="Back"
            >
              <ArrowLeft size={16} />
            </button>
            <div>
              <h1 className="text-2xl font-semibold text-slate-950">Code Mapping</h1>
              <p className="text-sm text-slate-600">
                {stats.total} source-code mappings, {stats.approved} approved, {stats.proposed} proposed
              </p>
            </div>
          </div>
          <button
            onClick={openNewDialog}
            className="inline-flex items-center gap-2 rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            <Plus size={16} />
            New Mapping
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="mb-4">
          <label className="relative block">
            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search source codes, destination concepts, source systems, or OMOP IDs"
              className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
            />
          </label>
        </div>

        <div className="mb-4 flex gap-2 overflow-x-auto border-b border-slate-200">
          {vocabularyOptions.map((vocabulary) => {
            const selected = vocabulary === selectedVocabulary;
            const counts = vocabularyStats[vocabulary] || { total: 0, unmapped: 0 };
            return (
              <button
                key={vocabulary}
                type="button"
                onClick={() => setActiveVocabulary(vocabulary)}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
                  selected
                    ? "border-slate-950 text-slate-950"
                    : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-950"
                }`}
              >
                {vocabulary}
                <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                  {counts.unmapped ? `${counts.total}/${counts.unmapped}` : counts.total}
                </span>
              </button>
            );
          })}
        </div>

        <div className="mb-4 flex flex-wrap items-center gap-3 text-sm text-slate-600">
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700">
            {selectedVocabularyStats.total} mappings in this source system
          </span>
        </div>

        <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
          <table className="w-full border-collapse text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-4 py-3 font-semibold">Source code</th>
                <th className="px-4 py-3 font-semibold">Destination OMOP Concept ID</th>
                <th className="px-4 py-3 font-semibold">Concept</th>
                <th className="px-4 py-3 font-semibold">Source code system</th>
                <th className="px-4 py-3 font-semibold">Status</th>
                <th className="w-16 px-4 py-3 font-semibold" aria-label="Actions" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredRows.map((row) => (
                <tr key={`${row.concept_id}-${row.mapping_id ?? "unmapped"}`} className="hover:bg-slate-50">
                  <td className="px-4 py-3 font-mono text-xs text-slate-900">
                    {row.source_code}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.concept_id}</td>
                  <td className="px-4 py-3">
                    <div className="font-medium text-slate-950">{row.concept_name}</div>
                    <div className="font-mono text-xs text-slate-500">{row.concept_vocabulary_id}:{row.concept_code}</div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-700">
                    {row.source_vocabulary_id || row.concept_vocabulary_id}
                  </td>
                  <td className="px-4 py-3">
                    <div className="inline-flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => toggleApproval(row)}
                        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                          row.status === "approved"
                            ? "border-green-500 bg-green-500 text-white"
                            : "border-slate-300 hover:border-slate-600"
                        }`}
                        title={row.status === "approved" ? "Mark mapping as proposed" : "Approve mapping"}
                      >
                        {row.status === "approved" && <Check size={10} />}
                      </button>
                      <span className={`inline-flex rounded px-2 py-1 text-xs font-medium ${statusClass[row.status]}`}>
                        {row.status}
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => openEditDialog(row)}
                      className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-300 text-slate-700 hover:bg-slate-100"
                      aria-label={`Edit ${row.concept_name}`}
                    >
                      <Pencil size={14} />
                    </button>
                  </td>
                </tr>
              ))}
              {filteredRows.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-sm text-slate-500">
                    No code mappings match the current tab and search.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {dialogMode && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
          <form onSubmit={submitForm} className="w-full max-w-3xl rounded-md bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
              <h2 className="text-lg font-semibold text-slate-950">
                {dialogMode === "new" ? "New Mapping" : "Edit Mapping"}
              </h2>
              <button
                type="button"
                onClick={closeDialog}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>

            <div className="grid max-h-[70vh] gap-4 overflow-y-auto px-5 py-5 md:grid-cols-2">
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Source code system
                <input
                  list="code-mapping-source-vocabularies"
                  value={form.source_vocabulary_id}
                  onChange={(e) => setField("source_vocabulary_id", e.target.value)}
                  required
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Source concept code
                <input
                  value={form.source_code}
                  onChange={(e) => setField("source_code", e.target.value)}
                  required
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Status
                <select
                  value={form.status}
                  onChange={(e) => setField("status", e.target.value)}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                >
                  <option value="proposed">Proposed</option>
                  <option value="approved">Approved</option>
                  <option value="rejected">Rejected</option>
                </select>
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Destination OMOP Concept ID
                <input
                  type="number"
                  value={form.target_concept_id}
                  onChange={(e) => setField("target_concept_id", e.target.value)}
                  required
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <div className="self-end rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700">
                {form.target_concept_name || "Choose a destination concept below"}
              </div>
              <div className="md:col-span-2">
                <div className="mb-2 flex items-center justify-between">
                  <label className="text-sm font-medium text-slate-700" htmlFor="code-mapping-concept-search">
                    Concept search
                  </label>
                  <button
                    type="button"
                    onClick={suggestCurrentCode}
                    className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                  >
                    <Sparkles size={13} />
                    Suggest
                  </button>
                </div>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={15} />
                  <input
                    id="code-mapping-concept-search"
                    value={conceptSearchQuery}
                    onChange={(e) => void searchConcepts(e.target.value)}
                    placeholder="Search destination concepts..."
                    className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
                  />
                </div>
                <div className="mt-2 max-h-40 overflow-y-auto rounded-md border border-slate-200">
                  {searchingConcepts && <div className="px-3 py-2 text-sm text-slate-500">Searching...</div>}
                  {!searchingConcepts && conceptResults.length === 0 && conceptSearchQuery.length >= 3 && (
                    <div className="px-3 py-2 text-sm text-slate-500">No suggestions found.</div>
                  )}
                  {!searchingConcepts && conceptResults.map((concept) => (
                    <button
                      key={concept.concept_id}
                      type="button"
                      onClick={() => applyConcept(concept)}
                      className="grid w-full grid-cols-[8rem_1fr_6rem] gap-2 border-b border-slate-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-slate-50"
                    >
                      <span className="font-mono text-slate-700">{concept.concept_code}</span>
                      <span className="text-slate-900">{concept.concept_name}</span>
                      <span className="font-mono text-slate-500">{concept.vocabulary_id}</span>
                    </button>
                  ))}
                </div>
              </div>
              <label className="grid gap-1 text-sm font-medium text-slate-700 md:col-span-2">
                Notes
                <textarea
                  value={form.notes}
                  onChange={(e) => setField("notes", e.target.value)}
                  rows={3}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm font-normal text-slate-950"
                />
              </label>
            </div>

            <datalist id="code-mapping-source-vocabularies">
              {vocabularyOptions.map((source) => <option key={source} value={source} />)}
            </datalist>

            <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-4">
              <button
                type="button"
                onClick={closeDialog}
                className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              >
                {saving ? "Saving" : dialogMode === "edit" ? "Update Mapping" : "Save Mapping"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
