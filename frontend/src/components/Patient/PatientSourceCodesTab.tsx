import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import api from "../../api/axios";
import type {
  PatientSourceCode,
  SourceCodesResponse,
  ResolveResult,
} from "../../types/sourceCodes";
import InlineDestinationPicker from "../CodeMappings/InlineDestinationPicker";
import {
  searchDestinationConcepts,
  destinationError,
  type DestinationConcept,
  type SavedMapping,
} from "../CodeMappings/destinationSearch";
import ConceptInputDetails from "../UI/ConceptInputDetails";

type Props = { personId: string };

const STATUS_BADGE: Record<string, { bg: string; text: string; label: string }> = {
  approved: { bg: "bg-green-100", text: "text-green-800", label: "Approved" },
  proposed: { bg: "bg-yellow-100", text: "text-yellow-800", label: "Proposed" },
  unmapped: { bg: "bg-red-100", text: "text-red-800", label: "Unmapped" },
};

const TABLE_LABELS: Record<string, string> = {
  measurement: "Measurement",
  observation: "Observation",
  condition: "Condition",
  drug_exposure: "Drug Exposure",
  procedure: "Procedure",
};

const TABLE_TO_DOMAIN: Record<string, string> = {
  measurement: "Measurement",
  observation: "Observation",
  condition: "Condition",
  drug_exposure: "Drug",
  procedure: "Procedure",
};

type VocabularyItem = { vocabulary_id: string; vocabulary_name: string };

type ReferenceData = {
  destination_vocabularies: VocabularyItem[];
};

