/**
 * EditMappingDialog -- full-featured dialog for creating or editing a code
 * mapping.  Extracted from CodeMappingPage so both that page and
 * PatientSourceCodesTab can reuse it.
 *
 * The component manages all internal dialog state (form, concept search,
 * suggest-one, destination options, repointing progress) and communicates
 * with its parent through props:
 *
 *   open / onClose      -- visibility
 *   mode / row          -- new or edit, the row being edited
 *   reference           -- domain & vocabulary reference data
 *   canApprove          -- whether the user can set status to Approved
 *   strategies / rankingModel -- suggest settings from the parent
 *   onSaved / onDeleted -- callbacks after a successful write or delete
 *   onBanner            -- surface informational banners to the parent
 */

import CanonicalUnitEditor from "./CanonicalUnitEditor";
import IndividualSuggestCandidates from "./IndividualSuggestCandidates";
import SourceVocabularyLookup from "./SourceVocabularyLookup";
import { searchDestinationConcepts } from "./destinationSearch";
import MintConceptDialog from "./MintConceptDialog";
import ConceptInputDetails from "@/components/UI/ConceptInputDetails";
import { HelpTip, Field, ReadOnlyField, INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";
import { confirmUnitOverride, unitConfirmation } from "./unitConfirmation";
import SourceEvidencePanel, { type SourceEvidence } from "./SourceEvidencePanel";
import api from "@/api/axios";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Search, Sparkles, Trash2, X } from "lucide-react";

import type { CandidateActivity } from "./SuggestCandidates";
import {
  type CodeMappingRow,
  type ConceptResult,
  type DestinationOption,
  type MappingForm,
  type Reference,
  type RepointResult,
  type SearchScope,
  type SuggestRunProgress,
  buildEditForm,
  emptyForm,
  omopTableFor,
  retirementDetail,
  approvalNote,
  strategyLabel,
  DEFAULT_SEARCH_SCOPE,
  CONCEPT_SEARCH_DEBOUNCE_MS,
  SUGGEST_POLL_INTERVAL_MS,
  SUGGEST_POLL_MAX_FAILURES,
  SUGGEST_POLL_TIMEOUT_MS,
  STRATEGY_LABELS,
  TIP,
} from "./codeMappingTypes";

// Re-export types that callers may need.
export type { CodeMappingRow, Reference, RepointResult, ConceptResult };

// ── Props ───────────────────────────────────────────────────────────

export interface EditMappingDialogProps {
  open: boolean;
  onClose: () => void;
  mode: "new" | "edit";
  row: CodeMappingRow | null;
  reference: Reference;
  canApprove: boolean;
  strategies: { umls: boolean; lexical: boolean; vectors: boolean };
  rankingModel: "anthropic" | "jev" | "both";
  onStrategiesChange?: (next: { umls: boolean; lexical: boolean; vectors: boolean }) => void;
  onRankingModelChange?: (next: "anthropic" | "jev" | "both") => void;
  onSaved: (saved: CodeMappingRow, repoint: RepointResult | null) => void;
  onDeleted: (row: CodeMappingRow) => void;
  onBanner?: (message: string | null) => void;
}

// ── Component ───────────────────────────────────────────────────────

