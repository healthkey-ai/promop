import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Pencil, Plus, Search, Sparkles, Trash2, X } from "lucide-react";
import api from "@/api/axios";
import { useAuth } from "@/hooks/useAuth";
import { HelpTip, Field, ReadOnlyField, INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";

/**
 * Code Mapping: incoming source codes -> destination OMOP concepts.
 *
 * The direction never reverses. A source code is something that arrived - a
 * LOINC or ICD code from a FHIR bundle, a lab's in-house test name off a PDF, a
 * phrase from a note. The destination is the OMOP concept it means, either an
 * existing Athena concept or one minted locally under an HK-* vocabulary.
 *
 * Tabs are **source vocabularies** (ICD-10-CM, CPT4, RxNorm, etc.) — what
 * arrived, not where it landed. Each tab has three sections:
 *   - ATHENA MAPPED: existing Athena-provided Maps-to relationships (read-only)
 *   - UNMAPPED: proposed/rejected rows awaiting curation (editable)
 *   - MAPPED: approved HealthKey-curated rows (editable, collapsible)
 *
 * The dialog reads top to bottom in the direction of the mapping: a SOURCE
 * block then a DESTINATION block. Every control is labelled and carries a
 * tooltip - the screen is dense enough that a field whose meaning has to be
 * inferred is a defect.
 */

interface CodeMappingRow {
  mapping_id: number | null;
  domain_id?: string;
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  source_concept_id?: number | null;
  destination_concept_id: number;
  destination_concept_name: string;
  destination_concept_code: string;
  destination_vocabulary_id: string;
  destination_concept_class_id: string;
  destination_omop_table: string;
  destination_domain_id?: string;
  standard_concept?: string | null;
  status: "proposed" | "approved" | "rejected" | "unmapped";
  notes: string;
  origin: string;
  origin_system: string;
  created_by: string;
  // Who signed the mapping off, and when. Distinct from created_by: approval
  // is the transition that rewrites stored patient data, and it survives every
  // later edit of the row.
  reviewer?: string;
  reviewed_at?: string | null;
  occurrence_count: number;
  has_mapping: boolean;
  mapping_origin?: "athena" | "healthkey";
}

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
  measurement_type?: "qualitative" | "quantitative";
  suggested_unit?: string;
}

interface VocabularyRef {
  vocabulary_id: string;
  vocabulary_name: string;
  is_local?: boolean;
}

interface DomainRef {
  domain_id: string;
  label: string;
}

interface SourceCodeSystemRef {
  vocabulary_id: string;
  label: string;
}

interface SourceVocabularyTab {
  vocabulary_id: string;
  label: string;
  is_standard: boolean;
}

interface Reference {
  domains: DomainRef[];
  source_code_systems_by_domain: Record<string, SourceCodeSystemRef[]>;
  destination_vocabularies: VocabularyRef[];
  omop_tables: Record<string, string>;
  source_vocabulary_tabs?: SourceVocabularyTab[];
}

interface RepointResult {
  rows_updated: number;
  persons_marked_stale: number;
  rows_collapsed: number;
}

interface MappingForm {
  domain_id: string;
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  source_concept_id: string;
  destination_concept_id: string;
  destination_concept_name: string;
  destination_concept_code: string;
  destination_vocabulary_id: string;
  destination_concept_class_id: string;
  standard_concept: string;
  omop_table: string;
  status: "proposed" | "approved" | "rejected";
  notes: string;
}

const emptyForm: MappingForm = {
  domain_id: "",
  source_vocabulary_id: "",
  source_code: "",
  source_code_description: "",
  source_concept_id: "",
  destination_concept_id: "",
  destination_concept_name: "",
  destination_concept_code: "",
  destination_vocabulary_id: "",
  destination_concept_class_id: "",
  standard_concept: "",
  omop_table: "",
  status: "proposed",
  notes: "",
};

const emptyReference: Reference = {
  domains: [],
  source_code_systems_by_domain: {},
  destination_vocabularies: [],
  omop_tables: {},
};

const statusClass: Record<string, string> = {
  proposed: "bg-amber-100 text-amber-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
  unmapped: "bg-amber-100 text-amber-800",
};