export default function PatientSourceCodesTab({ personId }: Props) {
  const [data, setData] = useState<SourceCodesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [resolveResult, setResolveResult] = useState<ResolveResult | null>(null);
  const [filter, setFilter] = useState<"all" | "unmapped">("all");
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [reference, setReference] = useState<ReferenceData | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const fetchSourceCodes = useCallback(async () => {
    try {
      setLoading(true);
      const res = await api.get<SourceCodesResponse>(
        `/v1/patient-records/${personId}/source-codes/`
      );
      setData(res.data);
      setError(null);
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string; error?: string } } })
              .response?.data
          : undefined;
      setError(detail?.detail || detail?.error || "Failed to load source codes.");
    } finally {
      setLoading(false);
    }
  }, [personId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await fetchSourceCodes();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchSourceCodes]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get<ReferenceData>("/v1/code-mappings/reference/");
        if (!cancelled) setReference(res.data);
      } catch {
        // Non-fatal: picker works without vocabulary filter
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleResolve = async () => {
    setResolving(true);
    setResolveResult(null);
    try {
      const res = await api.post<ResolveResult>(
        `/v1/patient-records/${personId}/resolve-source-codes/`,
        {}
      );
      setResolveResult(res.data);
      await fetchSourceCodes();
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string; error?: string } } })
              .response?.data
          : undefined;
      setError(detail?.detail || detail?.error || "Failed to resolve source codes.");
    } finally {
      setResolving(false);
    }
  };

  const handleRowClick = (index: number) => {
    if (editingIndex === index) {
      setEditingIndex(null);
    } else {
      setBanner(null);
      setEditingIndex(index);
    }
  };

  const handleSaved = (_saved: SavedMapping, concept: { concept_name: string }, statusLabel: string) => {
    setEditingIndex(null);
    setBanner(`Saved mapping: ${concept.concept_name} (${statusLabel}).`);
    void fetchSourceCodes();
  };

  const handleCancel = () => {
    setEditingIndex(null);
  };

  if (loading) {
    return (
      <div className="space-y-4 p-6">
        <div className="h-6 w-64 animate-pulse rounded bg-muted" />
        <div className="h-48 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-destructive">
        <p>{error}</p>
      </div>
    );
  }

  if (!data) return null;

  const { source_codes, summary } = data;
  const filtered =
    filter === "unmapped"
      ? source_codes.filter(
          (sc) => sc.mapping_status === "unmapped" || sc.concept_id === 0
        )
      : source_codes;

  return (
    <div className="space-y-4 p-6">
      {/* Summary banner */}
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">{summary.total}</span> source
          codes &mdash;{" "}
          <span className="text-red-600">{summary.unmapped} unmapped</span>,{" "}
          <span className="text-yellow-600">{summary.proposed} proposed</span>,{" "}
          <span className="text-green-600">{summary.approved} approved</span>
        </div>
        <button
          onClick={handleResolve}
          disabled={resolving}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {resolving ? (
            <>
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Resolving...
            </>
          ) : (
            "Generate OMOP"
          )}
        </button>
      </div>

      {/* Resolve result toast */}
      {resolveResult && (
        <div className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800">
          Resolved {resolveResult.resolved} code(s). {resolveResult.skipped} skipped,{" "}
          {resolveResult.already_resolved} already resolved.
        </div>
      )}

      {/* Save banner */}
      {banner && (
        <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-800">
          {banner}
        </div>
      )}

      {/* Filter toggles */}
      <div className="flex gap-2">
        <button
          onClick={() => setFilter("all")}
          className={`rounded-md px-3 py-1 text-sm ${
            filter === "all"
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          }`}
        >
          All ({summary.total})
        </button>
        <button
          onClick={() => setFilter("unmapped")}
          className={`rounded-md px-3 py-1 text-sm ${
            filter === "unmapped"
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          }`}
        >
          Unmapped ({summary.unmapped})
        </button>
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No source codes{filter === "unmapped" ? " need mapping" : " found"}.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Source Code</th>
                <th className="px-3 py-2 text-left font-medium">Domain</th>
                <th className="px-3 py-2 text-right font-medium">Rows</th>
                <th className="px-3 py-2 text-left font-medium">Current Concept</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Status</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Target</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map((sc, i) => (
                <SourceCodeRowWithEditor
                  key={`${sc.omop_table}-${sc.source_value}-${i}`}
                  sc={sc}
                  isEditing={editingIndex === i}
                  vocabularies={reference?.destination_vocabularies}
                  onClick={() => handleRowClick(i)}
                  onSaved={handleSaved}
                  onCancel={handleCancel}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SourceCodeRowWithEditor({
  sc,
  isEditing,
  vocabularies,
  onClick,
  onSaved,
  onCancel,
}: {
  sc: PatientSourceCode;
  isEditing: boolean;
  vocabularies?: VocabularyItem[];
  onClick: () => void;
  onSaved: (saved: SavedMapping, concept: DestinationConcept, statusLabel: string) => void;
  onCancel: () => void;
}) {
  const badge = STATUS_BADGE[sc.mapping_status] || STATUS_BADGE.unmapped;
  const sourceLabel = `${sc.source_vocabulary_id || "Uncoded"}:${sc.source_code || sc.source_value}`;

  return (
    <>
      <tr className="cursor-pointer hover:bg-muted/30" onClick={onClick}>
        <td className="px-3 py-2">
          <div className="font-mono text-xs">{sc.source_value}</div>
          {sc.source_vocabulary_id && (
            <div className="text-xs text-muted-foreground">{sc.source_vocabulary_id}</div>
          )}
        </td>
        <td className="px-3 py-2 text-xs text-muted-foreground">
          {TABLE_LABELS[sc.omop_table] || sc.omop_table}
        </td>
        <td className="px-3 py-2 text-right tabular-nums">{sc.row_count}</td>
        <td className="px-3 py-2 text-xs">
          {sc.concept_id === 0 ? (
            <span className="text-muted-foreground">No matching concept</span>
          ) : (
            <span>
              {sc.concept_name || `Concept ${sc.concept_id}`}
              <span className="ml-1 text-muted-foreground">({sc.concept_id})</span>
            </span>
          )}
        </td>
        <td className="px-3 py-2">
          <span
            className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${badge.bg} ${badge.text}`}
          >
            {badge.label}
          </span>
        </td>
        <td className="px-3 py-2 text-xs">
          {sc.mapping_target_concept_id ? (
            <span>
              {sc.mapping_target_concept_name || `Concept ${sc.mapping_target_concept_id}`}
              <span className="ml-1 text-muted-foreground">
                ({sc.mapping_target_concept_id})
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground">&mdash;</span>
          )}
        </td>
      </tr>
      {isEditing && (
        <tr>
          <td colSpan={6} className="bg-slate-50 p-3">
            {sc.mapping_id ? (
              <InlineDestinationPicker
                key={sc.mapping_id}
                mappingId={sc.mapping_id}
                sourceLabel={sourceLabel}
                vocabularies={vocabularies}
                onSaved={(saved, concept) =>
                  onSaved(saved, concept, saved.status === "approved" ? "approved" : "proposed")
                }
                onCancel={onCancel}
              />
            ) : (
              <NewMappingPicker
                sc={sc}
                sourceLabel={sourceLabel}
                vocabularies={vocabularies}
                onSaved={onSaved}
                onCancel={onCancel}
              />
            )}
          </td>
        </tr>
      )}
    </>
  );
}

/** Inline picker for source codes that have no SCCM row yet. Creates the mapping on save. */
function NewMappingPicker({
  sc,
  sourceLabel,
  vocabularies = [],
  onSaved,
  onCancel,
}: {
  sc: PatientSourceCode;
  sourceLabel: string;
  vocabularies?: VocabularyItem[];
  onSaved: (saved: SavedMapping, concept: DestinationConcept, statusLabel: string) => void;
  onCancel: () => void;
}) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [vocabulary, setVocabulary] = useState("");
  const [results, setResults] = useState<DestinationConcept[]>([]);
  const [selected, setSelected] = useState<DestinationConcept | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const writing = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setResults([]);
    setActiveIndex(-1);
    if (query.trim().length < 3 || selected) {
      setSearching(false);
      return () => { active = false; controller.abort(); };
    }
    setSearching(true);
    const timer = window.setTimeout(async () => {
      try {
        const matches = await searchDestinationConcepts(query, vocabulary, controller.signal);
        if (active) setResults(matches);
      } catch {
        if (active) setError("Could not search destinations. Change the search to try again.");
      } finally {
        if (active) setSearching(false);
      }
    }, 250);
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [query, vocabulary, selected]);

  const choose = (concept: DestinationConcept) => {
    setSelected(concept);
    setResults([]);
    setError("");
  };

  const save = async (approve: boolean) => {
    if (!selected || writing.current) return;
    writing.current = true;
    setSaving(true);
    setError("");
    try {
      const domain = TABLE_TO_DOMAIN[sc.omop_table] || "";
      const { data } = await api.post<SavedMapping>("/v1/code-mappings/", {
        source_code: sc.source_code || sc.source_value,
        source_vocabulary_id: sc.source_vocabulary_id || "",
        domain_id: domain,
        omop_table: sc.omop_table,
        destination_concept_id: selected.concept_id,
        status: approve ? "approved" : "proposed",
      });
      onSaved(data, selected, approve ? "approved" : "proposed");
    } catch (failure) {
      setError(destinationError(failure));
    } finally {
      writing.current = false;
      setSaving(false);
    }
  };

  return (
    <section aria-label={`Choose destination for ${sourceLabel}`} className="space-y-3 rounded-md border border-slate-300 bg-slate-50 p-4 text-left text-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="font-semibold text-slate-950">Choose a destination</h4>
          <p className="mt-0.5 text-xs text-slate-600">{sourceLabel}</p>
        </div>
        <button type="button" aria-label="Close destination picker" onClick={onCancel} disabled={saving} className="rounded p-1 text-slate-500 hover:bg-slate-200 disabled:opacity-50">
          <X size={16} />
        </button>
      </div>
      {error && <p role="alert" className="text-rose-700">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <label className="relative min-w-60 flex-1">
          <span className="sr-only">Search destination concepts inline</span>
          <Search size={15} className="pointer-events-none absolute left-3 top-3 text-slate-400" />
          <input
            autoFocus
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={results.length > 0}
            aria-controls={listId}
            aria-activedescendant={activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
            value={query}
            disabled={saving}
            placeholder="Search by name, code or OMOP ID"
            className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 outline-none focus:border-slate-700"
            onChange={(event) => { setQuery(event.target.value); setSelected(null); setError(""); }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" && results.length) { event.preventDefault(); setActiveIndex((index) => (index + 1) % results.length); }
              if (event.key === "ArrowUp" && results.length) { event.preventDefault(); setActiveIndex((index) => (index <= 0 ? results.length : index) - 1); }
              if (event.key === "Enter") { event.preventDefault(); if (activeIndex >= 0 && results[activeIndex]) choose(results[activeIndex]); }
              if (event.key === "Escape" && !saving) { event.preventDefault(); onCancel(); }
            }}
          />
        </label>
        {vocabularies.length > 0 && (
          <select
            aria-label="Inline search vocabulary"
            value={vocabulary}
            disabled={saving}
            onChange={(event) => { setVocabulary(event.target.value); setSelected(null); setError(""); }}
            className="h-10 max-w-64 rounded-md border border-slate-300 bg-white px-3"
          >
            <option value="">All vocabularies</option>
            {vocabularies.map((item) => (
              <option key={item.vocabulary_id} value={item.vocabulary_id}>{item.vocabulary_name || item.vocabulary_id}</option>
            ))}
          </select>
        )}
      </div>
      <p className="text-xs text-slate-500">Active, standard concepts and HealthKey concepts. Type at least 3 characters.</p>
      {searching && <p role="status" className="text-slate-600">Searching destinations...</p>}
      {!searching && query.trim().length >= 3 && !selected && !results.length && !error && (
        <p role="status">No matching destinations. Try another name or code.</p>
      )}
      {results.length > 0 && (
        <ul id={listId} role="listbox" aria-label="Destination search results" className="max-h-64 overflow-y-auto rounded border border-slate-200 bg-white">
          {results.map((concept, index) => (
            <li key={concept.concept_id} role="none">
              <button
                type="button"
                role="option"
                id={`${listId}-${index}`}
                aria-selected={activeIndex === index}
                onClick={() => choose(concept)}
                className={`block w-full border-b border-slate-100 px-3 py-2 text-left hover:bg-slate-100 ${activeIndex === index ? "bg-slate-100" : ""}`}
              >
                <span className="font-medium text-slate-950">{concept.concept_name}</span>
                <span className="mt-0.5 block text-xs text-slate-500">{concept.vocabulary_id}:{concept.concept_code} · OMOP {concept.concept_id}</span>
                {(concept.measurement_type || concept.suggested_unit) && (
                  <ConceptInputDetails domain_id={concept.domain_id} measurement_type={concept.measurement_type} suggested_unit={concept.suggested_unit} example_units={concept.example_units} />
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      {selected && (
        <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2" role="status">
          <p className="font-medium text-slate-950">Selected: {selected.concept_name}</p>
          <p className="text-xs text-slate-600">{selected.vocabulary_id}:{selected.concept_code} · OMOP {selected.concept_id}</p>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" disabled={!selected || saving} onClick={() => void save(false)} className="rounded-md bg-slate-950 px-3 py-2 font-medium text-white hover:bg-slate-800 disabled:opacity-40">
          {saving ? "Saving..." : "Save choice"}
        </button>
        <button type="button" disabled={saving} onClick={onCancel} className="rounded-md px-3 py-2 text-slate-600 hover:bg-slate-200 disabled:opacity-40">Cancel</button>
        <span className="text-xs text-slate-500">Save choice creates a proposed mapping for review.</span>
      </div>
    </section>
  );
}