export default function EditMappingDialog({
  open,
  onClose,
  mode,
  row: selectedRow,
  reference,
  canApprove,
  strategies,
  rankingModel,
  onStrategiesChange,
  onRankingModelChange,
  onSaved,
  onDeleted,
  onBanner,
}: EditMappingDialogProps) {
  // ── refs ────────────────────────────────────────────────────────
  const dialogRequest = useRef(0);
  const dialogChoice = useRef<number | null>(null);
  const conceptSearchTimer = useRef<number | null>(null);
  const conceptSearchAbort = useRef<AbortController | null>(null);

  // ── state ───────────────────────────────────────────────────────
  const [form, setForm] = useState<MappingForm>(emptyForm);
  const [searchVocabulary, setSearchVocabulary] = useState("");
  const [searchScope, setSearchScope] = useState<SearchScope>(DEFAULT_SEARCH_SCOPE);
  const [conceptSearchQuery, setConceptSearchQuery] = useState("");
  const [conceptResults, setConceptResults] = useState<ConceptResult[]>([]);
  const [destinationOptions, setDestinationOptions] = useState<DestinationOption[]>([]);
  const [loadingDestinations, setLoadingDestinations] = useState(false);
  const [destinationError, setDestinationError] = useState("");
  const [sourceEvidence, setSourceEvidence] = useState<SourceEvidence | null>(null);
  const [sourceEvidenceError, setSourceEvidenceError] = useState("");
  const [searchingConcepts, setSearchingConcepts] = useState(false);
  const [suggestionMessage, setSuggestionMessage] = useState("");
  const [individualSuggestion, setIndividualSuggestion] = useState<{ request: number; activity: CandidateActivity[]; running: boolean } | null>(null);
  const [repointing, setRepointing] = useState<{ from: string; to: string } | null>(null);
  const [repointResult, setRepointResult] = useState<RepointResult | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [mintOpen, setMintOpen] = useState(false);
  const [mintUnitOpen, setMintUnitOpen] = useState(false);

  const evidenceFacilities = sourceEvidence?.facilities || [];

  // ── Derived values ──────────────────────────────────────────────
  const isNewMapping = !selectedRow?.mapping_id;
  const sourceIdentityUnchanged = Boolean(
    selectedRow
    && form.source_code === selectedRow.source_code
    && form.source_vocabulary_id === selectedRow.source_vocabulary_id,
  );
  const willRepoint =
    mode === "edit"
    && form.status === "approved"
    && selectedRow !== null
    && String(selectedRow.destination_concept_id) !== form.destination_concept_id;

  const hasRetrieval = strategies.umls || strategies.lexical || strategies.vectors;

  /** Source code systems offered for the chosen domain, blank option first. */
  const sourceCodeSystems = useMemo(() => {
    const offered = reference.source_code_systems_by_domain[form.domain_id] || [];
    const withBlank = offered.some((s) => s.vocabulary_id === "")
      ? offered
      : [{ vocabulary_id: "", label: "None -- uncoded / free text" }, ...offered];
    const current = form.source_vocabulary_id;
    if (current && !withBlank.some((s) => s.vocabulary_id === current)) {
      return [...withBlank, { vocabulary_id: current, label: `${current} -- not typical for this domain` }];
    }
    return withBlank;
  }, [reference, form.domain_id, form.source_vocabulary_id]);

  // ── Initialize form when dialog opens ───────────────────────────
  useEffect(() => {
    if (!open) return;
    // Wrapped in an async IIFE to satisfy react-hooks/set-state-in-effect.
    (async () => {
      dialogRequest.current += 1;
      dialogChoice.current = null;
      setError("");
      setSearchingConcepts(false);
      setSuggestionMessage("");
      setRepointResult(null);
      setRepointing(null);
      setSaving(false);
      setMintOpen(false);
      setIndividualSuggestion(null);

      if (mode === "new") {
        // When opened from PatientSourceCodesTab, selectedRow carries the
        // source identity (code, vocabulary, domain, table) even though no
        // mapping exists yet.  Pre-populate those fields so the curator does
        // not have to re-type them.
        setForm(selectedRow ? buildEditForm(selectedRow, reference) : { ...emptyForm });
        setSearchVocabulary("");
        setSearchScope(DEFAULT_SEARCH_SCOPE);
        setConceptSearchQuery("");
        setConceptResults([]);
      } else if (selectedRow) {
        setForm(buildEditForm(selectedRow, reference));
        setSearchVocabulary(
          reference.destination_vocabularies.some((v) => v.vocabulary_id === selectedRow.destination_vocabulary_id)
            ? selectedRow.destination_vocabulary_id || ""
            : "",
        );
        setSearchScope(DEFAULT_SEARCH_SCOPE);
        setConceptSearchQuery("");
        setConceptResults([]);
      }
    })();
  }, [open, mode, selectedRow, reference]);

  // ── Acquire edit lock on open ───────────────────────────────────
  useEffect(() => {
    if (!open || mode !== "edit" || !selectedRow?.mapping_id) return;
    let cancelled = false;
    (async () => {
      try {
        await api.post(`/v1/code-mappings/${selectedRow.mapping_id}/lock/`);
      } catch (err) {
        if (cancelled) return;
        const resp = err && typeof err === "object" && "response" in err
          ? (err as { response?: { status?: number; data?: { detail?: string; locked_by?: string } } }).response
          : undefined;
        if (resp?.status === 423) {
          setError(`Locked by ${resp.data?.locked_by || "another user"}.`);
        }
        // Non-lock errors -- still allow editing, the save may work.
      }
    })();
    return () => { cancelled = true; };
  }, [open, mode, selectedRow?.mapping_id]);

  // ── Load destination options & source evidence for edit mode ─────
  useEffect(() => {
    let active = true;
    (async () => {
      setDestinationOptions([]);
      setDestinationError("");
      setSourceEvidence(null);
      setSourceEvidenceError("");
      if (!open || mode !== "edit" || !selectedRow?.mapping_id) return;
      setLoadingDestinations(true);
      try {
        const { data } = await api.get<CodeMappingRow & { destination_options: DestinationOption[] }>(
          `/v1/code-mappings/${selectedRow.mapping_id}/`,
        );
        if (!active) return;
        setDestinationOptions(data.destination_options || []);
        setSourceEvidence(data.source_evidence || null);
      } catch {
        if (!active) return;
        setDestinationError("Could not load imported destinations. Close and reopen this mapping to retry.");
        setSourceEvidenceError("Could not load source evidence. Close and reopen this mapping to retry.");
      } finally {
        if (active) setLoadingDestinations(false);
      }
    })();
    return () => { active = false; };
  }, [open, mode, selectedRow?.mapping_id]);

  // ── Cleanup timers on unmount ───────────────────────────────────
  useEffect(() => () => {
    dialogRequest.current += 1;
    if (conceptSearchTimer.current !== null) window.clearTimeout(conceptSearchTimer.current);
    conceptSearchAbort.current?.abort();
  }, []);

  // ── Handlers ────────────────────────────────────────────────────

  const closeDialog = useCallback(() => {
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setError("");
    setSearchingConcepts(false);
    setMintOpen(false);
    // Release edit lock.
    if (selectedRow?.mapping_id) {
      api.delete(`/v1/code-mappings/${selectedRow.mapping_id}/lock/`).catch(() => {});
    }
    setSaving(false);
    setRepointing(null);
    setRepointResult(null);
    onClose();
  }, [selectedRow?.mapping_id, onClose]);

  const setField = useCallback((field: keyof MappingForm, value: string) => {
    if (field.startsWith("source_") || field.startsWith("destination_")) setSuggestionMessage("");
    if (field.startsWith("destination_")) dialogChoice.current = dialogRequest.current;
    if (["source_code", "source_vocabulary_id", "source_code_description", "omop_table"].includes(field)) {
      dialogRequest.current += 1;
      setSearchingConcepts(false);
      setError("");
    }
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const setDomain = useCallback((domainId: string) => {
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setSearchingConcepts(false);
    setError("");
    setForm((prev) => {
      const offered = reference.source_code_systems_by_domain[domainId] || [];
      const stillOffered =
        !prev.source_vocabulary_id
        || offered.some((s) => s.vocabulary_id === prev.source_vocabulary_id);
      return {
        ...prev,
        domain_id: domainId,
        source_vocabulary_id: stillOffered ? prev.source_vocabulary_id : "",
        omop_table: omopTableFor(reference, domainId),
      };
    });
  }, [reference]);

  const applyConcept = useCallback((concept: ConceptResult, adoptDomain = false) => {
    dialogChoice.current = dialogRequest.current;
    setSuggestionMessage("");
    setForm((prev) => {
      const domainId = (adoptDomain ? concept.domain_id : prev.domain_id) || concept.domain_id || "";
      return {
        ...prev,
        domain_id: domainId,
        destination_concept_id: String(concept.concept_id),
        destination_concept_name: concept.concept_name,
        destination_concept_code: concept.concept_code || "",
        destination_vocabulary_id: concept.vocabulary_id,
        destination_concept_class_id: concept.concept_class_id || "",
        standard_concept: concept.standard_concept || "",
        destination_invalid_reason: concept.invalid_reason || "",
        omop_table: (adoptDomain ? "" : prev.omop_table) || omopTableFor(reference, domainId),
        measurement_type: concept.measurement_type || "",
        suggested_unit: concept.suggested_unit || "",
        example_units: concept.example_units || [],
      };
    });
  }, [reference]);

  const resolveConceptId = useCallback(async (rawId: string) => {
    const id = rawId.trim();
    if (!id) return;
    try {
      const resp = await api.get<ConceptResult>(`/v1/concepts/${id}/`);
      applyConcept(resp.data);
      setError("");
    } catch {
      setError(`No OMOP concept with id ${id}.`);
      setForm((prev) => ({
        ...prev,
        destination_concept_code: "",
        destination_concept_class_id: "",
        standard_concept: "",
        destination_invalid_reason: "",
      }));
    }
  }, [applyConcept]);

  const selectReplacement = useCallback(async () => {
    if (!form.destination_concept_id) return;
    try {
      const { data } = await api.get(`/v1/concepts/${form.destination_concept_id}/replacement/`);
      if (!data.replaced) {
        onBanner?.("No active replacement is recorded; search for a current destination concept.");
        return;
      }
      applyConcept(data.resolved_concept);
      onBanner?.(`Replaced with active concept ${data.resolved_concept.concept_id}.`);
    } catch {
      setError("Could not look up a replacement concept.");
    }
  }, [form.destination_concept_id, applyConcept, onBanner]);

  const searchConcepts = useCallback((query: string, vocabulary?: string, scope?: SearchScope) => {
    const effectiveVocabulary = vocabulary ?? searchVocabulary;
    const effectiveScope = scope ?? searchScope;
    const request = ++dialogRequest.current;
    setConceptSearchQuery(query);
    if (conceptSearchTimer.current !== null) window.clearTimeout(conceptSearchTimer.current);
    conceptSearchTimer.current = null;
    conceptSearchAbort.current?.abort();
    conceptSearchAbort.current = null;
    const q = query.trim();
    if (q.length < 3) {
      setSearchingConcepts(false);
      setConceptResults([]);
      return;
    }
    setSearchingConcepts(true);
    conceptSearchTimer.current = window.setTimeout(async () => {
      conceptSearchTimer.current = null;
      if (request !== dialogRequest.current) return;
      const controller = new AbortController();
      conceptSearchAbort.current = controller;
      try {
        const matches = await searchDestinationConcepts(q, effectiveVocabulary, controller.signal, effectiveScope);
        if (request === dialogRequest.current) setConceptResults(matches);
      } catch {
        if (request === dialogRequest.current) setConceptResults([]);
      } finally {
        if (request === dialogRequest.current) setSearchingConcepts(false);
      }
    }, CONCEPT_SEARCH_DEBOUNCE_MS);
  }, [searchVocabulary, searchScope]);

  const suggestCurrentCode = useCallback(async () => {
    setSuggestionMessage("");
    const request = ++dialogRequest.current;
    dialogChoice.current = null;
    setIndividualSuggestion({ request, activity: [], running: true });
    setError("");
    setSearchingConcepts(true);
    try {
      const enabled = Object.entries(strategies).filter(([, on]) => on).map(([name]) => name);
      const { data: started } = await api.post<SuggestRunProgress>("/v1/code-mappings/suggest-one/", {
        source_code: form.source_code, source_vocabulary_id: form.source_vocabulary_id,
        source_code_description: form.source_code_description, omop_table: form.omop_table,
        strategies: enabled, ranking_model: rankingModel, async: true,
      });
      let current = started;
      const deadline = Date.now() + SUGGEST_POLL_TIMEOUT_MS;
      let failures = 0;
      while (request === dialogRequest.current) {
        setIndividualSuggestion({ request, activity: current.activity ?? [], running: current.state === "queued" || current.state === "running" });
        if (current.state !== "queued" && current.state !== "running") break;
        if (Date.now() > deadline) throw new Error("Suggestion timed out");
        await new Promise(resolve => setTimeout(resolve, SUGGEST_POLL_INTERVAL_MS));
        if (request !== dialogRequest.current) return;
        try {
          const { data } = await api.get<SuggestRunProgress>(`/v1/code-mappings/suggest-runs/${started.run_id}/`, {
            params: { include_activity: "1" },
          });
          current = data;
          failures = 0;
        } catch (fetchError) {
          if (++failures > SUGGEST_POLL_MAX_FAILURES) throw fetchError;
        }
      }
      if (request !== dialogRequest.current) return;
      if (current.state === "failure") {
        setError(current.error || "Failed to suggest a destination concept.");
        return;
      }
      const result = current.activity?.filter(event => event.stage === "result").at(-1);
      if (result?.suggested && dialogChoice.current !== request) {
        applyConcept(result.suggested as ConceptResult);
        setSuggestionMessage("Winner filled in. You can choose another candidate before saving.");
      } else if (!result?.suggested && dialogChoice.current !== request) {
        setSuggestionMessage("No winner selected. You can choose a candidate or search below.");
      }
    } catch {
      if (request === dialogRequest.current) setError("Failed to suggest a destination concept. Any candidates already shown are still selectable.");
    } finally {
      if (request === dialogRequest.current) {
        setSearchingConcepts(false);
        setIndividualSuggestion(cur => cur?.request === request ? { ...cur, running: false } : cur);
      }
    }
  }, [strategies, rankingModel, form.source_code, form.source_vocabulary_id, form.source_code_description, form.omop_table, applyConcept]);

  const submitForm = useCallback(async (event: React.FormEvent) => {
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
        destination_unit_concept_id: form.destination_unit_concept_id
          ? Number(form.destination_unit_concept_id) : null,
      };
      let resp;
      try {
        resp = selectedRow?.mapping_id
          ? await api.patch(`/v1/code-mappings/${selectedRow.mapping_id}/`, payload)
          : await api.post("/v1/code-mappings/", payload);
      } catch (err) {
        const warning = unitConfirmation(err);
        if (!warning || !selectedRow?.mapping_id) throw err;
        if (!confirmUnitOverride(warning)) {
          setRepointing(null);
          setSaving(false);
          return;
        }
        resp = await api.patch(`/v1/code-mappings/${selectedRow.mapping_id}/`, {
          ...payload, confirm_unit_mismatch: true,
        });
      }
      const repoint: RepointResult | null = resp.data?.repoint ?? null;
      // Hold the dialog open on a re-point so the curator sees what moved.
      if (repoint && repoint.rows_updated) {
        setRepointResult(repoint);
        setRepointing(null);
        setSaving(false);
        onSaved(resp.data, repoint);
        return;
      }
      onSaved(resp.data, repoint);
      closeDialog();
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string } | Record<string, string[]> } }).response?.data
          : undefined;
      const message =
        detail && typeof detail === "object" && !Array.isArray(detail)
          ? Object.entries(detail).map(([field, value]) => `${field === "detail" ? "" : `${field}: `}${Array.isArray(value) ? value.join(", ") : value}`).join(" ")
          : "";
      setError(message || "Failed to save code mapping.");
      setRepointing(null);
      setSaving(false);
    }
  }, [form, selectedRow, willRepoint, onSaved, closeDialog]);

  const deleteMapping = useCallback(async (row: CodeMappingRow) => {
    if (!row.mapping_id) return;
    setError("");
    try {
      const response = await api.delete(`/v1/code-mappings/${row.mapping_id}/`);
      if (response.status === 200 && response.data?.mapping_id) {
        // Backend cleared the destination instead of deleting.
        onDeleted(response.data as CodeMappingRow);
      } else {
        onDeleted(row);
      }
      closeDialog();
    } catch {
      setError("Failed to delete code mapping.");
    }
  }, [onDeleted, closeDialog]);

  // ── Render ──────────────────────────────────────────────────────

  if (!open) return null;

  return (
    <>
      <div inert={mintOpen} className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
        <form
          onSubmit={submitForm}
          role="dialog"
          aria-label={mode === "new" ? "New Mapping" : "Edit Mapping"}
          className="w-full max-w-4xl rounded-md bg-white shadow-xl"
        >
          <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
            <h2 className="text-lg font-semibold text-slate-950">
              {mode === "new" ? "New Mapping" : "Edit Mapping"}
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
            {error && (
              <div role="alert" className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                {error}
              </div>
            )}
            {/* -- SOURCE ------------------------------------------------- */}
            <fieldset
              data-testid="source-block"
              className="mb-5 rounded-md border border-slate-200 p-4"
            >
              <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Source -- the code as it arrived
              </legend>
              {mode === "edit" && selectedRow && (
                <p data-testid="source-organization" className="mb-4 text-sm text-slate-700">
                  <span className="font-medium">Hospital / organization: </span>
                  {selectedRow.organization_name || sourceEvidence?.organization?.name
                    || (evidenceFacilities.length
                      ? `${evidenceFacilities[0].name}${evidenceFacilities.length > 1 ? ` (+${evidenceFacilities.length - 1} more)` : ""}`
                      : "Global / unattributed")}
                  {(selectedRow.organization_slug || sourceEvidence?.organization?.slug) && (
                    <span className="ml-1 text-xs text-slate-500">
                      ({selectedRow.organization_slug || sourceEvidence?.organization?.slug})
                    </span>
                  )}
                </p>
              )}
              <div data-testid="source-fields" className="grid gap-4 md:grid-cols-2">
                <Field id="domain_id" label="Domain" tip={TIP.domain}>
                  <select
                    id="domain_id"
                    title={TIP.domain}
                    value={form.domain_id}
                    onChange={(e) => setDomain(e.target.value)}
                    required
                    className={INPUT_CLASS}
                  >
                    <option value="">-- select --</option>
                    {reference.domains.map((d) => (
                      <option key={d.domain_id} value={d.domain_id}>{d.label || d.domain_id}</option>
                    ))}
                  </select>
                </Field>

                <Field id="source_vocabulary_id" label="Source Code System" tip={TIP.source_code_system}>
                  <select
                    id="source_vocabulary_id"
                    title={TIP.source_code_system}
                    value={form.source_vocabulary_id}
                    onChange={(e) => setField("source_vocabulary_id", e.target.value)}
                    className={INPUT_CLASS}
                  >
                    {sourceCodeSystems.map((s) => (
                      <option key={s.vocabulary_id || "__none__"} value={s.vocabulary_id}>
                        {s.label || s.vocabulary_id}
                      </option>
                    ))}
                  </select>
                </Field>

                <Field id="source_code" label="Source Code Value" tip={TIP.source_code_value}>
                  <input
                    id="source_code"
                    title={TIP.source_code_value}
                    value={form.source_code}
                    onChange={(e) => setField("source_code", e.target.value)}
                    required
                    className={`${INPUT_CLASS} font-mono`}
                  />
                  {sourceIdentityUnchanged && (selectedRow?.source_concept_id || selectedRow?.source_retired) && (
                    <p data-testid="source-code-metadata" className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-600">
                      {selectedRow.source_concept_id && <span>(OMOP concept {selectedRow.source_concept_id})</span>}
                      {selectedRow.source_retired && <span className="font-semibold text-red-700"
                        title={retirementDetail(selectedRow)}>Retired</span>}
                    </p>
                  )}
                </Field>

                <Field id="source_code_description" label="Source Description" tip={TIP.source_description}>
                  <input
                    id="source_code_description"
                    title={TIP.source_description}
                    value={form.source_code_description}
                    onChange={(e) => setField("source_code_description", e.target.value)}
                    className={INPUT_CLASS}
                  />
                </Field>

                {(reference.source_catalog_vocabularies || []).includes(form.source_vocabulary_id) && (
                  <SourceVocabularyLookup
                    vocabularyId={form.source_vocabulary_id}
                    code={form.source_code}
                    onSelect={(term) => {
                      setField("source_code", term.code);
                      setField("source_code_description", term.name.slice(0, 255));
                      setField("source_concept_id", "");
                    }}
                  />
                )}

                {form.source_unit && (
                  <ReadOnlyField id="source_unit_display" label="Source Unit" tip="Unit string as reported by the ETL source." value={form.source_unit} />
                )}
                {selectedRow?.example_quantity && (
                  <ReadOnlyField id="example_quantity_display" label="Example Qty" tip="Representative quantity value(s) from the source data." value={selectedRow.example_quantity} />
                )}
              </div>
              {form.source_metadata_notes && (
                <div className="mt-3">
                  <label className="mb-1 block text-xs font-medium text-slate-600">Source Notes</label>
                  <pre className="whitespace-pre-wrap rounded border bg-slate-50 px-3 py-2 text-xs text-slate-700">
                    {form.source_metadata_notes}
                  </pre>
                </div>
              )}
            </fieldset>

            {mode === "edit" && selectedRow?.mapping_id && (
              <SourceEvidencePanel evidence={sourceEvidence} loading={loadingDestinations}
                error={sourceEvidenceError} />
            )}

            {/* -- DESTINATION -------------------------------------------- */}
            <fieldset
              data-testid="destination-block"
              className="rounded-md border border-slate-200 p-4"
            >
              <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Destination -- the OMOP concept it means
              </legend>

              {mode === "edit" && (
                <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-3">
                  {(selectedRow?.destination_count || destinationOptions.length) > 1 && (
                    <p className="mb-2 font-semibold text-amber-900">
                      Multiple destinations are available for this source code. Review the source data alternatives and choose the correct destination.
                    </p>
                  )}
                  {loadingDestinations && <p role="status">Loading source destinations...</p>}
                  {destinationError && <p role="alert" className="text-red-700">{destinationError}</p>}
                  {destinationOptions.length > 0 && (
                    <>
                      <label htmlFor="imported-destination" className="mb-1 block text-sm font-medium">
                        Source data destinations ({destinationOptions.length})
                      </label>
                      <select id="imported-destination" className={INPUT_CLASS}
                        value={destinationOptions.some((option) => String(option.concept_id) === form.destination_concept_id) ? form.destination_concept_id : ""}
                        onChange={(event) => {
                          const option = destinationOptions.find((item) => String(item.concept_id) === event.target.value);
                          if (option?.selectable && option.concept_id !== null) {
                            applyConcept({ ...option, concept_id: option.concept_id }, true);
                          }
                        }}>
                        <option value="">Choose a destination</option>
                        {destinationOptions.map((option) => (
                          <option key={`${option.vocabulary_id}:${option.concept_code}`}
                            value={option.concept_id === null ? `unavailable:${option.vocabulary_id}:${option.concept_code}` : String(option.concept_id)}
                            disabled={!option.selectable}>
                            {option.concept_name} -- {option.vocabulary_id}:{option.concept_code} -- OMOP {option.concept_id ?? "not loaded"}
                            {option.measurement_type ? ` \u00b7 ${option.measurement_type === "quantitative" ? "Quantitative" : "Qualitative"}` : ""}
                            {option.suggested_unit ? ` \u00b7 ${option.suggested_unit}` : ""}
                            {option.origins.length ? ` (${option.origins.join(", ")})` : ""}
                            {!option.selectable ? " -- unavailable" : ""}
                          </option>
                        ))}
                      </select>
                      <p className="mt-1 text-xs text-slate-600">
                        Save your choice below. Imported alternatives are retained for review; unavailable targets cannot be selected.
                      </p>
                    </>
                  )}
                </div>
              )}

              {suggestionMessage && (
                <p role="status" className="mb-3 rounded-md bg-blue-50 px-3 py-2 text-sm text-blue-800">
                  {suggestionMessage}
                </p>
              )}

              {/* Search sits at the top: picking a concept fills everything below it. */}
              <div className="mb-4">
                <div className="mb-2 flex items-end justify-between gap-3">
                  <div className="flex items-center gap-1">
                    <label className="text-sm font-medium text-slate-700" htmlFor="code-mapping-concept-search">
                      Search destination concepts
                    </label>
                    <HelpTip tip={TIP.search} />
                  </div>
                  <div className="flex flex-wrap items-center gap-3">
                    {(["umls", "lexical", "vectors"] as const).map((key) => (
                      <div key={key} className="inline-flex items-center gap-1 text-xs text-slate-600">
                        <label className="inline-flex items-center gap-1">
                          <input type="checkbox" checked={strategies[key]}
                            onChange={(e) => onStrategiesChange?.({ ...strategies, [key]: e.target.checked })} />
                          {STRATEGY_LABELS[key]}
                        </label>
                        <HelpTip tip={key === "umls" ? "Bridge the code to an equivalent concept through UMLS. A unique match wins after the other enabled searches finish." : key === "lexical" ? "Retrieve candidate destinations by matching names and synonyms." : "Find candidate destinations by meaning, including concepts whose names and synonyms do not match the source wording."} />
                      </div>
                    ))}
                    <div className="inline-flex items-center gap-1 text-xs text-slate-600">
                      <label className="inline-flex items-center gap-1">
                        Ranker
                        <select
                          value={rankingModel}
                          onChange={(e) => onRankingModelChange?.(e.target.value as "anthropic" | "jev" | "both")}
                          className="h-7 rounded-md border border-slate-300 px-1.5 text-xs"
                        >
                          <option value="anthropic">Anthropic</option>
                          <option value="jev">Jev</option>
                          <option value="both">Both</option>
                        </select>
                      </label>
                      <HelpTip tip={TIP.ranker} />
                    </div>
                    <button
                      type="button"
                      onClick={() => void suggestCurrentCode()}
                      disabled={searchingConcepts || !hasRetrieval}
                      className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                    >
                      <Sparkles size={13} />
                      Suggest
                    </button>
                  </div>
                </div>
                {individualSuggestion?.request === dialogRequest.current && <IndividualSuggestCandidates
                  activity={individualSuggestion.activity} running={individualSuggestion.running}
                  selectedId={form.destination_concept_id}
                  onSelect={candidate => {
                    applyConcept(candidate as ConceptResult);
                    setSuggestionMessage("Candidate selected. Save the mapping to keep your choice.");
                  }} />}
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={15} />
                    <input
                      id="code-mapping-concept-search"
                      title={TIP.search}
                      value={conceptSearchQuery}
                      onChange={(e) => void searchConcepts(e.target.value)}
                      placeholder={
                        searchVocabulary
                          ? `Search ${searchVocabulary} concepts...`
                          : "Search destination concepts..."
                      }
                      className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
                    />
                  </div>
                  <div className="flex items-center gap-1">
                    <select
                      id="code-mapping-search-vocabulary"
                      aria-label="Search vocabulary"
                      title={TIP.search_vocabulary}
                      value={searchVocabulary}
                      onChange={(e) => {
                        setSearchVocabulary(e.target.value);
                        void searchConcepts(conceptSearchQuery, e.target.value);
                      }}
                      className="h-10 w-40 shrink-0 rounded-md border border-slate-300 px-2 text-sm text-slate-950"
                    >
                      <option value="">All vocabularies</option>
                      {reference.destination_vocabularies.map((v) => (
                        <option key={v.vocabulary_id} value={v.vocabulary_id}>{v.vocabulary_id}</option>
                      ))}
                    </select>
                    <HelpTip tip={TIP.search_vocabulary} />
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
                  <span>Showing active, standard concepts.</span>
                  {([
                    ["retired", "Include retired", TIP.search_include_retired],
                    ["nonStandard", "Include non-standard", TIP.search_include_non_standard],
                  ] as const).map(([key, label, tip]) => (
                    <span key={key} className="flex items-center gap-1">
                      <label className="flex items-center gap-1" title={tip}>
                        <input
                          type="checkbox"
                          checked={searchScope[key]}
                          onChange={(e) => {
                            const next = { ...searchScope, [key]: e.target.checked };
                            setSearchScope(next);
                            void searchConcepts(conceptSearchQuery, searchVocabulary, next);
                          }}
                        />
                        {label}
                      </label>
                      <HelpTip tip={tip} />
                    </span>
                  ))}
                </div>
                <div className="mt-2 max-h-40 overflow-y-auto rounded-md border border-slate-200">
                  {searchingConcepts && <div className="px-3 py-2 text-sm text-slate-500">Searching...</div>}
                  {!searchingConcepts && conceptResults.length === 0 && conceptSearchQuery.length >= 3 && (
                    <div className="px-3 py-2 text-sm text-slate-500">
                      {searchScope.retired && searchScope.nonStandard
                        ? "No suggestions found."
                        : "No active, standard concepts found. Widen the search with the options above."}
                    </div>
                  )}
                  {!searchingConcepts && conceptResults.map((concept) => (
                    <button
                      key={concept.concept_id}
                      type="button"
                      onClick={() => applyConcept(concept)}
                      className="w-full border-b border-slate-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-slate-50"
                    >
                      <span className="grid grid-cols-[8rem_1fr_6rem] gap-2">
                        <span className="font-mono text-slate-700">{concept.concept_code}</span>
                        <span className="text-slate-900">
                          {concept.concept_name}
                          {concept.invalid_reason ? (
                            <span className="ml-2 rounded bg-red-100 px-1 text-[10px] font-semibold uppercase text-red-800">Retired</span>
                          ) : concept.standard_concept === "S" ? (
                            <span className="ml-2 rounded bg-emerald-100 px-1 text-[10px] font-semibold uppercase text-emerald-800">Standard</span>
                          ) : (
                            <span className="ml-2 rounded bg-amber-100 px-1 text-[10px] font-semibold uppercase text-amber-800">Non-standard</span>
                          )}
                        </span>
                        <span className="font-mono text-slate-500">{concept.vocabulary_id}</span>
                      </span>
                      <ConceptInputDetails {...concept} />
                    </button>
                  ))}
                </div>
              </div>

              {/* Destination fields */}
              <div data-testid="destination-fields" className="grid gap-4 md:grid-cols-2">
                <Field id="destination_concept_id" label="Destination Concept ID" tip={TIP.destination_concept_id}>
                  <input
                    id="destination_concept_id"
                    title={TIP.destination_concept_id}
                    type="number"
                    value={form.destination_concept_id}
                    onChange={(e) => setField("destination_concept_id", e.target.value)}
                    onBlur={(e) => void resolveConceptId(e.target.value)}
                    required
                    className={`${INPUT_CLASS} font-mono`}
                  />
                </Field>

                <ReadOnlyField
                  id="destination_concept_name"
                  label="Destination Concept Name"
                  tip={TIP.destination_concept_name}
                  value={form.destination_concept_name}
                  testId="destination-concept-name"
                />

                <ReadOnlyField
                  id="destination_concept_code"
                  label="Destination Concept Code"
                  tip={TIP.destination_concept_code}
                  value={form.destination_concept_code}
                  testId="destination-concept-code"
                />
                <ReadOnlyField
                  id="destination_vocabulary_id"
                  label="Destination Vocabulary ID"
                  tip={TIP.destination_vocabulary_id}
                  value={form.destination_vocabulary_id}
                  testId="destination-vocabulary-id"
                />
                <ReadOnlyField
                  id="destination_concept_class_id"
                  label="Destination Concept Class"
                  tip={TIP.destination_concept_class}
                  value={form.destination_concept_class_id}
                  testId="destination-concept-class"
                />
                <ReadOnlyField
                  id="standard_concept"
                  label="Standard Concept"
                  tip={TIP.standard_concept}
                  value={form.standard_concept}
                  testId="standard-concept"
                />
                <ReadOnlyField
                  id="destination_status"
                  label="Destination Status"
                  tip="Active concepts can be used as destinations. A retired concept is no longer current; select an active replacement when one is available."
                  value={form.destination_invalid_reason ? `Retired / invalid (reason ${form.destination_invalid_reason})` : form.destination_concept_id ? "Active" : ""}
                  testId="destination-status"
                />
                <ReadOnlyField
                  id="omop_table"
                  label="Destination Table"
                  tip={TIP.destination_table}
                  value={form.omop_table}
                  testId="destination-table"
                />
                {form.suggested_unit && (
                  <ReadOnlyField
                    id="suggested_unit"
                    label="Suggested unit"
                    tip="A suggested unit, not a unit mandated by Athena. The instance canonical unit is configured below."
                    value={form.suggested_unit}
                    testId="suggested-unit"
                  />
                )}
              </div>
              {form.destination_concept_id && form.destination_vocabulary_id === "LOINC" && form.omop_table === "measurement" &&
                <CanonicalUnitEditor key={form.destination_concept_id} conceptId={Number(form.destination_concept_id)} />}
              {(form.omop_table === "measurement" || form.source_unit) && (
                <DestinationUnitPicker
                  sourceUnit={form.source_unit}
                  selectedConceptId={form.destination_unit_concept_id}
                  selectedConceptCode={form.destination_unit_concept_code}
                  selectedConceptName={form.destination_unit_concept_name}
                  matchType={form.unit_match_type}
                  onSelect={(unit) => {
                    setField("destination_unit_concept_id", unit ? String(unit.concept_id) : "");
                    setField("destination_unit_concept_code", unit?.concept_code || "");
                    setField("destination_unit_concept_name", unit?.concept_name || "");
                    setField("unit_match_type", unit?.match || "");
                  }}
                  onMintUnit={() => setMintUnitOpen(true)}
                />
              )}
              <div className="mt-3 flex justify-end gap-2">
                <button type="button" onClick={() => setMintOpen(true)} className="rounded border border-sky-300 px-3 py-2 text-sm text-sky-700">Mint new concept</button>
              </div>
              {form.destination_invalid_reason && (
                <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  <span>This destination is retired. Use its active replacement when available.</span>
                  <button type="button" onClick={() => void selectReplacement()} className="shrink-0 rounded border border-amber-400 px-2 py-1 text-xs font-medium hover:bg-amber-100">
                    Find replacement
                  </button>
                </div>
              )}
            </fieldset>

            {selectedRow && (
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <span className="inline-flex items-center gap-1.5 rounded-md bg-slate-100 px-2.5 py-1.5 text-xs font-medium text-slate-700">
                  Seen <span className="font-mono font-semibold">{selectedRow.occurrence_count || 0}</span> time{selectedRow.occurrence_count !== 1 ? "s" : ""}
                </span>
                <span className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium ${
                  selectedRow.destination_count !== 1 ? "bg-red-50 text-red-700" : "bg-slate-100 text-slate-700"
                }`}>
                  Destinations <span className="font-mono font-semibold">{selectedRow.destination_count ?? 0}</span>
                </span>
              </div>
            )}

            {selectedRow
              && (selectedRow.origin === "import"
                || selectedRow.created_by
                || approvalNote(selectedRow)) && (
              <p className="mt-2 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
                {selectedRow.origin === "import" ? (
                  <>
                    Proposed by import
                    {selectedRow.origin_system ? ` (${selectedRow.origin_system})` : ""}
                  </>
                ) : (
                  selectedRow.created_by ? <>Created by {selectedRow.created_by}</> : null
                )}
                {approvalNote(selectedRow)}
                {selectedRow.suggest_strategy ? (
                  <>
                    {" \u00b7 suggested via "}
                    <span className="font-medium">{strategyLabel[selectedRow.suggest_strategy] || selectedRow.suggest_strategy}</span>
                    {selectedRow.umls_cui ? ` (CUI ${selectedRow.umls_cui})` : ""}
                  </>
                ) : null}
              </p>
            )}

            <div className="mt-4 grid gap-1">
              <div className="flex items-center gap-1">
                <label className="text-sm font-medium text-slate-700" htmlFor="notes">Notes</label>
                <HelpTip tip={TIP.notes} />
              </div>
              <textarea
                id="notes"
                title={TIP.notes}
                value={form.notes}
                onChange={(e) => setField("notes", e.target.value)}
                rows={2}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm font-normal text-slate-950"
              />
            </div>

            {repointing && (
              <div
                role="status"
                className="mt-4 rounded-md border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900"
              >
                <div className="flex items-center gap-2 font-medium">
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
                  Updating concept {repointing.from} &rarr; {repointing.to}
                </div>
                <p className="mt-1 text-xs text-blue-800">Rewriting clinical rows already stored...</p>
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

          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 border-t border-slate-200 px-5 py-4">
            <div className="min-w-0 text-sm text-slate-700" data-testid="mapping-provenance">
              <span className="font-medium">Provenance</span>{" "}
              <span className="break-words text-slate-600">
                {selectedRow?.origin_system || "\u2014"}
              </span>
            </div>
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2">
                <label className="text-sm font-medium text-slate-700" htmlFor="status">Status</label>
                <HelpTip tip={TIP.status} />
                <select
                  id="status"
                  title={isNewMapping ? TIP.status_new : TIP.status}
                  value={isNewMapping ? "proposed" : form.status}
                  disabled={isNewMapping}
                  onChange={(e) => setField("status", e.target.value)}
                  className="h-9 rounded-md border border-slate-300 px-2 text-sm font-normal text-slate-950 disabled:bg-slate-100 disabled:text-slate-500"
                >
                  <option value="proposed">Proposed</option>
                  <option value="approved" disabled={!canApprove}>
                    {canApprove ? "Approved" : "Approved (admin only)"}
                  </option>
                  <option value="rejected">Rejected</option>
                </select>
              </div>
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
            <div className="flex justify-end gap-2">
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
                    : mode === "edit"
                      ? "Update Mapping"
                      : "Save Mapping"}
              </button>
            </div>
          </div>
        </form>
      </div>
      {mintOpen && (
        <MintConceptDialog
          vocabularies={reference.destination_vocabularies} domains={reference.domains}
          initialDomain={form.domain_id} initialName={form.source_code_description || form.source_code}
          sourceCode={form.source_code} sourceVocabulary={form.source_vocabulary_id}
          onClose={() => setMintOpen(false)}
          onSelect={concept => { applyConcept(concept as ConceptResult); setMintOpen(false); }}
        />
      )}
      {mintUnitOpen && (
        <MintConceptDialog
          vocabularies={[{ vocabulary_id: "HK-Units", vocabulary_name: "HealthKey Units" }]}
          domains={[{ domain_id: "Unit", label: "Unit" }]}
          initialDomain="Unit"
          initialName={form.source_unit}
          sourceCode={form.source_unit}
          sourceVocabulary=""
          onClose={() => setMintUnitOpen(false)}
          onSelect={(concept) => {
            setField("destination_unit_concept_id", String(concept.concept_id));
            setField("destination_unit_concept_code", concept.concept_code || "");
            setField("destination_unit_concept_name", concept.concept_name || "");
            setField("unit_match_type", "");
            setMintUnitOpen(false);
          }}
        />
      )}
    </>
  );
}

// ── Destination Unit Picker ─────────────────────────────────────────

interface UnitResult {
  concept_id: number;
  concept_code: string;
  concept_name: string;
  match: "exact" | "close";
}

interface DestinationUnitPickerProps {
  sourceUnit: string;
  selectedConceptId: string;
  selectedConceptCode: string;
  selectedConceptName: string;
  matchType: "exact" | "close" | "none" | "";
  onSelect: (unit: UnitResult | null) => void;
  onMintUnit: () => void;
}

function DestinationUnitPicker({
  sourceUnit, selectedConceptId, selectedConceptCode, selectedConceptName,
  matchType, onSelect, onMintUnit,
}: DestinationUnitPickerProps) {
  const [query, setQuery] = useState(sourceUnit);
  const [results, setResults] = useState<UnitResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) { setResults([]); setSearched(false); return; }
    setSearching(true);
    try {
      const { data } = await api.get<{ results: UnitResult[] }>("/v1/code-mappings/unit-search/", { params: { q } });
      setResults(data.results);
      setSearched(true);
      // Auto-select exact match
      if (!selectedConceptId && data.results.length > 0 && data.results[0].match === "exact") {
        onSelect(data.results[0]);
      } else if (data.results.length === 0) {
        onSelect(null);
      }
    } catch {
      setResults([]);
      setSearched(true);
    } finally {
      setSearching(false);
    }
  }, [selectedConceptId, onSelect]);

  // Auto-search on sourceUnit when dialog opens
  useEffect(() => {
    if (sourceUnit && !searched && !selectedConceptId) {
      (async () => { await doSearch(sourceUnit); })();
    }
  }, [sourceUnit, searched, selectedConceptId, doSearch]);

  const handleQueryChange = (value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => { void doSearch(value); }, 300);
  };

  const matchIndicator = matchType === "exact"
    ? <span className="text-green-600" title="Exact UCUM match">&#10003; Exact</span>
    : matchType === "close"
      ? <span className="text-amber-600" title="Close UCUM match">&#126; Close</span>
      : matchType === "none"
        ? <span className="text-red-600" title="No UCUM match">&#10007; No match</span>
        : null;

  return (
    <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-3">
      {selectedConceptId ? (
        <>
          <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-500">Destination Unit</span>
          <div className="flex items-center gap-2 text-sm">
            <span className="font-mono">{selectedConceptCode}</span>
            <span className="text-slate-500">{selectedConceptName}</span>
            {matchIndicator}
            <button type="button" onClick={() => onSelect(null)}
              className="ml-auto text-xs text-slate-500 hover:text-red-600">Clear</button>
          </div>
        </>
      ) : (
        <Field id="destination_unit_search" label="Destination Unit" tip="UCUM concept linked to this mapping. Used for unit conversion during ETL.">
          <div className="flex items-center gap-2">
            <input
              id="destination_unit_search"
              type="text"
              aria-label="Search UCUM units"
              value={query}
              onChange={(e) => handleQueryChange(e.target.value)}
              placeholder="Search UCUM units..."
              className={`${INPUT_CLASS} flex-1`}
            />
            {searching && <span className="text-xs text-slate-400">Searching...</span>}
          </div>
          {searched && results.length === 0 && !searching && (
            <div className="mt-2 flex items-center gap-2 text-xs text-red-600">
              <span>&#10007; No UCUM match found</span>
              <button type="button" onClick={onMintUnit}
                className="rounded border border-sky-300 px-2 py-1 text-sky-700 hover:bg-sky-50">
                Mint Unit
              </button>
            </div>
          )}
          {results.length > 0 && (
            <ul className="mt-1 max-h-40 overflow-y-auto rounded border bg-white text-xs">
              {results.map((r) => (
                <li key={r.concept_id}
                  className="flex cursor-pointer items-center gap-2 px-2 py-1.5 hover:bg-sky-50"
                  onClick={() => onSelect(r)}>
                  <span className="font-mono font-medium">{r.concept_code}</span>
                  <span className="text-slate-500">{r.concept_name}</span>
                  <span className={r.match === "exact" ? "ml-auto text-green-600" : "ml-auto text-amber-500"}>
                    {r.match === "exact" ? "exact" : "close"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Field>
      )}
    </div>
  );
}