/**
 * Which tab a row belongs to — keyed by source vocabulary.
 * Blank source_vocabulary_id ("") means uncoded/free text.
 * Apple and Garmin rows are consolidated under the Wearables tab.
 * FHIR OID URIs are merged into their canonical OMOP vocabulary.
 */
const VOCABULARY_ALIASES: Record<string, string> = {
  Apple: "OpenWearables",
  Garmin: "OpenWearables",
  "urn:oid:2.16.840.1.113883.6.96": "SNOMED",
};
function tabForRow(row: CodeMappingRow): string {
  return VOCABULARY_ALIASES[row.source_vocabulary_id] ?? row.source_vocabulary_id;
}

/**
 * OMOP domain -> the clinical table its facts land in. Only a fallback: the
 * reference endpoint is authoritative, and hardcoding the mapping in the
 * frontend is what this table exists to avoid. It covers the window before
 * the first fetch resolves.
 */
const DOMAIN_TO_TABLE: Record<string, string> = {
  Condition: "condition",
  Drug: "drug_exposure",
  Measurement: "measurement",
  Observation: "observation",
  Procedure: "procedure",
};

/** Tooltip copy, verbatim from the design (plan section 3.1). */
const TIP = {
  domain:
    "What kind of fact this is. Chosen first: it decides which code systems are offered and which OMOP table the fact lands in.",
  source_code_system:
    "The external code system the value arrived in — NDC or ATC for drugs, ICD-10-CM or SNOMED for conditions. Leave as None for uncoded data; a parsed paper lab or a phrase from a note has no code system, which is normal.",
  source_code_value:
    "Exactly what appears in the source data — the code if there is one, otherwise the raw text.",
  source_description:
    "Human-readable description of the source code, where the source supplies one.",
  source_concept_id:
    "The OMOP concept for the source code itself, if that vocabulary is loaded. Blank is normal — most source systems are ones we receive codes in without holding their concepts.",
  destination_concept_id:
    "The OMOP concept this source code means. Type an id directly or pick one from the search above.",
  destination_concept_name:
    "Name of the destination concept. Editable only for a HealthKey-minted concept; Athena concepts are named by Athena.",
  destination_concept_code:
    "The destination concept's own code in its vocabulary, e.g. 33358-3.",
  destination_vocabulary_id:
    "Vocabulary the destination concept belongs to — SNOMED, LOINC, or an HK-* vocabulary when we minted it.",
  destination_concept_class:
    "The concept's class within its vocabulary, e.g. Clinical Finding, Lab Test.",
  standard_concept:
    "'S' means a standard Athena concept. Blank means a HealthKey-minted concept in a quarantined HK-* vocabulary.",
  destination_table:
    "The OMOP clinical table the fact is stored in. Follows from Domain.",
  search:
    "Search OMOP concepts by name or code. Suggest seeds the search from the source description.",
  search_vocabulary:
    "Which vocabulary the search looks in. Defaults to the destination's own vocabulary; widen it to re-point a minted HK-* mapping at a standard concept.",
  status:
    "Proposed is awaiting review. Approving also re-points the clinical rows already stored. Rejected hides the row behind a filter. Only org admins and staff can approve mappings — doctors and analysts may propose mappings for review.",
  status_new:
    "A new mapping always starts as Proposed. Only org admins and staff can approve it once reviewed — approval is what rewrites the clinical rows already stored.",
  notes: "Why this decision was made, for the next curator who opens the row.",
} as const;

function omopTableFor(reference: Reference, domainId: string): string {
  if (!domainId) return "";
  return reference.omop_tables[domainId] || DOMAIN_TO_TABLE[domainId] || "";
}

/** Highest occurrence count first: the code seen 400 times is worth more of a curator's time. */
const byOccurrence = (a: CodeMappingRow, b: CodeMappingRow) =>
  (b.occurrence_count || 0) - (a.occurrence_count || 0)
  || (a.source_code || "").localeCompare(b.source_code || "");

/** Primary sort by origin_system (provenance), then by occurrence count. */
const byProvenanceThenOccurrence = (a: CodeMappingRow, b: CodeMappingRow) =>
  (a.origin_system || "").localeCompare(b.origin_system || "")
  || byOccurrence(a, b);

