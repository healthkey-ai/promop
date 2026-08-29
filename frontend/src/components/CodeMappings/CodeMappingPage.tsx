import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Pencil, Plus, Search, X } from "lucide-react";
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
  status: "active" | "retired" | "rejected" | "unmapped";
  notes: string;
  has_mapping: boolean;
}

interface ReferenceData {
  vocabularies: Array<{ vocabulary_id: string; vocabulary_name: string }>;
  domains: Array<{ domain_id: string; domain_name: string }>;
  concept_classes: Array<{ concept_class_id: string; concept_class_name: string }>;
}

interface MappingForm {
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  target_concept_id: string;
  target_concept_name: string;
  target_concept_code: string;
  target_vocabulary_id: string;
  target_vocabulary_name: string;
  domain_id: string;
  concept_class_id: string;
  status: "active" | "retired" | "rejected";
  notes: string;
}

const emptyForm: MappingForm = {
  source_vocabulary_id: "",
  source_code: "",
  source_code_description: "",
  target_concept_id: "",
  target_concept_name: "",
  target_concept_code: "",
  target_vocabulary_id: "HK-Observation",
  target_vocabulary_name: "",
  domain_id: "Observation",
  concept_class_id: "Clinical Observation",
  status: "active",
  notes: "",
};

const statusClass: Record<CodeMappingRow["status"], string> = {
  active: "bg-green-100 text-green-800",
  retired: "bg-slate-100 text-slate-700",
  rejected: "bg-red-100 text-red-800",
  unmapped: "bg-amber-100 text-amber-800",
};

