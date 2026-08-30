import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Pencil, Plus, Search, Sparkles, Trash2, X } from "lucide-react";
import api from "@/api/axios";

/**
 * Code Mapping: incoming source codes -> destination OMOP concepts.
 *
 * The direction never reverses. A source code is something that arrived - a
 * LOINC or ICD code from a FHIR bundle, a lab's in-house test name off a PDF, a
 * phrase from a note. The destination is the OMOP concept it means, either an
 * existing Athena concept or one minted locally under an HK-* vocabulary.
 *
 * Tabs are destination vocabularies, and each splits into Unmapped (proposed,
 * awaiting an SME) and Mapped (approved). Curation is mostly re-pointing a
 * proposed mapping off the concept an import minted and onto a standard one -
 * which is why SNOMED and LOINC have tabs too: that re-pointing is what puts
 * mappings there.
 */

interface CodeMappingRow {
  mapping_id: number | null;
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  destination_concept_id: number;
  destination_concept_name: string;
  destination_concept_code: string;
  destination_vocabulary_id: string;
  destination_concept_class_id: string;
  destination_omop_table: string;
  destination_domain_id: string;
  status: "proposed" | "approved" | "rejected" | "unmapped";
  notes: string;
  origin: string;
  origin_system: string;
  occurrence_count: number;
  has_mapping: boolean;
}

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
}

interface VocabularyRef {
  vocabulary_id: string;
  vocabulary_name: string;
  is_local?: boolean;
}

interface Reference {
  source_code_systems: VocabularyRef[];
  destination_vocabularies: VocabularyRef[];
  omop_tables: { value: string; label: string }[];
}

interface RepointResult {
  rows_updated: number;
  persons_marked_stale: number;
  rows_collapsed: number;
}

interface MappingForm {
  source_code: string;
  source_vocabulary_id: string;
  source_code_description: string;
  destination_vocabulary_id: string;
  destination_concept_name: string;
  destination_concept_id: string;
  destination_concept_class_id: string;
  omop_table: string;
  status: "proposed" | "approved" | "rejected";
  notes: string;
}

const emptyForm: MappingForm = {
  source_code: "",
  source_vocabulary_id: "",
  source_code_description: "",
  destination_vocabulary_id: "",
  destination_concept_name: "",
  destination_concept_id: "",
  destination_concept_class_id: "",
  omop_table: "",
  status: "proposed",
  notes: "",
};

const emptyReference: Reference = {
  source_code_systems: [],
  destination_vocabularies: [],
  omop_tables: [],
};

const statusClass: Record<string, string> = {
  proposed: "bg-amber-100 text-amber-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
  unmapped: "bg-amber-100 text-amber-800",
};

/** OMOP domain -> the clinical table its facts land in. */
const DOMAIN_TO_TABLE: Record<string, string> = {
  Measurement: "measurement",
  Observation: "observation",
  Condition: "condition",
  Drug: "drug_exposure",
  Procedure: "procedure",
};