/**
 * Provenance first, then machines before humans, then author alphabetically.
 *
 * An import's proposal is nobody's decision yet — it is the work the queue
 * exists for, so it sorts above every hand-written mapping. Human drafts then
 * group by author, which keeps one curator's in-progress work together
 * instead of interleaving it with everyone else's by occurrence count.
 */
const byProvenanceThenAuthor = (a: CodeMappingRow, b: CodeMappingRow) => {
  const machine = (r: CodeMappingRow) => (r.origin === "import" ? 0 : 1);
  return (a.origin_system || "").localeCompare(b.origin_system || "")
    || machine(a) - machine(b)
    || (a.created_by || "").localeCompare(b.created_by || "")
    || byOccurrence(a, b);
};

/**
 * The sign-off half of the provenance line: " · approved by ada@x on 2026-08-31".
 *
 * Empty until a mapping has actually been approved. Rows approved before the
 * reviewer was recorded carry neither field, and saying nothing is honest
 * where naming whoever last edited the row would not be — that is exactly the
 * confusion updated_by created (#848).
 *
 * The date is sliced off the ISO timestamp rather than formatted locally: the
 * day a decision was made is what matters, and the server sends UTC.
 */
function approvalNote(row: CodeMappingRow): string {
  // Only on an approved row. The stamp is cleared server-side when a mapping is
  // un-approved, but a client holding an older payload must not assert
  // "approved by X" over something that is no longer approved.
  if (row.status !== "approved") return "";
  if (!row.reviewer && !row.reviewed_at) return "";
  const who = row.reviewer ? ` by ${row.reviewer}` : "";
  // Rendered in the viewer's own timezone. Slicing the UTC string showed a
  // curator at UTC-7 approving at 17:00 the following day's date.
  const when = row.reviewed_at
    ? ` on ${new Date(row.reviewed_at).toLocaleDateString()}`
    : "";
  return ` · approved${who}${when}`;
}