function buildEditForm(row: CodeMappingRow): MappingForm {
  return {
    source_vocabulary_id: row.source_vocabulary_id || row.concept_vocabulary_id,
    source_code: row.source_code,
    source_code_description: row.source_code_description,
    target_concept_id: String(row.concept_id),
    target_concept_name: row.concept_name,
    target_concept_code: row.concept_code,
    target_vocabulary_id: row.concept_vocabulary_id,
    target_vocabulary_name: "",
    domain_id: row.domain_id || "Observation",
    concept_class_id: row.concept_class_id || "Clinical Observation",
    status: row.status === "unmapped" ? "active" : row.status,
    notes: row.notes || "",
  };
}

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [references, setReferences] = useState<ReferenceData>({
    vocabularies: [],
    domains: [],
    concept_classes: [],
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [dialogMode, setDialogMode] = useState<"new" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [form, setForm] = useState<MappingForm>(emptyForm);

  const fetchRows = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rowResp, referenceResp] = await Promise.all([
        api.get<CodeMappingRow[]>("/v1/code-mappings/"),
        api.get<ReferenceData>("/v1/code-mappings/vocabularies/"),
      ]);
      setRows(rowResp.data);
      setReferences(referenceResp.data);
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

  const sourceOptions = useMemo(() => {
    const values = new Set<string>();
    rows.forEach((row) => {
      if (row.source_vocabulary_id) values.add(row.source_vocabulary_id);
      if (row.concept_vocabulary_id) values.add(row.concept_vocabulary_id);
    });
    references.vocabularies.forEach((vocab) => values.add(vocab.vocabulary_id));
    return [...values].sort();
  }, [references.vocabularies, rows]);

  const filteredRows = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((row) => {
      if (sourceFilter && row.source_vocabulary_id !== sourceFilter && row.concept_vocabulary_id !== sourceFilter) {
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
    });
  }, [rows, searchQuery, sourceFilter]);

  const stats = useMemo(() => ({
    total: rows.length,
    active: rows.filter((row) => row.status === "active").length,
    unmapped: rows.filter((row) => !row.has_mapping).length,
  }), [rows]);

  const openNewDialog = () => {
    setSelectedRow(null);
    setForm(emptyForm);
    setDialogMode("new");
  };

  const openEditDialog = (row: CodeMappingRow) => {
    setSelectedRow(row);
    setForm(buildEditForm(row));
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
                {stats.total} quarantined concepts, {stats.active} active source codes, {stats.unmapped} unmapped
              </p>
            </div>
          </div>
          <button
            onClick={openNewDialog}
            className="inline-flex items-center gap-2 rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
          >
            <Plus size={16} />
            New Code
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="mb-4 grid gap-3 md:grid-cols-[1fr_240px]">
          <label className="relative block">
            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search codes, concepts, vocabularies, or concept ids"
              className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
            />
          </label>
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-950 outline-none focus:border-slate-700"
            aria-label="Source filter"
          >
            <option value="">All sources</option>
            {sourceOptions.map((source) => (
              <option key={source} value={source}>{source}</option>
            ))}
          </select>
        </div>

        <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
          <table className="w-full border-collapse text-left text-sm">
            <thead className="bg-slate-100 text-xs uppercase text-slate-600">
              <tr>
                <th className="px-4 py-3 font-semibold">Code</th>
                <th className="px-4 py-3 font-semibold">Destination OMOP Concept ID</th>
                <th className="px-4 py-3 font-semibold">Concept</th>
                <th className="px-4 py-3 font-semibold">Source</th>
                <th className="px-4 py-3 font-semibold">Status</th>
                <th className="w-16 px-4 py-3 font-semibold" aria-label="Actions" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredRows.map((row) => (
                <tr key={`${row.concept_id}-${row.mapping_id ?? "unmapped"}`} className="hover:bg-slate-50">
                  <td className="px-4 py-3 font-mono text-xs text-slate-900">
                    {row.source_code || <span className="font-sans text-slate-500">Unmapped</span>}
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
                    <span className={`inline-flex rounded px-2 py-1 text-xs font-medium ${statusClass[row.status]}`}>
                      {row.status}
                    </span>
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
                    No code mappings match the current filters.
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
                {dialogMode === "new" ? "New Code" : "Edit Code"}
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
                Source
                <input
                  list="code-mapping-source-vocabularies"
                  value={form.source_vocabulary_id}
                  onChange={(e) => setField("source_vocabulary_id", e.target.value)}
                  required
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Code
                <input
                  value={form.source_code}
                  onChange={(e) => setField("source_code", e.target.value)}
                  required
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700 md:col-span-2">
                Code description
                <input
                  value={form.source_code_description}
                  onChange={(e) => setField("source_code_description", e.target.value)}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Destination OMOP Concept ID
                <input
                  type="number"
                  min={2000000000}
                  value={form.target_concept_id}
                  onChange={(e) => setField("target_concept_id", e.target.value)}
                  required
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Target vocabulary
                <input
                  list="code-mapping-target-vocabularies"
                  value={form.target_vocabulary_id}
                  onChange={(e) => setField("target_vocabulary_id", e.target.value)}
                  required
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700 md:col-span-2">
                Destination concept name
                <input
                  value={form.target_concept_name}
                  onChange={(e) => setField("target_concept_name", e.target.value)}
                  required
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Target concept code
                <input
                  value={form.target_concept_code}
                  onChange={(e) => setField("target_concept_code", e.target.value)}
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Status
                <select
                  value={form.status}
                  onChange={(e) => setField("status", e.target.value)}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                >
                  <option value="active">Active</option>
                  <option value="retired">Retired</option>
                  <option value="rejected">Rejected</option>
                </select>
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Domain
                <input
                  list="code-mapping-domains"
                  value={form.domain_id}
                  onChange={(e) => setField("domain_id", e.target.value)}
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
              <label className="grid gap-1 text-sm font-medium text-slate-700">
                Concept class
                <input
                  list="code-mapping-classes"
                  value={form.concept_class_id}
                  onChange={(e) => setField("concept_class_id", e.target.value)}
                  readOnly={dialogMode === "edit"}
                  className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950 read-only:bg-slate-100"
                />
              </label>
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
              {sourceOptions.map((source) => <option key={source} value={source} />)}
            </datalist>
            <datalist id="code-mapping-target-vocabularies">
              {references.vocabularies.map((vocab) => <option key={vocab.vocabulary_id} value={vocab.vocabulary_id} />)}
            </datalist>
            <datalist id="code-mapping-domains">
              {references.domains.map((domain) => <option key={domain.domain_id} value={domain.domain_id} />)}
            </datalist>
            <datalist id="code-mapping-classes">
              {references.concept_classes.map((conceptClass) => (
                <option key={conceptClass.concept_class_id} value={conceptClass.concept_class_id} />
              ))}
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
                {saving ? "Saving" : "Save"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