function buildEditForm(row: CodeMappingRow): MappingForm {
  return {
    source_code: row.source_code,
    // No fallback to the destination vocabulary. A mapping with no source code
    // system genuinely has none, and showing the destination's there is what
    // put HK-Wearable in the source column to begin with.
    source_vocabulary_id: row.source_vocabulary_id,
    source_code_description: row.source_code_description || "",
    destination_vocabulary_id: row.destination_vocabulary_id,
    destination_concept_name: row.destination_concept_name,
    destination_concept_id: String(row.destination_concept_id),
    destination_concept_class_id: row.destination_concept_class_id || "",
    omop_table: row.destination_omop_table || DOMAIN_TO_TABLE[row.destination_domain_id] || "",
    status: row.status === "unmapped" ? "proposed" : row.status,
    notes: row.notes || "",
  };
}

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [activeVocabulary, setActiveVocabulary] = useState("");
  const [mappedCollapsed, setMappedCollapsed] = useState(true);
  const [showRejected, setShowRejected] = useState(false);
  const [dialogMode, setDialogMode] = useState<"new" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [form, setForm] = useState<MappingForm>(emptyForm);
  const [conceptSearchQuery, setConceptSearchQuery] = useState("");
  const [conceptResults, setConceptResults] = useState<ConceptResult[]>([]);
  const [searchingConcepts, setSearchingConcepts] = useState(false);
  const [repointing, setRepointing] = useState<{ from: string; to: string } | null>(null);
  const [repointResult, setRepointResult] = useState<RepointResult | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rowResp, refResp] = await Promise.all([
        api.get<CodeMappingRow[]>("/v1/code-mappings/"),
        api.get<Reference>("/v1/code-mappings/reference/"),
      ]);
      setRows(rowResp.data);
      setReference(refResp.data || emptyReference);
    } catch {
      setError("Failed to load code mappings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Wrapped: react-hooks/set-state-in-effect traces into the callback and
    // errors on a direct call, and a red lint job turns every open PR red.
    (async () => {
      await fetchAll();
    })();
  }, [fetchAll]);

  const vocabularyTabs = useMemo(() => {
    const counts: Record<string, { proposed: number; approved: number }> = {};
    reference.destination_vocabularies.forEach((v) => {
      counts[v.vocabulary_id] = { proposed: 0, approved: 0 };
    });
    rows.forEach((row) => {
      const key = row.destination_vocabulary_id;
      if (!key) return;
      if (!counts[key]) counts[key] = { proposed: 0, approved: 0 };
      if (row.status === "approved") counts[key].approved += 1;
      else if (row.status === "proposed") counts[key].proposed += 1;
    });
    return reference.destination_vocabularies
      .map((v) => ({ ...v, ...counts[v.vocabulary_id] }))
      .concat(
        Object.keys(counts)
          .filter((k) => !reference.destination_vocabularies.some((v) => v.vocabulary_id === k))
          .map((k) => ({ vocabulary_id: k, vocabulary_name: k, is_local: true, ...counts[k] })),
      );
  }, [rows, reference]);

  // Land on work, not on the alphabetically-first tab. A curator opens this
  // page to review proposals; defaulting to an empty SNOMED tab would make the
  // queue look empty when it is not.
  const defaultVocabulary = useMemo(() => {
    const withWork = vocabularyTabs.find((t) => t.proposed > 0);
    if (withWork) return withWork.vocabulary_id;
    const withAny = vocabularyTabs.find((t) => t.proposed + t.approved > 0);
    return withAny?.vocabulary_id ?? vocabularyTabs[0]?.vocabulary_id ?? "";
  }, [vocabularyTabs]);

  const selectedVocabulary = activeVocabulary || defaultVocabulary;

  const visibleRows = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((row) => {
      // Rejected rows are hidden but reachable. Filtering them out with no way
      // back would strand the source code for good: it appears in neither
      // section, cannot be re-opened to un-reject, and re-creating it trips the
      // (source_vocabulary_id, source_code) unique constraint.
      if (row.status === "rejected" && !showRejected) return false;
      if (selectedVocabulary && row.destination_vocabulary_id !== selectedVocabulary) return false;
      if (!q) return true;
      return [
        row.source_code,
        row.source_vocabulary_id,
        row.source_code_description,
        row.destination_concept_name,
        row.destination_concept_code,
        String(row.destination_concept_id),
      ].some((value) => (value || "").toLowerCase().includes(q));
    });
  }, [rows, searchQuery, selectedVocabulary, showRejected]);

  /** Highest occurrence count first: the code seen 400 times is worth more of a curator's time. */
  const byOccurrence = (a: CodeMappingRow, b: CodeMappingRow) =>
    (b.occurrence_count || 0) - (a.occurrence_count || 0)
    || (a.source_code || "").localeCompare(b.source_code || "");

  const unmappedRows = useMemo(
    () => visibleRows.filter((r) => r.status !== "approved").sort(byOccurrence),
    [visibleRows],
  );
  const rejectedCount = useMemo(
    () => rows.filter((r) => r.status === "rejected"
      && (!selectedVocabulary || r.destination_vocabulary_id === selectedVocabulary)).length,
    [rows, selectedVocabulary],
  );
  const mappedRows = useMemo(
    () => visibleRows.filter((r) => r.status === "approved").sort(byOccurrence),
    [visibleRows],
  );

  const openNewDialog = () => {
    setSelectedRow(null);
    setForm({ ...emptyForm, destination_vocabulary_id: selectedVocabulary });
    setConceptSearchQuery("");
    setConceptResults([]);
    setRepointResult(null);
    setDialogMode("new");
  };

  const openEditDialog = (row: CodeMappingRow) => {
    setSelectedRow(row);
    setForm(buildEditForm(row));
    setConceptSearchQuery("");
    setConceptResults([]);
    setRepointResult(null);
    setDialogMode("edit");
  };

  const closeDialog = () => {
    setDialogMode(null);
    setSelectedRow(null);
    setSaving(false);
    setRepointing(null);
    setRepointResult(null);
  };

  const setField = (field: keyof MappingForm, value: string) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  /** Apply a concept to the form: id, name, vocabulary, class, and a default table. */
  const applyConcept = (concept: ConceptResult) => {
    setForm((prev) => ({
      ...prev,
      destination_concept_id: String(concept.concept_id),
      destination_concept_name: concept.concept_name,
      destination_vocabulary_id: concept.vocabulary_id,
      destination_concept_class_id: concept.concept_class_id || "",
      omop_table: prev.omop_table || DOMAIN_TO_TABLE[concept.domain_id] || "",
    }));
  };

  /**
   * Resolve a hand-typed concept id. Concept class is never typed - it follows
   * from the concept - so it has to be fetched whenever the id changes by hand.
   */
  const resolveConceptId = async (rawId: string) => {
    const id = rawId.trim();
    if (!id) return;
    try {
      const resp = await api.get<ConceptResult>(`/v1/concepts/${id}/`);
      applyConcept(resp.data);
      setError("");
    } catch {
      setError(`No OMOP concept with id ${id}.`);
      setField("destination_concept_class_id", "");
    }
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
      const params: Record<string, string> = { q, limit: "25" };
      // Scope to the chosen destination vocabulary so a curator after a LOINC
      // code is not wading through a million SNOMED hits.
      if (form.destination_vocabulary_id) params.vocabulary_id = form.destination_vocabulary_id;
      const resp = await api.get("/v1/concepts/search/", { params });
      setConceptResults(resp.data.results || resp.data || []);
    } catch {
      setConceptResults([]);
    } finally {
      setSearchingConcepts(false);
    }
  };

  const suggestCurrentCode = () => {
    const query = form.source_code_description || form.source_code || "";
    void searchConcepts(query.replace(/[-_]/g, " "));
  };

  const willRepoint =
    dialogMode === "edit"
    && form.status === "approved"
    && selectedRow !== null
    && String(selectedRow.destination_concept_id) !== form.destination_concept_id;

  const submitForm = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    if (willRepoint && selectedRow) {
      setRepointing({
        from: String(selectedRow.destination_concept_id),
        to: form.destination_concept_id,
      });
    }
    try {
      const payload = {
        ...form,
        destination_concept_id: Number(form.destination_concept_id),
        target_concept_id: Number(form.destination_concept_id),
      };
      const resp = selectedRow?.mapping_id
        ? await api.patch(`/v1/code-mappings/${selectedRow.mapping_id}/`, payload)
        : await api.post("/v1/code-mappings/", payload);
      const repoint: RepointResult | null = resp.data?.repoint ?? null;
      await fetchAll();
      // Hold the dialog open on a re-point so the curator sees what moved;
      // a silent close would leave them guessing whether it worked.
      if (repoint && repoint.rows_updated) {
        setRepointResult(repoint);
        setRepointing(null);
        setSaving(false);
        return;
      }
      closeDialog();
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
      setRepointing(null);
      setSaving(false);
    }
  };

  const toggleApproval = async (row: CodeMappingRow) => {
    if (!row.mapping_id) {
      openEditDialog(row);
      return;
    }
    setError("");
    try {
      await api.patch(`/v1/code-mappings/${row.mapping_id}/`, {
        source_vocabulary_id: row.source_vocabulary_id,
        source_code: row.source_code,
        source_code_description: row.source_code_description,
        destination_concept_id: row.destination_concept_id,
        destination_vocabulary_id: row.destination_vocabulary_id,
        omop_table: row.destination_omop_table,
        status: row.status === "approved" ? "proposed" : "approved",
        notes: row.notes,
      });
      await fetchAll();
    } catch {
      setError("Failed to update code mapping status.");
    }
  };

  const deleteMapping = async (row: CodeMappingRow) => {
    if (!row.mapping_id) return;
    setError("");
    try {
      await api.delete(`/v1/code-mappings/${row.mapping_id}/`);
      closeDialog();
      await fetchAll();
    } catch {
      setError("Failed to delete code mapping.");
    }
  };

  const renderTable = (sectionRows: CodeMappingRow[], emptyText: string) => (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <table className="w-full border-collapse text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600">
          <tr>
            <th className="px-4 py-3 font-semibold">Source code</th>
            <th className="px-4 py-3 font-semibold">Source code system</th>
            <th className="px-4 py-3 font-semibold">Destination concept</th>
            <th className="px-4 py-3 font-semibold">Concept ID</th>
            <th className="px-4 py-3 font-semibold">OMOP table</th>
            <th className="px-4 py-3 font-semibold">Seen</th>
            <th className="px-4 py-3 font-semibold">Status</th>
            <th className="w-16 px-4 py-3 font-semibold" aria-label="Actions" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sectionRows.map((row) => (
            <tr
              key={row.mapping_id ?? `c-${row.destination_concept_id}`}
              role="button"
              tabIndex={0}
              onClick={() => openEditDialog(row)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  openEditDialog(row);
                }
              }}
              className="cursor-pointer hover:bg-slate-50"
            >
              <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.source_code}</td>
              <td className="px-4 py-3 font-mono text-xs text-slate-700">
                {row.source_vocabulary_id || <span className="italic text-slate-400">uncoded</span>}
              </td>
              <td className="px-4 py-3">
                <div className="font-medium text-slate-950">{row.destination_concept_name}</div>
                <div className="font-mono text-xs text-slate-500">
                  {row.destination_vocabulary_id}:{row.destination_concept_code}
                </div>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.destination_concept_id}</td>
              <td className="px-4 py-3 text-xs text-slate-700">{row.destination_omop_table}</td>
              <td className="px-4 py-3 text-xs text-slate-700">{row.occurrence_count || "—"}</td>
              <td className="px-4 py-3">
                <div className="inline-flex items-center gap-2">
                  <button
                    type="button"
                    // Stops a one-click approve from also opening the dialog.
                    onClick={(e) => { e.stopPropagation(); void toggleApproval(row); }}
                    className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                      row.status === "approved"
                        ? "border-green-500 bg-green-500 text-white"
                        : "border-slate-300 hover:border-slate-600"
                    }`}
                    title={row.status === "approved" ? "Mark mapping as proposed" : "Approve mapping"}
                    aria-label={row.status === "approved" ? `Unapprove ${row.source_code}` : `Approve ${row.source_code}`}
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
                  type="button"
                  onClick={(e) => { e.stopPropagation(); openEditDialog(row); }}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-300 text-slate-700 hover:bg-slate-100"
                  aria-label={`Edit ${row.source_code}`}
                >
                  <Pencil size={14} />
                </button>
              </td>
            </tr>
          ))}
          {sectionRows.length === 0 && (
            <tr>
              <td colSpan={8} className="px-4 py-8 text-center text-sm text-slate-500">{emptyText}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );

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
                Source codes from FHIR, paper labs and notes, mapped to destination OMOP concepts
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
              placeholder="Search source codes, destination concepts, or OMOP IDs"
              className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
            />
          </label>
        </div>

        <div
          role="tablist"
          aria-label="Destination vocabularies"
          className="mb-4 flex gap-2 overflow-x-auto border-b border-slate-200"
        >
          {vocabularyTabs.map((tab) => {
            const selected = tab.vocabulary_id === selectedVocabulary;
            return (
              <button
                key={tab.vocabulary_id}
                type="button"
                onClick={() => setActiveVocabulary(tab.vocabulary_id)}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
                  selected
                    ? "border-slate-950 text-slate-950"
                    : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-950"
                }`}
              >
                {tab.vocabulary_id}
                {/* The badge counts review work, which is the number a curator is working down. */}
                <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                  {tab.proposed}
                </span>
              </button>
            );
          })}
        </div>

        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-700">
            Unmapped <span className="font-normal text-slate-500">({unmappedRows.length})</span>
          </h2>
          <div className="mb-2 flex items-center justify-between gap-3">
            <p className="text-xs text-slate-500">
              The destination concept exists — an import minted or chose it — but no curator has confirmed it.
            </p>
            {rejectedCount > 0 && (
              <label className="flex shrink-0 items-center gap-1.5 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={showRejected}
                  onChange={(e) => setShowRejected(e.target.checked)}
                />
                Show {rejectedCount} rejected
              </label>
            )}
          </div>
          {renderTable(unmappedRows, "Nothing awaiting review in this vocabulary.")}
        </section>

        <section>
          <button
            type="button"
            onClick={() => setMappedCollapsed((v) => !v)}
            className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
          >
            {mappedCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
            Mapped <span className="font-normal text-slate-500">({mappedRows.length})</span>
          </button>
          {!mappedCollapsed && renderTable(mappedRows, "No approved mappings in this vocabulary.")}
        </section>
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

            <div className="max-h-[70vh] overflow-y-auto px-5 py-5">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Source</h3>
              <div className="mb-5 grid gap-4 md:grid-cols-2">
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Source Code
                  <input
                    id="source_code"
                    value={form.source_code}
                    onChange={(e) => setField("source_code", e.target.value)}
                    required
                    className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                  />
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Source Code System
                  <select
                    id="source_vocabulary_id"
                    value={form.source_vocabulary_id}
                    onChange={(e) => setField("source_vocabulary_id", e.target.value)}
                    className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                  >
                    {/* Blank is a real answer: a paper lab or a note has no code system. */}
                    <option value="">— none (uncoded / free text) —</option>
                    {reference.source_code_systems.map((v) => (
                      <option key={v.vocabulary_id} value={v.vocabulary_id}>{v.vocabulary_id}</option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700 md:col-span-2">
                  Source Code Description
                  <input
                    id="source_code_description"
                    value={form.source_code_description}
                    onChange={(e) => setField("source_code_description", e.target.value)}
                    className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                  />
                </label>
              </div>

              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Destination</h3>
              <div className="grid gap-4 md:grid-cols-2">
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Destination Vocabulary
                  <select
                    id="destination_vocabulary_id"
                    value={form.destination_vocabulary_id}
                    onChange={(e) => setField("destination_vocabulary_id", e.target.value)}
                    className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                  >
                    <option value="">— select —</option>
                    {reference.destination_vocabularies.map((v) => (
                      <option key={v.vocabulary_id} value={v.vocabulary_id}>{v.vocabulary_id}</option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Destination Concept Name
                  <input
                    id="destination_concept_name"
                    value={form.destination_concept_name}
                    onChange={(e) => setField("destination_concept_name", e.target.value)}
                    required
                    className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                  />
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Destination Concept ID
                  <input
                    id="destination_concept_id"
                    type="number"
                    value={form.destination_concept_id}
                    onChange={(e) => setField("destination_concept_id", e.target.value)}
                    onBlur={(e) => void resolveConceptId(e.target.value)}
                    required
                    className="h-10 rounded-md border border-slate-300 px-3 font-mono text-sm font-normal text-slate-950"
                  />
                </label>
                <label className="grid gap-1 text-sm font-medium text-slate-700">
                  Destination OMOP Table
                  <select
                    id="omop_table"
                    value={form.omop_table}
                    onChange={(e) => setField("omop_table", e.target.value)}
                    required
                    className="h-10 rounded-md border border-slate-300 px-3 text-sm font-normal text-slate-950"
                  >
                    <option value="">— select —</option>
                    {reference.omop_tables.map((t) => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </label>
                <div className="grid gap-1 text-sm font-medium text-slate-700">
                  Destination Concept Class
                  {/* Read-only: the class follows from the concept, it is not a choice. */}
                  <div
                    data-testid="destination-concept-class"
                    className="flex h-10 items-center rounded-md bg-slate-100 px-3 text-sm font-normal text-slate-700"
                  >
                    {form.destination_concept_class_id || "—"}
                  </div>
                </div>
              </div>

              <div className="mt-4">
                <div className="mb-2 flex items-center justify-between">
                  <label className="text-sm font-medium text-slate-700" htmlFor="code-mapping-concept-search">
                    Search destination concepts
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
                    placeholder={
                      form.destination_vocabulary_id
                        ? `Search ${form.destination_vocabulary_id} concepts...`
                        : "Search destination concepts..."
                    }
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

              {selectedRow?.origin === "import" && (
                <p className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
                  Proposed by import{selectedRow.origin_system ? ` (${selectedRow.origin_system})` : ""}
                  {selectedRow.occurrence_count ? ` · seen ${selectedRow.occurrence_count} time(s)` : ""}
                </p>
              )}

              <label className="mt-4 grid gap-1 text-sm font-medium text-slate-700">
                Notes
                <textarea
                  value={form.notes}
                  onChange={(e) => setField("notes", e.target.value)}
                  rows={2}
                  className="rounded-md border border-slate-300 px-3 py-2 text-sm font-normal text-slate-950"
                />
              </label>

              {/* Re-pointing rewrites every stored row carrying this code, which
                  can run for a while. Without this the dialog looks frozen and a
                  curator clicks again. */}
              {repointing && (
                <div
                  role="status"
                  className="mt-4 rounded-md border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900"
                >
                  <div className="flex items-center gap-2 font-medium">
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
                    Updating concept {repointing.from} → {repointing.to}
                  </div>
                  <p className="mt-1 text-xs text-blue-800">Rewriting clinical rows already stored…</p>
                </div>
              )}
              {repointResult && (
                <div
                  role="status"
                  className="mt-4 rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-900"
                >
                  Updated {repointResult.rows_updated} row(s) across{" "}
                  {repointResult.persons_marked_stale} patient(s).
                  {repointResult.rows_collapsed > 0 && ` ${repointResult.rows_collapsed} duplicate(s) collapsed.`}{" "}
                  Patient records queued for re-derivation.
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-2 border-t border-slate-200 px-5 py-4">
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
                  Status
                  <select
                    id="status"
                    value={form.status}
                    onChange={(e) => setField("status", e.target.value)}
                    className="h-9 rounded-md border border-slate-300 px-2 text-sm font-normal text-slate-950"
                  >
                    <option value="proposed">Proposed</option>
                    <option value="approved">Approved</option>
                    <option value="rejected">Rejected</option>
                  </select>
                </label>
                {selectedRow?.mapping_id && (
                  <button
                    type="button"
                    onClick={() => void deleteMapping(selectedRow)}
                    className="inline-flex items-center gap-1.5 rounded-md border border-red-200 px-2.5 py-1.5 text-xs font-medium text-red-700 hover:bg-red-50"
                  >
                    <Trash2 size={13} />
                    Delete
                  </button>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={closeDialog}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100"
                >
                  {repointResult ? "Close" : "Cancel"}
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
                >
                  {saving
                    ? "Saving"
                    : willRepoint
                      ? "Update & Approve"
                      : dialogMode === "edit"
                        ? "Update Mapping"
                        : "Save Mapping"}
                </button>
              </div>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