function buildEditForm(row: CodeMappingRow, reference: Reference): MappingForm {
  const domainId = row.domain_id || row.destination_domain_id || "";
  return {
    domain_id: domainId,
    // No fallback to the destination vocabulary. A mapping with no source code
    // system genuinely has none, and showing the destination's there is what
    // put HK-Wearable in the source column to begin with.
    source_vocabulary_id: row.source_vocabulary_id,
    source_code: row.source_code,
    source_code_description: row.source_code_description || "",
    source_concept_id: row.source_concept_id ? String(row.source_concept_id) : "",
    destination_concept_id: String(row.destination_concept_id),
    destination_concept_name: row.destination_concept_name,
    destination_concept_code: row.destination_concept_code || "",
    destination_vocabulary_id: row.destination_vocabulary_id,
    destination_concept_class_id: row.destination_concept_class_id || "",
    standard_concept: row.standard_concept || "",
    omop_table: row.destination_omop_table || omopTableFor(reference, domainId),
    status: row.status === "unmapped" ? "proposed" : row.status,
    notes: row.notes || "",
  };
}

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  const canApprove = !!(currentUser?.is_staff || currentUser?.is_org_admin);
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  // `null` means no choice has been made, so use the work-prioritized default.
  // The empty string is a real vocabulary ID: it represents the Uncoded tab.
  const [activeVocabulary, setActiveVocabulary] = useState<string | null>(null);
  const [unmappedCollapsed, setUnmappedCollapsed] = useState(false);
  const [mappedCollapsed, setMappedCollapsed] = useState(true);
  const [athenaCollapsed, setAthenaCollapsed] = useState(true);
  const [showRejected, setShowRejected] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  // "" while the field is mid-edit; coerced when sent. Coercing on every
  // keystroke snapped the box to 1 the moment a curator cleared it.
  const [minOccurrences, setMinOccurrences] = useState<number | "">(10);
  const [strategies, setStrategies] = useState({
    umls: true, vectors: true, lexical: true,
  });
  const [dialogMode, setDialogMode] = useState<"new" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [form, setForm] = useState<MappingForm>(emptyForm);
  const [searchVocabulary, setSearchVocabulary] = useState("");
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
      setReference({ ...emptyReference, ...(refResp.data || {}) });
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
    const sourceVocabTabs = reference.source_vocabulary_tabs || [];
    const counts: Record<string, { proposed: number; approved: number; athena: number }> = {};
    sourceVocabTabs.forEach((v) => {
      counts[v.vocabulary_id] = { proposed: 0, approved: 0, athena: 0 };
    });
    rows.forEach((row) => {
      const key = tabForRow(row);
      if (!counts[key]) counts[key] = { proposed: 0, approved: 0, athena: 0 };
      if (row.mapping_origin === "athena") counts[key].athena += 1;
      else if (row.status === "approved") counts[key].approved += 1;
      else if (row.status === "proposed") counts[key].proposed += 1;
    });
    // Use server-provided tab order. Add any data-only tabs not in the list.
    const result = sourceVocabTabs.map((v) => ({
      ...v,
      ...(counts[v.vocabulary_id] || { proposed: 0, approved: 0, athena: 0 }),
    }));
    // Data-only tabs (not in server list) only appear when they have
    // proposed mappings needing curation — fully-mapped vocabularies
    // (e.g. ATC, HemOnc, RxNorm Extension with only Athena rows) stay hidden.
    const known = new Set(sourceVocabTabs.map((v) => v.vocabulary_id));
    Object.keys(counts)
      .filter((k) => !known.has(k) && counts[k].proposed > 0)
      .forEach((k) => {
        result.push({
          vocabulary_id: k,
          label: k || "Uncoded",
          is_standard: false,
          ...counts[k],
        });
      });
    return result;
  }, [rows, reference]);

  // Land on work, not on the alphabetically-first tab.
  const defaultVocabulary = useMemo(() => {
    const withWork = vocabularyTabs.find((t) => t.proposed > 0);
    if (withWork) return withWork.vocabulary_id;
    const withAny = vocabularyTabs.find((t) => t.proposed + t.approved + t.athena > 0);
    if (withAny) return withAny.vocabulary_id;
    return vocabularyTabs[0]?.vocabulary_id ?? "";
  }, [vocabularyTabs]);

  const selectedVocabulary = activeVocabulary ?? defaultVocabulary;

  const visibleRows = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((row) => {
      // Rejected rows are hidden but reachable. Filtering them out with no way
      // back would strand the source code for good: it appears in neither
      // section, cannot be re-opened to un-reject, and re-creating it trips the
      // (source_vocabulary_id, source_code) unique constraint.
      if (row.status === "rejected" && !showRejected) return false;
      // A query is an intentional escape hatch from the current tab: a
      // curator should not have to try every code system to find an incoming
      // code. With no query, retain the focused, one-vocabulary-at-a-time
      // review queue.
      if (!q && tabForRow(row) !== selectedVocabulary) return false;
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

  // Three-section layout: UNMAPPED / MAPPED / ATHENA MAPPED.
  const athenaRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin === "athena").sort(byProvenanceThenOccurrence),
    [visibleRows],
  );
  const unmappedRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin !== "athena" && r.status !== "approved").sort(byProvenanceThenAuthor),
    [visibleRows],
  );
  const rejectedCount = useMemo(
    () => rows.filter((r) => r.status === "rejected"
      && r.mapping_origin !== "athena"
      && tabForRow(r) === selectedVocabulary).length,
    [rows, selectedVocabulary],
  );
  const mappedRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin !== "athena" && r.status === "approved").sort(byProvenanceThenOccurrence),
    [visibleRows],
  );

  /** Source code systems offered for the chosen domain, blank option first. */
  const sourceCodeSystems = useMemo(() => {
    const offered = reference.source_code_systems_by_domain[form.domain_id] || [];
    const withBlank = offered.some((s) => s.vocabulary_id === "")
      ? offered
      : [{ vocabulary_id: "", label: "None — uncoded / free text" }, ...offered];
    // A stored system the domain's catalogue does not list still has to render
    // as itself. Without this the select falls back to its first option and an
    // ICD-10-CM-coded row minted into HK-Labs displays as "uncoded" — the same
    // "the source column shows the wrong thing" defect this page exists to fix.
    const current = form.source_vocabulary_id;
    if (current && !withBlank.some((s) => s.vocabulary_id === current)) {
      return [...withBlank, { vocabulary_id: current, label: `${current} — not typical for this domain` }];
    }
    return withBlank;
  }, [reference, form.domain_id, form.source_vocabulary_id]);

  const openNewDialog = () => {
    setSelectedRow(null);
    setForm({ ...emptyForm });
    setSearchVocabulary("");
    setConceptSearchQuery("");
    setConceptResults([]);
    setRepointResult(null);
    setDialogMode("new");
  };

  const openEditDialog = (row: CodeMappingRow) => {
    setSelectedRow(row);
    setForm(buildEditForm(row, reference));
    setSearchVocabulary(row.destination_vocabulary_id || "");
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

  /**
   * Domain is the first choice and it settles two others: which source code
   * systems are plausible, and which OMOP table the fact lands in.
   */
  const setDomain = (domainId: string) => {
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
  };

  /** Apply a concept to the form: id, name, code, vocabulary, class, standard flag. */
  const applyConcept = (concept: ConceptResult) => {
    setForm((prev) => {
      // A concept only supplies the domain when the curator has not chosen one;
      // Domain is theirs, and the table follows from it, not from the concept.
      const domainId = prev.domain_id || concept.domain_id || "";
      return {
        ...prev,
        domain_id: domainId,
        destination_concept_id: String(concept.concept_id),
        destination_concept_name: concept.concept_name,
        destination_concept_code: concept.concept_code || "",
        destination_vocabulary_id: concept.vocabulary_id,
        destination_concept_class_id: concept.concept_class_id || "",
        standard_concept: concept.standard_concept || "",
        omop_table: prev.omop_table || omopTableFor(reference, domainId),
      };
    });
  };

  /**
   * Resolve a hand-typed concept id. Everything below the id - name, code,
   * vocabulary, class, standard flag - follows from the concept, so it has to
   * be fetched whenever the id changes by hand.
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
      setForm((prev) => ({
        ...prev,
        destination_concept_code: "",
        destination_concept_class_id: "",
        standard_concept: "",
      }));
    }
  };

  const searchConcepts = async (query: string, vocabulary = searchVocabulary) => {
    // Keep the raw value in state and trim only for the request. Trimming
    // before setState meant typing a space produced the same string back, React
    // re-rendered without it, and a multi-word search could never be typed.
    setConceptSearchQuery(query);
    const q = query.trim();
    if (q.length < 3) {
      setConceptResults([]);
      return;
    }
    setSearchingConcepts(true);
    try {
      const params: Record<string, string> = { q, limit: "25" };
      // Scope to the destination vocabulary so a curator after a LOINC code is
      // not wading through a million SNOMED hits.
      if (vocabulary) params.vocabulary_id = vocabulary;
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

  // Keyed on the same condition submitForm branches on. dialogMode can say
  // "edit" for a row with no mapping_id (toggleApproval opens one that way),
  // and the two diverging let a curator pick Approved on what the server then
  // treats as a create and silently downgrades.
  const isNewMapping = !selectedRow?.mapping_id;

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

  /**
   * Fill this tab's queue from source codes nobody has mapped.
   *
   * Only on the HK-* tabs: those hold locally minted destinations, and the
   * unmapped codes are what they are minted for. A standard vocabulary is
   * somewhere a curator re-points *into* — enumerating SNOMED's 1.09M concepts
   * would not be a queue.
   */
  const runSuggest = async () => {
    setSuggesting(true);
    setError("");
    setBanner(null);
    try {
      const activeStrategies = Object.entries(strategies)
        .filter(([, v]) => v)
        .map(([k]) => k);
      const resp = await api.post("/v1/code-mappings/suggest/", {
        source_vocabulary_id: selectedVocabulary,
        min_occurrences: Number(minOccurrences) || 1,
        strategies: activeStrategies,
      });
      const { created = 0, considered = 0, ranked = 0, truncated,
              landed_in: landed = {},
              strategy_counts: stratCounts = {} } = resp.data || {};
      await fetchAll();
      // Say which tabs the new rows are in. A ranked suggestion's destination
      // is a standard concept, so its mapping belongs to the LOINC or SNOMED
      // tab rather than the HK-* one the button is on — correct, and baffling
      // if the curator is left to discover it.
      const where = Object.entries(landed as Record<string, number>)
        .sort((a, b) => b[1] - a[1])
        .map(([vocab, n]) => `${n} in ${vocab}`)
        .join(", ");
      const byStrategy = Object.entries(stratCounts as Record<string, number>)
        .filter(([, n]) => n > 0)
        .map(([s, n]) => `${n} via ${s}`)
        .join(", ");
      setBanner(
        created
          ? `Proposed ${created} mapping(s) from ${considered} unmapped code(s), `
            + `${ranked} with a suggested destination`
            + (byStrategy ? ` (${byStrategy})` : "")
            + (where ? ` — ${where}.` : ".")
            + (truncated ? " More remain — run Suggest again." : "")
          : `No unmapped codes seen ${Number(minOccurrences) || 1}+ times in this vocabulary.`,
      );
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: Record<string, unknown> } }).response?.data
          : undefined;
      const message = detail && typeof detail === "object"
        ? Object.values(detail).map(String).join(" ")
        : "";
      setError(message || "Failed to suggest mappings.");
    } finally {
      setSuggesting(false);
    }
  };

  const toggleApproval = async (row: CodeMappingRow) => {
    if (!row.mapping_id) {
      openEditDialog(row);
      return;
    }
    setError("");
    // Approving from the table triggers the same clinical rewrite the dialog
    // does. Dropping the response left a curator with no sign that 400 rows
    // had just moved -- the table simply refetched.
    const approving = row.status !== "approved";
    if (approving) setBanner(null);
    try {
      const resp = await api.patch(`/v1/code-mappings/${row.mapping_id}/`, {
        domain_id: row.domain_id || row.destination_domain_id || "",
        source_vocabulary_id: row.source_vocabulary_id,
        source_code: row.source_code,
        source_code_description: row.source_code_description,
        destination_concept_id: row.destination_concept_id,
        destination_vocabulary_id: row.destination_vocabulary_id,
        omop_table: row.destination_omop_table,
        status: row.status === "approved" ? "proposed" : "approved",
        notes: row.notes,
      });
      const repoint: RepointResult | null = resp.data?.repoint ?? null;
      await fetchAll();
      if (repoint && repoint.rows_updated) {
        setBanner(
          `${row.source_code}: updated ${repoint.rows_updated} row(s) across `
          + `${repoint.persons_marked_stale} patient(s)`
          + (repoint.rows_collapsed ? `, ${repoint.rows_collapsed} duplicate(s) collapsed` : "")
          + ". Patient records queued for re-derivation.",
        );
      }
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

  const renderTable = (sectionRows: CodeMappingRow[], emptyText: string, { hideStatus = false }: { hideStatus?: boolean } = {}) => {
    const colCount = 5 + (hideStatus ? 0 : 2);
    return (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <table className="w-full border-collapse text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600">
          <tr>
            <th className="px-4 py-3 font-semibold">Provenance</th>
            <th className="px-4 py-3 font-semibold">Source code</th>
            <th className="px-4 py-3 font-semibold">Source description</th>
            <th className="px-4 py-3 font-semibold">Destination concept</th>
            <th className="px-4 py-3 font-semibold">Concept ID</th>
            {!hideStatus && <th className="px-4 py-3 font-semibold">Status</th>}
            {!hideStatus && <th className="w-16 px-4 py-3 font-semibold" aria-label="Actions" />}
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
              <td className="px-4 py-3 text-xs text-slate-700">{row.origin_system || "—"}</td>
              <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.source_code}</td>
              <td className="px-4 py-3 text-xs text-slate-700">{row.source_code_description || "—"}</td>
              <td className="px-4 py-3">
                <div className="font-medium text-slate-950">{row.destination_concept_name}</div>
                <div className="font-mono text-xs text-slate-500">
                  {row.destination_vocabulary_id}:{row.destination_concept_code}
                </div>
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.destination_concept_id}</td>
              {!hideStatus && (
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
              )}
              {!hideStatus && (
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
              )}
            </tr>
          ))}
          {sectionRows.length === 0 && (
            <tr>
              <td colSpan={colCount} className="px-4 py-8 text-center text-sm text-slate-500">{emptyText}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
    );
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

        {banner && (
          <div
            role="status"
            className="mb-4 rounded-md border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-900"
          >
            {banner}
          </div>
        )}

        <div className="mb-4">
          <label className="relative block">
            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
            <input
              aria-label="Search mappings"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search source codes, destination concepts, or OMOP IDs"
              className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
            />
          </label>
        </div>

        <div
          role="tablist"
          aria-label="Source vocabularies"
          className="mb-4 flex gap-2 overflow-x-auto border-b border-slate-200"
        >
          {vocabularyTabs.map((tab) => {
            const selected = tab.vocabulary_id === selectedVocabulary;
            return (
              <button
                key={tab.vocabulary_id || "__uncoded__"}
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => setActiveVocabulary(tab.vocabulary_id)}
                title={`Source vocabulary: ${tab.label}`}
                className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${
                  selected
                    ? "border-slate-950 text-slate-950"
                    : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-950"
                } ${tab.is_standard ? "italic" : ""}`}
              >
                {tab.label}
                {tab.proposed > 0 && (
                  <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                    {tab.proposed}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* At the top of the tab, not buried in a section header: this is how
            an empty queue gets filled, so it has to be visible before there is
            anything to scroll past. Shown on every tab and disabled on the
            standard ones — a button that silently vanishes reads as a bug. */}
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <label className="text-xs text-slate-600" htmlFor="min_occurrences">
            Suggest mappings for codes seen at least
          </label>
          <input
            id="min_occurrences"
            type="number"
            min={1}
            value={minOccurrences}
            onChange={(e) => setMinOccurrences(e.target.value === "" ? "" : Number(e.target.value))}
            title="How often a code must appear before it is worth a curator's time. 43% of unmapped codes are seen exactly once."
            className="h-8 w-16 rounded-md border border-slate-300 px-2 text-xs"
          />
          <span className="text-xs text-slate-600">times</span>
          {(["umls", "vectors", "lexical"] as const).map((key) => (
            <label key={key} className="inline-flex items-center gap-1 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={strategies[key]}
                onChange={(e) =>
                  setStrategies((prev) => ({ ...prev, [key]: e.target.checked }))
                }
                className="h-3.5 w-3.5 rounded border-slate-300"
              />
              {key === "umls" ? "UMLS" : key === "vectors" ? "Vectors" : "Lexical"}
            </label>
          ))}
          <button
            type="button"
            onClick={() => void runSuggest()}
            disabled={suggesting || !Object.values(strategies).some(Boolean)}
            title="Propose mappings for unmapped source codes in this vocabulary."
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles size={13} />
            {suggesting ? "Suggesting…" : "Suggest"}
          </button>
        </div>

        <section className="mb-6">
          <button
            type="button"
            onClick={() => setUnmappedCollapsed((v) => !v)}
            className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
          >
            {unmappedCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
            Unmapped <span className="font-normal text-slate-500">({unmappedRows.length})</span>
          </button>
          {!unmappedCollapsed && (
            <>
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
            </>
          )}
        </section>

        <section className="mb-6">
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

        {athenaRows.length > 0 && (
          <section>
            <button
              type="button"
              onClick={() => setAthenaCollapsed((v) => !v)}
              className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
            >
              {athenaCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              Athena Mapped <span className="font-normal text-slate-500">({athenaRows.length})</span>
            </button>
            {!athenaCollapsed && renderTable(athenaRows, "No Athena mappings in this vocabulary.", { hideStatus: true })}
          </section>
        )}
      </div>

      {dialogMode && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
          <form
            onSubmit={submitForm}
            role="dialog"
            aria-label={dialogMode === "new" ? "New Mapping" : "Edit Mapping"}
            className="w-full max-w-3xl rounded-md bg-white shadow-xl"
          >
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
              {/* ── SOURCE ───────────────────────────────────────────────── */}
              <fieldset
                data-testid="source-block"
                className="mb-5 rounded-md border border-slate-200 p-4"
              >
                <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Source — the code as it arrived
                </legend>
                <div data-testid="source-fields" className="grid gap-4 md:grid-cols-2">
                  {/* Domain is first on purpose: it decides which code systems
                      are offered and which OMOP table the fact lands in. */}
                  <Field id="domain_id" label="Domain" tip={TIP.domain}>
                    <select
                      id="domain_id"
                      title={TIP.domain}
                      value={form.domain_id}
                      onChange={(e) => setDomain(e.target.value)}
                      required
                      className={INPUT_CLASS}
                    >
                      <option value="">— select —</option>
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
                      {/* Blank is a real answer: a paper lab or a note has no code system. */}
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

                  <ReadOnlyField
                    id="source_concept_id"
                    label="Source Concept ID"
                    tip={TIP.source_concept_id}
                    value={form.source_concept_id}
                    testId="source-concept-id"
                  />
                </div>
              </fieldset>

              {/* ── DESTINATION ──────────────────────────────────────────── */}
              <fieldset
                data-testid="destination-block"
                className="rounded-md border border-slate-200 p-4"
              >
                <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Destination — the OMOP concept it means
                </legend>

                {/* Search sits at the top: picking a concept fills everything below it. */}
                <div className="mb-4">
                  <div className="mb-2 flex items-end justify-between gap-3">
                    <div className="flex items-center gap-1">
                      <label className="text-sm font-medium text-slate-700" htmlFor="code-mapping-concept-search">
                        Search destination concepts
                      </label>
                      <HelpTip tip={TIP.search} />
                    </div>
                    <div className="flex items-center gap-3">
                      <div className="flex items-center gap-1">
                        <label className="text-sm font-medium text-slate-700" htmlFor="code-mapping-search-vocabulary">
                          Search vocabulary
                        </label>
                        <HelpTip tip={TIP.search_vocabulary} />
                      </div>
                      <button
                        type="button"
                        onClick={suggestCurrentCode}
                        className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
                      >
                        <Sparkles size={13} />
                        Suggest
                      </button>
                    </div>
                  </div>
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
                    {/* Destination Vocabulary ID itself is read-only - it is a
                        property of the resolved concept. The search still needs
                        a scope a curator can widen, or re-pointing a minted
                        HK-* mapping at a standard concept would be impossible,
                        which is the whole point of the queue. */}
                    <select
                      id="code-mapping-search-vocabulary"
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
                        className="w-full border-b border-slate-100 px-3 py-2 text-left text-xs last:border-0 hover:bg-slate-50"
                      >
                        <span className="grid grid-cols-[8rem_1fr_6rem] gap-2">
                          <span className="font-mono text-slate-700">{concept.concept_code}</span>
                          <span className="text-slate-900">{concept.concept_name}</span>
                          <span className="font-mono text-slate-500">{concept.vocabulary_id}</span>
                        </span>
                        {concept.measurement_type && (
                          <span className="mt-1 block text-slate-500">
                            {concept.measurement_type === "quantitative" ? "Quantitative" : "Qualitative"}
                            {concept.suggested_unit && ` · Unit: ${concept.suggested_unit}`}
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                </div>

                {/* The order a curator checks them in: the id, its name, then
                    the four facts that follow from it, then the table. */}
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

                  {/* Read-only: the API has no write path for a concept name, so an
                      editable box accepted a rename, saved, and let the old name
                      come back on refetch with no error. Renaming a
                      HealthKey-minted concept is real curation, but it needs a
                      write path first. */}
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
                    id="omop_table"
                    label="Destination Table"
                    tip={TIP.destination_table}
                    value={form.omop_table}
                    testId="destination-table"
                  />
                </div>
              </fieldset>

              {selectedRow
                && (selectedRow.origin === "import"
                  || selectedRow.created_by
                  || approvalNote(selectedRow)) && (
                <p className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">
                  {selectedRow.origin === "import" ? (
                    <>
                      Proposed by import
                      {selectedRow.origin_system ? ` (${selectedRow.origin_system})` : ""}
                    </>
                  ) : (
                    // created_by is SET_NULL, so a deleted author serializes
                    // blank -- rendering "Created by " with nothing after it.
                    selectedRow.created_by ? <>Created by {selectedRow.created_by}</> : null
                  )}
                  {/* Both halves of the provenance: who raised it, and who
                      signed it off. Approval is the only transition that
                      rewrites patient data, so a reviewer looking at an
                      approved mapping needs to see whose decision it was. */}
                  {approvalNote(selectedRow)}
                  {selectedRow.occurrence_count ? ` · seen ${selectedRow.occurrence_count} time(s)` : ""}
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
                <div className="flex items-center gap-2">
                  <label className="text-sm font-medium text-slate-700" htmlFor="status">Status</label>
                  <HelpTip tip={TIP.status} />
                  {/* A new mapping is always proposed; the server enforces it.
                      Offering Approved here would promise a one-step create-and-
                      approve the API no longer honours, and approval is the only
                      transition that rewrites patient data. */}
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
