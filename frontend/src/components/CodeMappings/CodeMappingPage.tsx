import CanonicalUnitEditor from "./CanonicalUnitEditor";
import PageTitle from '@/components/Branding/PageTitle';
import IndividualSuggestCandidates from "./IndividualSuggestCandidates";
import InlineDestinationPicker from "./InlineDestinationPicker";
import SourceVocabularyLookup from "./SourceVocabularyLookup";
import { searchDestinationConcepts } from "./destinationSearch";
import SuggestCandidates, { type CandidateActivity } from "./SuggestCandidates";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Download, Pencil, Plus, Search, Sparkles, Trash2, X } from "lucide-react";
import api from "@/api/axios";
import MintConceptDialog from "./MintConceptDialog";
import ConceptInputDetails from "@/components/UI/ConceptInputDetails";
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
  source_retired?: boolean | null;
  source_retirement_evidence?: string[];
  umls_source_name?: string;
  destination_concept_id: number;
  destination_concept_name: string;
  destination_concept_code: string;
  destination_vocabulary_id: string;
  destination_concept_class_id: string;
  destination_omop_table: string;
  destination_domain_id?: string;
  standard_concept?: string | null;
  destination_invalid_reason?: string | null;
  status: "proposed" | "approved" | "rejected" | "unmapped";
  notes: string;
  origin: string;
  origin_system: string;
  suggest_strategy: string;
  umls_cui: string;
  created_by: string;
  // Who signed the mapping off, and when. Distinct from created_by: approval
  // is the transition that rewrites stored patient data, and it survives every
  // later edit of the row.
  reviewer?: string;
  reviewed_at?: string | null;
  occurrence_count: number;
  destination_count: number;
  has_mapping: boolean;
  mapping_origin?: "athena" | "healthkey";
  measurement_type?: "qualitative" | "quantitative";
  suggested_unit?: string;
  example_units?: string[];
  locked_by_username?: string | null;
  locked_at?: string | null;
}

const EXPORT_COLUMNS: (keyof CodeMappingRow)[] = [
  "source_vocabulary_id", "source_code", "source_code_description",
  "occurrence_count", "origin_system", "destination_concept_id",
  "destination_concept_name", "destination_concept_code",
  "destination_vocabulary_id", "destination_domain_id", "status",
  "reviewer", "reviewed_at", "notes",
];

function downloadFile(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function escapeCsvField(value: unknown): string {
  const s = value == null ? "" : String(value);
  if (s.includes(",") || s.includes('"') || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

function downloadMappings(rows: CodeMappingRow[], section: string, format: "csv" | "json") {
  const timestamp = new Date().toISOString().slice(0, 10);
  const safeName = section.toLowerCase().replace(/\s+/g, "-");
  if (format === "json") {
    const data = rows.map((row) => {
      const obj: Record<string, unknown> = {};
      for (const col of EXPORT_COLUMNS) obj[col] = row[col] ?? null;
      return obj;
    });
    downloadFile(JSON.stringify(data, null, 2), `code-mappings-${safeName}-${timestamp}.json`, "application/json");
  } else {
    const header = EXPORT_COLUMNS.join(",");
    const lines = rows.map((row) => EXPORT_COLUMNS.map((col) => escapeCsvField(row[col])).join(","));
    downloadFile([header, ...lines].join("\n"), `code-mappings-${safeName}-${timestamp}.csv`, "text/csv");
  }
}

function DownloadMenu({ rows, section }: { rows: CodeMappingRow[]; section: string }) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;
  return (
    <span className="relative ml-2 inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        className="inline-flex items-center gap-1 text-xs font-normal normal-case tracking-normal text-slate-500 hover:text-slate-700"
        title={`Download ${section}`}
      >
        <Download size={12} /> Download
      </button>
      {open && (
        <span className="absolute left-0 top-full z-10 mt-1 flex flex-col rounded border border-slate-200 bg-white shadow-md">
          <button type="button" className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-slate-50"
            onClick={(e) => { e.stopPropagation(); downloadMappings(rows, section, "csv"); setOpen(false); }}>
            CSV
          </button>
          <button type="button" className="whitespace-nowrap px-3 py-1.5 text-left text-xs hover:bg-slate-50"
            onClick={(e) => { e.stopPropagation(); downloadMappings(rows, section, "json"); setOpen(false); }}>
            JSON
          </button>
        </span>
      )}
    </span>
  );
}

interface ConceptResult {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
  invalid_reason?: string | null;
  measurement_type?: "qualitative" | "quantitative";
  suggested_unit?: string;
  example_units?: string[];
}

interface SearchScope {
  retired: boolean;
  nonStandard: boolean;
}

const DEFAULT_SEARCH_SCOPE: SearchScope = { retired: false, nonStandard: false };

interface DestinationOption extends Omit<ConceptResult, "concept_id"> {
  concept_id: number | null;
  selectable: boolean;
  origins: string[];
  selected: boolean;
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
  suggest_max_per_run?: number;
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

interface SuggestionAccuracy {
  latest_reviewed?: SuggestionAccuracy | null;
  all_models?: SuggestionAccuracy & { model_versions: number };
  review_totals?: { approved: number; rejected: number; overridden: number };
  model_version?: string | null;
  accepted: number;
  approved: number;
  overridden: number;
  rejected: number;
  reviewed: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}

interface AccuracyResponse {
  overall: SuggestionAccuracy;
  by_source_vocabulary: Record<string, SuggestionAccuracy>;
  suggest_model_version?: string;
}

const OVERALL_TAB = "__overall__";
const metric = (value: number | null) => value === null ? "—" : `${(value * 100).toFixed(1)}%`;

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
  destination_invalid_reason: string;
  omop_table: string;
  measurement_type: string;
  suggested_unit: string;
  example_units: string[];
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
  destination_invalid_reason: "",
  omop_table: "",
  measurement_type: "",
  suggested_unit: "",
  example_units: [],
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

const strategyLabel: Record<string, string> = {
  umls: "UMLS",
  vectors: "Vectors",
  lexical: "Lexical",
  semantic: "Vectors",  // legacy alias
};

/** How far along a run is, counting the phase it is actually in.

 Retrieval is two thirds of the wall clock and finishes for every code before
 the first destination is written, so counting only writes would leave the bar
 at zero for most of the wait. Once writing starts the count switches to `done`
 rather than taking the max: retrieval is pinned at the total by then, and a bar
 sitting at 100% beside a label reading "Writing suggestions… 2 of 50" is the
 two halves of one strip contradicting each other. */
const suggestProgressCount = (run: SuggestRunProgress) => {
  if (run.state === "success") return run.total;
  return run.done > 0 ? run.done : run.retrieved;
};

const describeSuggestRun = (run: SuggestRunProgress) => {
  if (run.state === "failure") return run.error || "The suggest run failed.";
  if (run.state === "success") {
    if (run.total === 0) return "Done — nothing on this tab was awaiting a suggestion.";
    return `Done — wrote ${run.destinations} new destination(s) across ${run.total} code(s).`
      // A run is capped well below a tab's backlog, so without this the curator
      // cannot tell from the page that another run is warranted.
      + (run.remaining ? ` ${run.remaining} still awaiting a suggestion — run Suggest again.` : "");
  }
  if (run.total === 0) return "Nothing queued on this tab.";
  // The destination count is what the run is for, so it is shown while the run
  // is still going rather than only at the end.
  if (run.done > 0) {
    return `Writing suggestions… ${run.done} of ${run.total}`
      + ` · ${run.destinations} destination(s)`;
  }
  if (run.retrieved > 0) return `Searching for candidates… ${run.retrieved} of ${run.total}`;
  return "Starting…";
};

const SUGGEST_POLL_INTERVAL_MS = 1000;
// Long enough that a word typed at normal speed is one request, short enough
// that the pause is not felt once the curator stops.
const CONCEPT_SEARCH_DEBOUNCE_MS = 250;
// Consecutive, not cumulative: a run lasting minutes may lose the odd poll.
const SUGGEST_POLL_MAX_FAILURES = 5;
// Allow the worker's default 15-minute limit plus time waiting in the queue.
const SUGGEST_POLL_TIMEOUT_MS = 20 * 60 * 1000;

/** Progress of one queued Suggest run, as /suggest-runs/<id>/ reports it. */
type SuggestRunProgress = {
  activity?: CandidateActivity[];
  run_id: string;
  state: "queued" | "running" | "success" | "failure";
  total: number;
  retrieved: number;
  done: number;
  /** New destinations written — what the run achieved, and the headline number. */
  destinations: number;
  /** Codes still awaiting a suggestion on this tab once the run finished. */
  remaining: number;
  strategy_counts: Record<string, number>;
  landed_in: Record<string, number>;
  error: string;
  ranking_model?: string;
};

/** Pipeline order, which is also the order the checkboxes read in. */
const STRATEGY_LABELS = {
  umls: "UMLS",
  lexical: "Lexical",
  vectors: "Vectors",
} as const;

/**
 * Which tab a row belongs to — keyed by source vocabulary.
 * Blank source_vocabulary_id ("") means uncoded/free text.
 * Apple and Garmin rows are consolidated under the Wearables tab.
 * ICD10CM rows are merged into the ICD-10 tab (#1028).
 * FHIR OID URIs are merged into their canonical OMOP vocabulary.
 */
const VOCABULARY_ALIASES: Record<string, string> = {
  Apple: "OpenWearables",
  Garmin: "OpenWearables",
  ICD10CM: "ICD10",
  "urn:oid:2.16.840.1.113883.6.96": "SNOMED",
};
function tabForRow(row: CodeMappingRow): string {
  return VOCABULARY_ALIASES[row.source_vocabulary_id] ?? row.source_vocabulary_id;
}

/**
 * Which vocabulary a row's source code is unique within. Not the tab: Apple,
 * Garmin and OpenWearables share the Wearables tab but are separate
 * vocabularies, so one code in two of them is two mappings, not a duplicate.
 * Only an OID spelling names the same vocabulary.
 */
const SAME_VOCABULARY_SPELLINGS: Record<string, string> = {
  "urn:oid:2.16.840.1.113883.6.96": "SNOMED",
};
function duplicateScopeForRow(row: CodeMappingRow): string {
  return SAME_VOCABULARY_SPELLINGS[row.source_vocabulary_id] ?? row.source_vocabulary_id;
}

function sectionForRow(row: CodeMappingRow): MappingSection {
  if (row.mapping_origin === "athena") return "Athena Mapped";
  if (row.status === "approved") return "Mapped";
  if (row.status === "rejected") return "Rejected";
  return "Unmapped";
}

function mappingRowId(row: CodeMappingRow): string {
  return `code-mapping-${row.mapping_id ?? encodeURIComponent(JSON.stringify([
    row.source_vocabulary_id, row.source_code, row.destination_concept_id, sectionForRow(row),
  ]))}`;
}

type MappingSection = "Unmapped" | "Mapped" | "Rejected" | "Athena Mapped";
type SortColumn = "origin_system" | "source_code" | "occurrence_count" | "source_code_description"
  | "destination_concept_name" | "destination_concept_id" | "destination_count" | "status";
type SectionSort = { column: SortColumn; descending: boolean };

function retirementLabel(row: CodeMappingRow | null): string {
  return row?.source_retired === true ? "Retired" : row?.source_retired === false ? "No" : "Unknown";
}

function retirementDetail(row: CodeMappingRow | null): string {
  return row?.source_retirement_evidence?.join("; ")
    || (row?.source_retired === false ? "No retirement indication in the loaded source metadata."
      : "No source retirement metadata available. Missing metadata does not mean the code is retired.");
}

function sortMappingRows(rows: CodeMappingRow[], sort?: SectionSort): CodeMappingRow[] {
  if (!sort) return rows;
  return [...rows].sort((a, b) => {
    const left = a[sort.column];
    const right = b[sort.column];
    // Unknown values stay last in either direction.
    if (left == null && right != null) return 1;
    if (right == null && left != null) return -1;
    const comparison = left == null ? 0 : typeof left === "number" || typeof left === "boolean"
      ? Number(left) - Number(right)
      : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" });
    return (sort.descending ? -comparison : comparison)
      || (sort.column === "origin_system"
        ? byOccurrence(a, b) || (a.mapping_id ?? 0) - (b.mapping_id ?? 0)
        : 0);
  });
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
    "'S' means a standard Athena concept. Blank means non-standard; a retired concept is called out separately.",
  destination_table:
    "The OMOP clinical table the fact is stored in. Follows from Domain.",
  search:
    "Search OMOP concepts by name or code. Suggest seeds the search from the source description.",
  search_include_retired:
    "Retired concepts are hidden because they should not be new destinations. Turn this on only to find the retired concept a mapping already points at.",
  search_include_non_standard:
    "Only standard concepts (and HealthKey's own HK-* concepts) are offered, since OMOP analytics read standard concepts. Turn this on to see non-standard ones, such as ICD-10 or source-vocabulary codes.",
  search_vocabulary:
    "Which vocabulary the search looks in. Defaults to the destination's own vocabulary; widen it to re-point a minted HK-* mapping at a standard concept.",
  status:
    "Proposed is awaiting review. Approving also re-points the clinical rows already stored. Rejected hides the row behind a filter. Only org admins and staff can approve mappings — doctors and analysts may propose mappings for review.",
  status_new:
    "A new mapping always starts as Proposed. Only org admins and staff can approve it once reviewed — approval is what rewrites the clinical rows already stored.",
  notes: "Why this decision was made, for the next curator who opens the row.",
  ranker: "Which AI model ranks the candidates. Anthropic uses Claude, Jev uses the Typesafe SystemOne API. Both runs both concurrently and picks the higher-confidence winner.",
} as const;

function omopTableFor(reference: Reference, domainId: string): string {
  if (!domainId) return "";
  return reference.omop_tables[domainId] || DOMAIN_TO_TABLE[domainId] || "";
}

/** Highest occurrence count first: the code seen 400 times is worth more of a curator's time. */
const byOccurrence = (a: CodeMappingRow, b: CodeMappingRow) =>
  (b.occurrence_count || 0) - (a.occurrence_count || 0)
  || (a.source_code || "").localeCompare(b.source_code || "");


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
    destination_invalid_reason: row.destination_invalid_reason || "",
    omop_table: row.destination_omop_table || omopTableFor(reference, domainId),
    measurement_type: row.measurement_type || "",
    suggested_unit: row.suggested_unit || "",
    example_units: row.example_units || [],
    status: row.status === "unmapped" ? "proposed" : row.status,
    notes: row.notes || "",
  };
}

type BrowseResponse = {
  results: CodeMappingRow[];
  duplicates: CodeMappingRow[];
  tabs: { vocabulary_id: string; label: string; is_standard: boolean; proposed: number; approved: number; athena: number }[];
  selected_source: string;
  pages: Record<MappingSection, { page: number; page_size: number; total: number }>;
  rejected_count: number;
};
const sectionNames: MappingSection[] = ["Unmapped", "Mapped", "Rejected", "Athena Mapped"];

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  const canApprove = !!(currentUser?.is_staff || currentUser?.is_org_admin);
  const [browse, setBrowse] = useState<BrowseResponse | null>(null);
  const [pages, setPages] = useState<Partial<Record<MappingSection, number>>>({});
  const loadSequence = useRef(0);
  const dialogRequest = useRef(0);
  const dialogChoice = useRef<number | null>(null);
  const conceptSearchTimer = useRef<number | null>(null);
  const conceptSearchAbort = useRef<AbortController | null>(null);
  const [individualSuggestion, setIndividualSuggestion] = useState<{ request: number; activity: CandidateActivity[]; running: boolean } | null>(null);
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const referenceCache = useRef<Reference | null>(null);
  const [accuracy, setAccuracy] = useState<AccuracyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [mintOpen, setMintOpen] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  // `null` means no choice has been made, so use the work-prioritized default.
  // The empty string is a real vocabulary ID: it represents the Uncoded tab.
  const [activeVocabulary, setActiveVocabulary] = useState<string | null>(null);
  const [unmappedCollapsed, setUnmappedCollapsed] = useState(false);
  const [mappedCollapsed, setMappedCollapsed] = useState(true);
  const [rejectedCollapsed, setRejectedCollapsed] = useState(true);
  const [athenaCollapsed, setAthenaCollapsed] = useState(true);
  const [sectionSorts, setSectionSorts] = useState<Partial<Record<MappingSection, SectionSort>>>({
    Unmapped: { column: "origin_system", descending: false },
  });
  const [navigationTarget, setNavigationTarget] = useState<{ id: string } | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestionLimit, setSuggestionLimit] = useState<number | "">(100);
  const maxSuggestions = reference.suggest_max_per_run || 100;
  const validSuggestionLimit = suggestionLimit !== "" && Number.isInteger(suggestionLimit)
    && suggestionLimit >= 1 && suggestionLimit <= maxSuggestions;
  const [strategies, setStrategies] = useState({
    umls: true, lexical: true, vectors: true,
  });
  const [rankingModel, setRankingModel] = useState<"anthropic" | "jev" | "both">("anthropic");
  const [inlineMappingId, setInlineMappingId] = useState<number | null>(null);
  const [dialogMode, setDialogMode] = useState<"new" | "edit" | null>(null);
  const [selectedRow, setSelectedRow] = useState<CodeMappingRow | null>(null);
  const [form, setForm] = useState<MappingForm>(emptyForm);
  const [searchVocabulary, setSearchVocabulary] = useState("");
  // What the destination search leaves out unless asked (#1465). Off by
  // default: a retired or non-standard concept is almost never the right
  // destination, and offering them is how they came to be approved.
  const [searchScope, setSearchScope] = useState<SearchScope>(DEFAULT_SEARCH_SCOPE);
  const [conceptSearchQuery, setConceptSearchQuery] = useState("");
  const [conceptResults, setConceptResults] = useState<ConceptResult[]>([]);
  const [destinationOptions, setDestinationOptions] = useState<DestinationOption[]>([]);
  const [loadingDestinations, setLoadingDestinations] = useState(false);
  const [destinationError, setDestinationError] = useState("");

  useEffect(() => {
    setDestinationOptions([]);
    setDestinationError("");
    if (dialogMode !== "edit" || !selectedRow?.mapping_id) return;
    let active = true;
    setLoadingDestinations(true);
    api.get<{ destination_options: DestinationOption[] }>(`/v1/code-mappings/${selectedRow.mapping_id}/`)
      .then(({ data }) => { if (active) setDestinationOptions(data.destination_options || []); })
      .catch(() => { if (active) setDestinationError("Could not load imported destinations. Close and reopen this mapping to retry."); })
      .finally(() => { if (active) setLoadingDestinations(false); });
    return () => { active = false; };
  }, [dialogMode, selectedRow?.mapping_id]);

  const [searchingConcepts, setSearchingConcepts] = useState(false);
  const [checkingUmls, setCheckingUmls] = useState(false);
  const [umlsCheckMessage, setUmlsCheckMessage] = useState("");
  const [suggestionMessage, setSuggestionMessage] = useState("");
  const [repointing, setRepointing] = useState<{ from: string; to: string } | null>(null);
  const [repointResult, setRepointResult] = useState<RepointResult | null>(null);
  const [replaceExisting, setReplaceExisting] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState(false);
  // Progress of the queued Suggest run, polled while it works. Null when no run
  // is in flight; `flash` marks the moment it finished so the strip can announce
  // itself before settling into the banner.
  const [suggestRun, setSuggestRun] = useState<SuggestRunProgress | null>(null);
  const [suggestFlash, setSuggestFlash] = useState(false);
  // Which run the page is still interested in. A poll compares against this so
  // a superseded run — or an unmounted page — stops rather than setting state
  // nobody is showing.
  const suggestRunRef = useRef<string | null>(null);
  const flashTimer = useRef<number | null>(null);

  useEffect(() => () => {
    dialogRequest.current += 1;
    suggestRunRef.current = null;
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { setDebouncedSearch(searchQuery); setPages({}); }, 250);
    return () => window.clearTimeout(timer);
  }, [searchQuery]);

  const fetchAll = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError("");
    const params: Record<string, string | number> = {
      browse: 1, search: debouncedSearch,
    };
    if (activeVocabulary !== null) params.source = activeVocabulary;
    sectionNames.forEach((section, index) => {
      params[`page_${index}`] = pages[section] || 1;
      const sort = sectionSorts[section];
      params[`order_${index}`] = sort ? `${sort.descending ? "-" : ""}${sort.column}` : "-occurrence_count";
    });
    try {
      const requests = await Promise.allSettled([
        api.get<BrowseResponse | CodeMappingRow[]>("/v1/code-mappings/", { params }).then(({ data }) => {
          if (sequence !== loadSequence.current) return;
          setBrowse(Array.isArray(data) ? null : data);
          setRows(Array.isArray(data) ? data : data.results);
        }),
        referenceCache.current ? Promise.resolve() : api.get<Reference>("/v1/code-mappings/reference/").then(({ data }) => {
          if (sequence !== loadSequence.current) return;
          referenceCache.current = { ...emptyReference, ...(data || {}) };
          setReference(referenceCache.current);
          setSuggestionLimit((current) => current === "" ? current : Math.min(current, data?.suggest_max_per_run || 100));
        }),
        api.get<AccuracyResponse>("/v1/code-mappings/accuracy/").then(({ data }) => {
          // Review counters can arrive before the slower table refresh.
          if (sequence === loadSequence.current) setAccuracy(data);
        }),
      ]);
      if (requests.some((request) => request.status === "rejected")) throw new Error("Refresh failed");
    } catch {
      if (sequence === loadSequence.current) setError("Failed to load code mappings.");
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, [activeVocabulary, debouncedSearch, pages, sectionSorts]);

  const refreshCurrent = useRef(fetchAll);
  useEffect(() => { refreshCurrent.current = fetchAll; }, [fetchAll]);

  useEffect(() => {
    (async () => { await fetchAll(); })();
    return () => { loadSequence.current += 1; };
  }, [fetchAll]);

  const vocabularyTabs = useMemo(() => {
    if (browse) return browse.tabs;
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
    result.push({
      vocabulary_id: OVERALL_TAB,
      label: "Overall",
      is_standard: false,
      proposed: counts ? rows.filter((r) => r.status === "proposed").length : 0,
      approved: rows.filter((r) => r.status === "approved").length,
      athena: rows.filter((r) => r.mapping_origin === "athena").length,
    });
    return result;
  }, [rows, reference, browse]);

  // Land on work, not on the alphabetically-first tab.
  const defaultVocabulary = useMemo(() => {
    const withWork = vocabularyTabs.find((t) => t.vocabulary_id !== OVERALL_TAB && t.proposed > 0);
    if (withWork) return withWork.vocabulary_id;
    const withAny = vocabularyTabs.find((t) => t.vocabulary_id !== OVERALL_TAB && t.proposed + t.approved + t.athena > 0);
    if (withAny) return withAny.vocabulary_id;
    return vocabularyTabs[0]?.vocabulary_id ?? "";
  }, [vocabularyTabs]);

  const selectedVocabulary = activeVocabulary ?? browse?.selected_source ?? defaultVocabulary;
  const overallTab = selectedVocabulary === OVERALL_TAB;
  // Keep the latest reviewed model visible while a newer model awaits reviews.
  const scopedAccuracy = overallTab
    ? accuracy?.overall
    : accuracy?.by_source_vocabulary?.[selectedVocabulary];
  const modelAccuracy = scopedAccuracy ?? accuracy?.overall;
  const selectedAccuracy = modelAccuracy?.latest_reviewed ?? modelAccuracy;
  // Match History's All models row across every vocabulary and model version.
  // Older API responses can supply global counts, but not global scores.
  const allModels = accuracy?.overall?.all_models;
  const reviewTotals = allModels ?? accuracy?.overall?.review_totals ?? accuracy?.overall;

  const suggestModelVersion = accuracy?.suggest_model_version ?? "";

  // Audit the entire tab, not just expanded/search-visible rows. A hidden
  // rejected mapping still owns its source code and can block re-creation.
  const duplicateCodes = useMemo(() => {
    const groups = new Map<string, { code: string; vocabulary: string; rows: CodeMappingRow[] }>();
    for (const row of browse?.duplicates ?? rows) {
      if (!overallTab && tabForRow(row) !== selectedVocabulary) continue;
      const vocabulary = duplicateScopeForRow(row);
      const code = row.source_code.trim().toUpperCase();
      if (!code) continue;
      // Keyed on the vocabulary, not the tab: LOINC:123 and ICD10:123 are not
      // duplicates on Overall, nor Apple:123 and Garmin:123 on Wearables.
      const key = JSON.stringify([vocabulary, code]);
      const group = groups.get(key) ?? { code, vocabulary, rows: [] };
      group.rows.push(row);
      groups.set(key, group);
    }
    return [...groups.values()].filter((group) => group.rows.length > 1)
      .sort((a, b) => a.vocabulary.localeCompare(b.vocabulary) || a.code.localeCompare(b.code));
  }, [rows, overallTab, selectedVocabulary, browse]);

  const revealDuplicate = (row: CodeMappingRow) => {
    if (browse) { openEditDialog(row); return; }
    setSearchQuery("");
    if (row.mapping_origin === "athena") setAthenaCollapsed(false);
    else if (row.status === "approved") setMappedCollapsed(false);
    else if (row.status === "rejected") setRejectedCollapsed(false);
    else setUnmappedCollapsed(false);
    // An object also retriggers navigation when the same link is clicked twice.
    setNavigationTarget({ id: mappingRowId(row) });
  };

  useEffect(() => {
    if (!navigationTarget) return;
    const target = document.getElementById(navigationTarget.id);
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    target?.focus({ preventScroll: true });
  }, [navigationTarget]);

  const visibleRows = useMemo(() => {
    if (browse) return rows;
    const q = searchQuery.trim().toLowerCase();
    return rows.filter((row) => {
      // A query is an intentional escape hatch from the current tab: a
      // curator should not have to try every code system to find an incoming
      // code. With no query, retain the focused, one-vocabulary-at-a-time
      // review queue.
      if (!q && selectedVocabulary !== OVERALL_TAB && tabForRow(row) !== selectedVocabulary) return false;
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
  }, [rows, searchQuery, selectedVocabulary, browse]);

  // Rows can arrive from other tabs: in browse mode the server searches every
  // coding system whenever a query is set, and the client filter above does
  // the same in legacy mode. Such hits carry no system in the table's usual
  // columns, so label them with their tab while any are on screen (#963).
  // Keyed on the rows, not on how they got here.
  const tabLabels = useMemo(
    () => new Map(vocabularyTabs.map((tab) => [tab.vocabulary_id, tab.label])),
    [vocabularyTabs],
  );
  const systemLabel = (row: CodeMappingRow) =>
    tabLabels.get(tabForRow(row)) ?? (row.source_vocabulary_id || "Uncoded");
  const foreignHits = useMemo(
    () => (overallTab ? 0 : visibleRows.filter((row) => tabForRow(row) !== selectedVocabulary).length),
    [overallTab, visibleRows, selectedVocabulary],
  );
  const showSystemColumn = foreignHits > 0;
  // The debounced query is what the server has answered, so the message
  // describes the rows on screen rather than re-announcing every keystroke.
  const crossTabSearch = !overallTab && debouncedSearch.trim() !== "";

  // Four-section layout: UNMAPPED / MAPPED / REJECTED / ATHENA MAPPED.
  const athenaRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin === "athena").sort(browse ? () => 0 : byOccurrence),
    [visibleRows, browse],
  );
  const unmappedRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin !== "athena" && r.status !== "approved" && r.status !== "rejected").sort(browse ? () => 0 : byOccurrence),
    [visibleRows, browse],
  );
  const rejectedRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin !== "athena" && r.status === "rejected").sort(browse ? () => 0 : byOccurrence),
    [visibleRows, browse],
  );
  const mappedRows = useMemo(
    () => visibleRows.filter((r) => r.mapping_origin !== "athena" && r.status === "approved").sort(browse ? () => 0 : byOccurrence),
    [visibleRows, browse],
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
    setInlineMappingId(null);
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setError("");
    setSearchingConcepts(false);
    setCheckingUmls(false);
    setSelectedRow(null);
    setForm({ ...emptyForm });
    setSearchVocabulary("");
    setSearchScope(DEFAULT_SEARCH_SCOPE);
    setConceptSearchQuery("");
    setConceptResults([]);
    setUmlsCheckMessage("");
    setRepointResult(null);
    setDialogMode("new");
  };

  const openEditDialog = async (row: CodeMappingRow) => {
    setInlineMappingId(null);
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setError("");
    setSearchingConcepts(false);
    setCheckingUmls(false);
    // Acquire edit lock before opening the dialog.
    if (row.mapping_id) {
      try {
        await api.post(`/v1/code-mappings/${row.mapping_id}/lock/`);
      } catch (err) {
        const resp = err && typeof err === "object" && "response" in err
          ? (err as { response?: { status?: number; data?: { detail?: string; locked_by?: string } } }).response
          : undefined;
        if (resp?.status === 423) {
          setError(`Locked by ${resp.data?.locked_by || "another user"}.`);
          return;
        }
        // Non-lock errors — still open the dialog, editing may work.
      }
    }
    setSelectedRow(row);
    setForm(buildEditForm(row, reference));
    // Open scoped to the row's own destination vocabulary when the dialog can
    // offer it; otherwise search everything rather than show a scope the
    // dropdown has no entry for (an ICD10CM destination, say).
    setSearchVocabulary(
      reference.destination_vocabularies.some((v) => v.vocabulary_id === row.destination_vocabulary_id)
        ? row.destination_vocabulary_id || ""
        : "",
    );
    setSearchScope(DEFAULT_SEARCH_SCOPE);
    setConceptSearchQuery("");
    setConceptResults([]);
    setUmlsCheckMessage("");
    setRepointResult(null);
    setDialogMode("edit");
  };

  const closeDialog = () => {
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setError("");
    setSearchingConcepts(false);
    setCheckingUmls(false);
    setMintOpen(false);
    // Release edit lock when closing.
    if (selectedRow?.mapping_id) {
      api.delete(`/v1/code-mappings/${selectedRow.mapping_id}/lock/`).catch(() => {});
    }
    setDialogMode(null);
    setSelectedRow(null);
    setSaving(false);
    setRepointing(null);
    setRepointResult(null);
    setUmlsCheckMessage("");
  };

  const setField = (field: keyof MappingForm, value: string) => {
    if (field.startsWith("source_") || field.startsWith("destination_")) setSuggestionMessage("");
    if (field.startsWith("destination_")) dialogChoice.current = dialogRequest.current;
    if (["source_code", "source_vocabulary_id", "source_code_description", "omop_table"].includes(field)) {
      dialogRequest.current += 1;
      setSearchingConcepts(false);
      setCheckingUmls(false);
      setError("");
    }
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  /**
   * Domain is the first choice and it settles two others: which source code
   * systems are plausible, and which OMOP table the fact lands in.
   */
  const setDomain = (domainId: string) => {
    setSuggestionMessage("");
    dialogRequest.current += 1;
    setSearchingConcepts(false);
    setCheckingUmls(false);
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
  };

  /** Apply a concept to the form: id, name, code, vocabulary, class, standard flag. */
  const applyConcept = (concept: ConceptResult, adoptDomain = false) => {
    dialogChoice.current = dialogRequest.current;
    setSuggestionMessage("");
    setForm((prev) => {
      // A concept only supplies the domain when the curator has not chosen one;
      // Domain is theirs, and the table follows from it, not from the concept.
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
        destination_invalid_reason: "",
      }));
    }
  };

  const selectReplacement = async () => {
    if (!form.destination_concept_id) return;
    try {
      const { data } = await api.get(`/v1/concepts/${form.destination_concept_id}/replacement/`);
      if (!data.replaced) {
        setBanner("No active replacement is recorded; search for a current destination concept.");
        return;
      }
      applyConcept(data.resolved_concept);
      setBanner(`Replaced with active concept ${data.resolved_concept.concept_id}.`);
    } catch {
      setError("Could not look up a replacement concept.");
    }
  };

  useEffect(() => () => {
    if (conceptSearchTimer.current !== null) window.clearTimeout(conceptSearchTimer.current);
    conceptSearchAbort.current?.abort();
  }, []);

  const searchConcepts = (query: string, vocabulary = searchVocabulary, scope = searchScope) => {
    // Keep the raw value in state and trim only for the request. Trimming
    // before setState meant typing a space produced the same string back, React
    // re-rendered without it, and a multi-word search could never be typed.
    const request = ++dialogRequest.current;
    setConceptSearchQuery(query);
    setCheckingUmls(false);
    // At most one search is ever outstanding (#1466). Firing one per keystroke
    // queued a second-long query per letter behind each other on the server —
    // discarding the stale responses here did not stop the server running them.
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
      // The dialog moved on (closed, suggested, picked a concept) while waiting.
      if (request !== dialogRequest.current) return;
      const controller = new AbortController();
      conceptSearchAbort.current = controller;
      try {
        const matches = await searchDestinationConcepts(q, vocabulary, controller.signal, scope);
        if (request === dialogRequest.current) setConceptResults(matches);
      } catch {
        if (request === dialogRequest.current) setConceptResults([]);
      } finally {
        if (request === dialogRequest.current) setSearchingConcepts(false);
      }
    }, CONCEPT_SEARCH_DEBOUNCE_MS);
  };

  const suggestCurrentCode = async () => {
    setSuggestionMessage("");
    const request = ++dialogRequest.current;
    dialogChoice.current = null;
    setIndividualSuggestion({ request, activity: [], running: true });
    setCheckingUmls(false);
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
        } catch (error) {
          if (++failures > SUGGEST_POLL_MAX_FAILURES) throw error;
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
        setIndividualSuggestion(current => current?.request === request ? { ...current, running: false } : current);
      }
    }
  };

  const checkUmls = async () => {
    const request = ++dialogRequest.current;
    setSearchingConcepts(false);
    setError("");
    setCheckingUmls(true);
    setUmlsCheckMessage("");
    try {
      const { data } = await api.post("/v1/code-mappings/check-umls/", {
        source_code: form.source_code,
        source_vocabulary_id: form.source_vocabulary_id,
      });
      if (request !== dialogRequest.current) return;
      if (!data.found) {
        setUmlsCheckMessage("Missing from UMLS");
        return;
      }
      setForm((prev) => ({
        ...prev,
        source_code_description: data.source_code_description || prev.source_code_description,
        source_concept_id: data.source_concept_id ? String(data.source_concept_id) : "",
      }));
      setUmlsCheckMessage("Found in UMLS");
    } catch {
      if (request === dialogRequest.current) setError("Failed to check UMLS.");
    } finally {
      if (request === dialogRequest.current) setCheckingUmls(false);
    }
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

  const applySavedMapping = (saved: CodeMappingRow) => {
    if (!saved?.mapping_id || !saved.status) return;
    // Reflect a successful server write immediately, never a speculative
    // approval. Background reload reconciles ordering, totals and other users.
    setRows((current) => current.map((row) => row.mapping_id === saved.mapping_id ? saved : row));
    setBrowse((current) => {
      const previous = current?.results.find((row) => row.mapping_id === saved.mapping_id);
      if (!current || !previous || previous.status === saved.status
          || previous.source_vocabulary_id !== saved.source_vocabulary_id
          || previous.mapping_origin === "athena") return current;
      const pages = { ...current.pages };
      for (const [row, delta] of [[previous, -1], [saved, 1]] as const) {
        const section = sectionForRow(row);
        pages[section] = { ...pages[section], total: Math.max(0, pages[section].total + delta) };
      }
      return {
        ...current, pages,
        results: current.results.map((row) => row.mapping_id === saved.mapping_id ? saved : row),
        rejected_count: current.rejected_count + Number(saved.status === "rejected") - Number(previous.status === "rejected"),
        tabs: current.tabs.map((tab) => tab.vocabulary_id === OVERALL_TAB || tab.vocabulary_id === tabForRow(saved) ? {
          ...tab,
          proposed: tab.proposed + Number(saved.status === "proposed") - Number(previous.status === "proposed"),
          approved: tab.approved + Number(saved.status === "approved") - Number(previous.status === "approved"),
        } : tab),
      };
    });
  };

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
      applySavedMapping(resp.data);
      void refreshCurrent.current();
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
          ? Object.entries(detail).map(([field, value]) => `${field === "detail" ? "" : `${field}: `}${Array.isArray(value) ? value.join(", ") : value}`).join(" ")
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
  const hasRetrieval = strategies.umls || strategies.lexical || strategies.vectors;

  const runSuggest = async () => {
    if (!validSuggestionLimit) {
      setError(`Choose a number of suggestions between 1 and ${maxSuggestions}.`);
      return;
    }
    // Replace is only valid when a specific vocabulary is selected (the backend
    // rejects replace without source_vocabulary_id to prevent global deletes).
    const effectiveReplace = replaceExisting && !overallTab;

    // Show inline confirmation when replacing existing suggestions.
    if (effectiveReplace && !confirmReplace) {
      setConfirmReplace(true);
      return;
    }
    setConfirmReplace(false);

    setSuggesting(true);
    setError("");
    setBanner(null);
    setSuggestFlash(false);
    setSuggestRun(null);
    try {
      const activeStrategies = Object.entries(strategies)
        .filter(([, v]) => v)
        .map(([k]) => k);
      // 202 with a run id: the work is queued, because a code costs ~3.5s and a
      // tab holds dozens, which does not fit inside a request.
      const { data: started } = await api.post<SuggestRunProgress>(
        "/v1/code-mappings/suggest/",
        {
          source_vocabulary_id: selectedVocabulary,
          limit: suggestionLimit,
          strategies: activeStrategies,
          ranking_model: rankingModel,
          replace: effectiveReplace,
          include_activity: true,
        },
      );
      suggestRunRef.current = started.run_id;
      setSuggestRun(started);
      const finished = await pollSuggestRun(started);
      if (suggestRunRef.current !== started.run_id) return;
      setSuggestRun(finished);

      if (finished.state === "failure") {
        setError(finished.error || "The suggest run failed.");
        return;
      }
      await refreshCurrent.current();
      announceSuggestRun(finished);
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

  /** Poll until the run reaches a terminal state, updating the strip as it goes.

   Bounded, because "running" is not a promise. If a broker is configured but
   nothing is consuming the queue, or the worker dies mid-run, the row never
   leaves `running` — an unbounded loop would poll for ever with the Suggest
   button disabled, recoverable only by reloading the page. */
  const pollSuggestRun = async (started: SuggestRunProgress) => {
    let current = started;
    let failures = 0;
    const deadline = Date.now() + SUGGEST_POLL_TIMEOUT_MS;
    // The inline dispatcher (a machine with no broker) finishes before the 202
    // is even written, so a run can arrive already terminal — poll only while
    // there is something left to watch.
    while (current.state === "queued" || current.state === "running") {
      if (Date.now() > deadline) {
        return {
          ...current,
          state: "failure" as const,
          error:
            "The suggest run stopped reporting progress. It may still be running — "
            + "reload to check, and make sure a Celery worker is consuming the queue.",
        };
      }
      await new Promise((resolve) => setTimeout(resolve, SUGGEST_POLL_INTERVAL_MS));
      if (suggestRunRef.current !== started.run_id) return current;  // superseded or unmounted
      try {
        const { data } = await api.get<SuggestRunProgress>(
          `/v1/code-mappings/suggest-runs/${started.run_id}/`, { params: { include_activity: "1" } },
        );
        failures = 0;
        current = data;
        if (suggestRunRef.current === started.run_id) setSuggestRun(data);
      } catch (err) {
        // One blip is not a failed run. The work is on a worker and carries on
        // writing destinations; treating a dropped GET as failure would show
        // "Failed to suggest mappings" over a run that succeeded, and skip the
        // refetch that puts its rows on screen.
        failures += 1;
        if (failures > SUGGEST_POLL_MAX_FAILURES) throw err;
      }
    }
    return current;
  };

  const announceSuggestRun = (run: SuggestRunProgress) => {
    // Say which tabs the new rows are in. A ranked suggestion's destination
    // is a standard concept, so its mapping belongs to the LOINC or SNOMED
    // tab rather than the HK-* one the button is on — correct, and baffling
    // if the curator is left to discover it.
    const where = Object.entries(run.landed_in || {})
      .sort((a, b) => b[1] - a[1])
      .map(([vocab, n]) => `${n} in ${vocab}`)
      .join(", ");
    const byStrategy = Object.entries(run.strategy_counts || {})
      .filter(([, n]) => n > 0)
      .map(([strategy, n]) => `${n} via ${strategy}`)
      .join(", ");
    setBanner(
      run.total
        ? `Wrote ${run.destinations} new destination(s) across ${run.total} queued code(s)`
          + (byStrategy ? ` (${byStrategy})` : "")
          + (where ? ` — ${where}.` : ".")
          + (run.remaining ? ` ${run.remaining} still awaiting a suggestion — run Suggest again.` : "")
        : "No queued codes on this tab awaiting a suggestion.",
    );
    setSuggestFlash(true);
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setSuggestFlash(false), 2500);
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
      applySavedMapping(resp.data);
      void refreshCurrent.current();
      if (repoint && repoint.rows_updated) {
        setBanner(
          `${row.source_code}: updated ${repoint.rows_updated} row(s) across `
          + `${repoint.persons_marked_stale} patient(s)`
          + (repoint.rows_collapsed ? `, ${repoint.rows_collapsed} duplicate(s) collapsed` : "")
          + ". Patient records queued for re-derivation.",
        );
      }
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || "Failed to update code mapping status.");
    }
  };

  const deleteMapping = async (row: CodeMappingRow) => {
    if (!row.mapping_id) return;
    setError("");
    try {
      const response = await api.delete(`/v1/code-mappings/${row.mapping_id}/`);
      closeDialog();
      if (response.status === 200 && response.data?.mapping_id) {
        // Backend cleared the destination instead of deleting; apply locally.
        applySavedMapping(response.data as CodeMappingRow);
      }
      await refreshCurrent.current();
    } catch {
      setError("Failed to delete code mapping.");
    }
  };

  const renderTable = (sectionRows: CodeMappingRow[], emptyText: string, section: MappingSection, { hideStatus = false }: { hideStatus?: boolean } = {}) => {
    const colCount = 7 + (showSystemColumn ? 1 : 0) + (hideStatus ? 0 : 2);
    const sort = sectionSorts[section];
    const pagination = browse?.pages[section];
    const header = (label: string, column: SortColumn) => (
      <th className="px-4 py-3 font-semibold" aria-sort={sort?.column === column ? (sort.descending ? "descending" : "ascending") : "none"}>
        <button type="button" title={`Sort ${section} by ${label}`} className="inline-flex items-center gap-1 hover:underline focus:outline-2"
          onClick={() => { setPages((previous) => ({ ...previous, [section]: 1 })); setSectionSorts((previous) => ({ ...previous, [section]: {
            column, descending: previous[section]?.column === column ? !previous[section]?.descending : false,
          } })); }}>
          {label}<span aria-hidden="true">{sort?.column === column ? (sort.descending ? "↓" : "↑") : "↕"}</span>
        </button>
      </th>
    );
    return (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <table aria-label={`${section} mappings`} className="w-full border-collapse text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600">
          <tr>
            {header("Source code", "source_code")}
            {showSystemColumn && <th className="px-4 py-3 font-semibold">System</th>}
            {header("Seen", "occurrence_count")}
            {header("Source description", "source_code_description")}
            {header("Provenance", "origin_system")}
            {header("Destination concept", "destination_concept_name")}
            {header("Concept ID", "destination_concept_id")}
            {header("Dest count", "destination_count")}
            {!hideStatus && header("Status", "status")}
            {!hideStatus && <th className="w-16 px-4 py-3 font-semibold" aria-label="Actions" />}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {(browse ? sectionRows : sortMappingRows(sectionRows, sort)).map((row) => (
            <Fragment key={mappingRowId(row)}>
            <tr
              id={mappingRowId(row)}
              role="button"
              tabIndex={0}
              onClick={() => openEditDialog(row)}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget) return;
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  openEditDialog(row);
                }
              }}
              className={`scroll-mt-24 cursor-pointer hover:bg-slate-50 focus:outline-2 focus:outline-red-600 ${
                navigationTarget?.id === mappingRowId(row) ? "bg-red-50" : ""
              }`}
            >
              <td className="px-4 py-3 font-mono text-xs text-slate-900">
                {row.locked_by_username && <span title={`Locked by ${row.locked_by_username}`} className="mr-1 text-amber-500">&#128274;</span>}
                {row.source_code}
              </td>
              {showSystemColumn && (
                <td className="px-4 py-3 text-xs text-slate-700">{systemLabel(row)}</td>
              )}
              <td className="px-4 py-3 text-right font-mono text-xs text-slate-700">{row.occurrence_count || 0}</td>
              <td className="px-4 py-3 text-xs text-slate-700">{row.source_code_description || "—"}</td>
              <td className="px-4 py-3 text-xs text-slate-700">{row.origin_system || "—"}</td>
              <td className="px-4 py-3">
                <div className="font-medium text-slate-950">{row.destination_concept_name}</div>
                <div className="font-mono text-xs text-slate-500">
                  {row.destination_vocabulary_id}:{row.destination_concept_code}
                </div>
                {(row.measurement_type || row.suggested_unit) && (
                  <ConceptInputDetails domain_id={row.destination_domain_id || ""} measurement_type={row.measurement_type} suggested_unit={row.suggested_unit} example_units={row.example_units} />
                )}
                {section === "Unmapped" && row.mapping_id && <button type="button"
                  aria-label={`Choose destination for ${row.source_code}`}
                  aria-expanded={inlineMappingId === row.mapping_id}
                  onClick={event => { event.stopPropagation(); setInlineMappingId(current => current === row.mapping_id ? null : row.mapping_id); }}
                  className="mt-2 inline-flex items-center gap-1 rounded border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100">
                  <Search size={12} />{row.destination_concept_id ? "Change destination" : "Choose destination"}
                </button>}
              </td>
              <td className="px-4 py-3 font-mono text-xs text-slate-900">{row.destination_concept_id}</td>
              <td className={`px-4 py-3 text-center font-mono text-xs font-medium ${row.destination_count !== 1 ? "text-red-600" : "text-slate-700"}`}>{row.destination_count ?? 0}</td>
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
                  {row.suggest_strategy && (
                    <span className="inline-flex rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700" title={`Suggested via ${strategyLabel[row.suggest_strategy] || row.suggest_strategy}`}>
                      {strategyLabel[row.suggest_strategy] || row.suggest_strategy}
                    </span>
                  )}
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
            {inlineMappingId === row.mapping_id && row.mapping_id && <tr>
              <td colSpan={colCount} className="bg-slate-50 p-3">
                <InlineDestinationPicker key={row.mapping_id} mappingId={row.mapping_id}
                  sourceLabel={`${row.source_vocabulary_id || "Uncoded"}:${row.source_code} — ${row.source_code_description}`}
                  vocabularies={reference.destination_vocabularies}
                  initialVocabulary={reference.destination_vocabularies.some(item => item.vocabulary_id === row.destination_vocabulary_id) ? row.destination_vocabulary_id : ""}
                  canApprove={canApprove} onCancel={() => setInlineMappingId(null)}
                  onSaved={(saved, concept) => {
                    applySavedMapping(saved as CodeMappingRow);
                    setInlineMappingId(null);
                    setBanner(`${row.source_code}: saved ${concept.concept_name}${saved.status === "approved" ? " and approved the mapping" : " for review"}.`);
                    void refreshCurrent.current();
                  }} />
              </td>
            </tr>}
            </Fragment>
          ))}
          {sectionRows.length === 0 && (
            <tr>
              <td colSpan={colCount} className="px-4 py-8 text-center text-sm text-slate-500">{emptyText}</td>
            </tr>
          )}
        </tbody>
      </table>
      {pagination && pagination.total > pagination.page_size && (
        <nav aria-label={`${section} pages`} className="flex items-center justify-end gap-3 border-t p-3 text-sm">
          <button type="button" disabled={loading || pagination.page <= 1}
            onClick={() => setPages((previous) => ({ ...previous, [section]: pagination.page - 1 }))}>Previous</button>
          <span>Page {pagination.page} of {Math.ceil(pagination.total / pagination.page_size)} · {pagination.total} mappings</span>
          <button type="button" disabled={loading || pagination.page * pagination.page_size >= pagination.total}
            onClick={() => setPages((previous) => ({ ...previous, [section]: pagination.page + 1 }))}>Next</button>
        </nav>
      )}
    </div>
    );
  };

  if (loading && rows.length === 0 && !browse) {
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
              <PageTitle className="text-2xl font-semibold text-slate-950">Code Mapping</PageTitle>
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

        {loading && <p role="status" className="mb-2 text-sm text-slate-500">Loading mappings…</p>}

        {error && !dialogMode && (
          <div role="alert" className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
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
          {crossTabSearch && (
            <p className="mt-1 text-xs text-slate-600" role="status">
              Searching all coding systems
              {foreignHits > 0
                ? ` — ${foreignHits} match${foreignHits === 1 ? "" : "es"} from other tabs; the System column says which.`
                : " — every match is on this tab."}
            </p>
          )}
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
                onClick={() => {
                  setPages({});
                  setActiveVocabulary(tab.vocabulary_id);
                  if (tab.vocabulary_id === OVERALL_TAB) {
                    setUnmappedCollapsed(true);
                    setMappedCollapsed(true);
                    setRejectedCollapsed(true);
                    setAthenaCollapsed(true);
                  }
                }}
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

        {/* Keep the primary action immediately below the source tabs. */}
        <div role="group" aria-label="Suggest controls" className="mb-4 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void runSuggest()}
            disabled={suggesting || !hasRetrieval || !validSuggestionLimit || overallTab}
            title={overallTab ? "Select a specific vocabulary tab to run suggestions." : "Propose destinations for queued source codes on this tab."}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles size={13} />
            {suggesting ? "Suggesting…" : "Suggest"}
          </button>
          <input
            aria-label="Number of suggestions"
            type="number"
            min={1}
            max={maxSuggestions}
            value={suggestionLimit}
            onChange={(event) => setSuggestionLimit(event.target.value === "" ? "" : Number(event.target.value))}
            title={`Maximum codes to process this run (1–${maxSuggestions}), in Seen priority order.`}
            className="h-8 w-16 rounded-md border border-slate-300 px-2 text-xs"
          />
          <span className="text-xs text-slate-600">Using</span>
          {(["umls", "lexical", "vectors"] as const).map((key) => (
            <span key={key} className="inline-flex items-center gap-1">
              <label className="inline-flex items-center gap-1 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={strategies[key]}
                  onChange={(e) =>
                    setStrategies((prev) => ({ ...prev, [key]: e.target.checked }))
                  }
                  className="h-3.5 w-3.5 rounded border-slate-300 disabled:opacity-40"
                />
                <span>
                  {STRATEGY_LABELS[key]}
                </span>
              </label>
            </span>
          ))}
          <label className="inline-flex items-center gap-1 text-xs text-slate-600">
            <input
              type="checkbox"
              checked={replaceExisting}
              onChange={(e) => { setReplaceExisting(e.target.checked); setConfirmReplace(false); }}
              className="h-3.5 w-3.5 rounded border-slate-300"
            />
            Replace Current Suggestions
          </label>
          <label className="inline-flex items-center gap-1 text-xs text-slate-600">
            Ranker
            <select
              value={rankingModel}
              onChange={(e) => setRankingModel(e.target.value as "anthropic" | "jev" | "both")}
              className="h-7 rounded-md border border-slate-300 px-1.5 text-xs"
            >
              <option value="anthropic">Anthropic</option>
              <option value="jev">Jev</option>
              <option value="both">Both</option>
            </select>
          </label>
          <section
            aria-label="Suggestion accuracy"
            title="Overall reviews across all vocabularies and model versions, matching the History page's All models row."
            className="ml-auto flex max-w-full shrink-0 flex-wrap divide-x rounded-md border border-slate-200 bg-slate-50 text-right text-xs"
          >
            {([
              ['Approved', reviewTotals?.approved],
              ['Rejected', reviewTotals?.rejected],
              ['Other destination', reviewTotals?.overridden],
            ] as const).map(([label, value]) => (
              <div key={label} className="px-3 py-2">
                <div className="font-medium text-slate-500">{label}</div>
                <div className="text-sm font-semibold text-slate-900">{value ?? 0}</div>
              </div>
            ))}
            {([['Precision', allModels?.precision], ['Recall', allModels?.recall], ['F1', allModels?.f1]] as const).map(([label, value]) => (
              <div key={label} className="px-3 py-2">
                <div className="font-medium text-slate-500">{label}</div>
                <div className="text-sm font-semibold text-slate-900">{metric(value ?? null)}</div>
              </div>
            ))}
            <a href="/code-mappings/accuracy" className="px-3 py-2 text-left font-medium text-slate-700 underline hover:text-slate-950">
              History
            </a>
          </section>
        </div>

        {suggestRun && <SuggestCandidates key={suggestRun.run_id}
          activity={suggestRun.activity ?? []}
          finished={suggestRun.state === "success" || suggestRun.state === "failure"}
          canApprove={canApprove} vocabularies={reference.destination_vocabularies}
          domains={reference.domains}
          onSaved={() => { void refreshCurrent.current(); }} />}


        {/* Directly under the Suggest button, because that is where the eye
            already is when the wait starts. The run is queued and a code costs
            ~3.5s, so a spinner alone would leave a curator unable to tell a
            working run from a stuck one. */}
        {suggestRun && (
          <div
            role="status"
            aria-live="polite"
            data-testid="suggest-progress"
            className={
              "mb-4 rounded-md border px-3 py-2 text-xs transition-colors duration-500 "
              + (suggestRun.state === "failure"
                ? "border-rose-300 bg-rose-50 text-rose-900"
                : suggestFlash
                  ? "border-emerald-400 bg-emerald-50 text-emerald-900"
                  : "border-slate-300 bg-slate-50 text-slate-700")
            }
          >
            <div className="flex items-center justify-between gap-3">
              <span>{describeSuggestRun(suggestRun)}</span>
              <span className="font-medium tabular-nums">
                {suggestProgressCount(suggestRun)}/{suggestRun.total}
              </span>
            </div>
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className={
                  "h-full rounded-full transition-all duration-300 "
                  + (suggestRun.state === "failure"
                    ? "bg-rose-500"
                    : suggestRun.state === "success"
                      ? "bg-emerald-500"
                      : "bg-sky-500")
                }
                style={{
                  width: `${suggestRun.total
                    ? Math.round((suggestProgressCount(suggestRun) / suggestRun.total) * 100)
                    : 0}%`,
                }}
              />
            </div>
          </div>
        )}

        {confirmReplace && (
          <div className="mb-4 flex items-center gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            <span>
              {(() => {
                const mappingVersion = selectedAccuracy?.model_version;
                const isVersionBump = !!mappingVersion && mappingVersion !== suggestModelVersion;
                return isVersionBump
                  ? "This will replace all current suggestions and effectively freezes accuracy results for current model."
                  : "This will replace all current suggestions.";
              })()}
            </span>
            <button
              type="button"
              onClick={() => void runSuggest()}
              className="rounded bg-amber-600 px-2 py-1 text-xs font-medium text-white hover:bg-amber-700"
            >
              Confirm
            </button>
            <button
              type="button"
              onClick={() => setConfirmReplace(false)}
              className="text-xs font-medium text-amber-700 underline hover:text-amber-900"
            >
              Cancel
            </button>
          </div>
        )}

        {duplicateCodes.length > 0 && (
          <div role="alert" className="mb-4 rounded-md border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">
            <p className="font-semibold">Error: {duplicateCodes.length} duplicate source code{duplicateCodes.length === 1 ? "" : "s"} on this tab</p>
            <p className="mt-1">These source codes occur in multiple mapping rows. Follow a link, then select the row to open its edit dialog and delete unwanted duplicates. Hidden rejected mappings are included.</p>
            <ul aria-label="Duplicate source codes" className="mt-2 max-h-60 space-y-2 overflow-y-auto">
              {duplicateCodes.map((group) => (
                <li key={JSON.stringify([group.vocabulary, group.code])}>
                  <span className="font-mono font-semibold">{group.vocabulary || "Uncoded"}: {group.code}</span>
                  <ul className="ml-4 list-disc">
                    {group.rows.map((row) => (
                      <li key={mappingRowId(row)}>
                        <a
                          href={`#${mappingRowId(row)}`}
                          onClick={(event) => { event.preventDefault(); revealDuplicate(row); }}
                          className="rounded underline hover:text-red-950 focus:outline-2 focus:outline-red-600"
                        >
                          {row.source_code} — {sectionForRow(row)} · {row.source_vocabulary_id || "Uncoded"}
                          {row.status === "rejected" ? " · rejected" : ""}
                          {row.mapping_id != null ? ` · mapping #${row.mapping_id}` : ""}
                        </a>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mb-4 flex items-center">
          <span className="text-sm font-semibold uppercase tracking-wide text-slate-700">All</span>
          <span className="ml-1 text-sm font-normal text-slate-500">({visibleRows.length})</span>
          <DownloadMenu rows={visibleRows} section={`All-${selectedVocabulary}`} />
        </div>

        <section className="mb-6">
          <button
            type="button"
            onClick={() => setUnmappedCollapsed((v) => !v)}
            className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
          >
            {unmappedCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
            Unmapped <span className="font-normal text-slate-500">({browse?.pages.Unmapped.total ?? unmappedRows.length})</span>
          </button>
          <DownloadMenu rows={unmappedRows} section="Unmapped" />
          {!unmappedCollapsed && (
            <>
          <div className="mb-2">
            <p className="text-xs text-slate-500">
              The destination concept exists — an import minted or chose it — but no curator has confirmed it.
            </p>
          </div>
          {renderTable(unmappedRows, "Nothing awaiting review in this vocabulary.", "Unmapped")}
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
            Mapped <span className="font-normal text-slate-500">({browse?.pages.Mapped.total ?? mappedRows.length})</span>
          </button>
          <DownloadMenu rows={mappedRows} section="Mapped" />
          {!mappedCollapsed && renderTable(mappedRows, "No approved mappings in this vocabulary.", "Mapped")}
        </section>

        {(rejectedRows.length > 0 || (browse?.pages.Rejected?.total ?? 0) > 0) && (
          <section className="mb-6">
            <button
              type="button"
              onClick={() => setRejectedCollapsed((v) => !v)}
              className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
            >
              {rejectedCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              Rejected <span className="font-normal text-slate-500">({browse?.pages.Rejected?.total ?? rejectedRows.length})</span>
            </button>
            <DownloadMenu rows={rejectedRows} section="Rejected" />
            {!rejectedCollapsed && renderTable(rejectedRows, "No rejected mappings in this vocabulary.", "Rejected")}
          </section>
        )}

        {athenaRows.length > 0 && (
          <section>
            <button
              type="button"
              onClick={() => setAthenaCollapsed((v) => !v)}
              className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
            >
              {athenaCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              Athena Mapped <span className="font-normal text-slate-500">({browse?.pages["Athena Mapped"].total ?? athenaRows.length})</span>
            </button>
            <DownloadMenu rows={athenaRows} section="Athena Mapped" />
            {!athenaCollapsed && renderTable(athenaRows, "No Athena mappings in this vocabulary.", "Athena Mapped", { hideStatus: true })}
          </section>
        )}
      </div>

      {dialogMode && (
        <div inert={mintOpen} className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4">
          <form
            onSubmit={submitForm}
            role="dialog"
            aria-label={dialogMode === "new" ? "New Mapping" : "Edit Mapping"}
            className="w-full max-w-4xl rounded-md bg-white shadow-xl"
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
              {error && (
                <div role="alert" className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
                  {error}
                </div>
              )}
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

                  <div className="flex items-end gap-2">
                    <button
                      type="button"
                      onClick={checkUmls}
                      disabled={checkingUmls || !form.source_code.trim() || !form.source_vocabulary_id}
                      className="rounded-md border border-sky-300 px-3 py-2 text-sm font-medium text-sky-700 hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {checkingUmls ? "Checking UMLS…" : "Check UMLS"}
                    </button>
                    {umlsCheckMessage && <span className="pb-2 text-sm text-slate-600">{umlsCheckMessage}</span>}
                  </div>

                  <SourceVocabularyLookup
                    vocabularyId={form.source_vocabulary_id}
                    code={form.source_code}
                    onSelect={(term) => {
                      setField("source_code", term.code);
                      setField("source_code_description", term.name.slice(0, 255));
                      setField("source_concept_id", "");
                    }}
                  />

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
                  <ReadOnlyField
                    id="source_retirement"
                    label="Source code retirement"
                    tip="Retirement is based on the source vocabulary's invalid reason or expired validity date, never the destination or UMLS preference flag."
                    value={retirementLabel(selectedRow && form.source_code === selectedRow.source_code
                      && form.source_vocabulary_id === selectedRow.source_vocabulary_id ? selectedRow : null)}
                    testId="source-retirement"
                  />
                  {selectedRow?.source_retired && form.source_code === selectedRow.source_code
                    && form.source_vocabulary_id === selectedRow.source_vocabulary_id && (
                    <p className="text-sm font-semibold text-red-700" role="status">Source code is retired. {retirementDetail(selectedRow)}</p>
                  )}
                  {selectedRow?.umls_source_name && (
                    <ReadOnlyField
                      id="umls_source_name"
                      label="UMLS Name"
                      tip="Canonical UMLS preferred name for this source code. Read-only — edit Source Description instead."
                      value={selectedRow.umls_source_name}
                      testId="umls-source-name"
                    />
                  )}
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

                {dialogMode === "edit" && (
                  <div className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-3">
                    {(selectedRow?.destination_count || destinationOptions.length) > 1 && (
                      <p className="mb-2 font-semibold text-amber-900">
                        Multiple destinations are available for this source code. Review the source data alternatives and choose the correct destination.
                      </p>
                    )}
                    {loadingDestinations && <p role="status">Loading source destinations…</p>}
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
                              {option.concept_name} — {option.vocabulary_id}:{option.concept_code} — OMOP {option.concept_id ?? "not loaded"}
                              {option.measurement_type ? ` · ${option.measurement_type === "quantitative" ? "Quantitative" : "Qualitative"}` : ""}
                              {option.suggested_unit ? ` · ${option.suggested_unit}` : ""}
                              {option.origins.length ? ` (${option.origins.join(", ")})` : ""}
                              {!option.selectable ? " — unavailable" : ""}
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
                            <input type="checkbox" checked={strategies[key]} onChange={(e) => setStrategies((prev) => ({ ...prev, [key]: e.target.checked }))} />
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
                            onChange={(e) => setRankingModel(e.target.value as "anthropic" | "jev" | "both")}
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
                    {/* Destination Vocabulary ID itself is read-only - it is a
                        property of the resolved concept. The search still needs
                        a scope a curator can widen, or re-pointing a minted
                        HK-* mapping at a standard concept would be impossible,
                        which is the whole point of the queue. */}
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
                            {/* Why a code "looks wrong" (#1465): say what kind of
                                concept it is instead of leaving the curator to
                                infer it from the code's shape. */}
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
                <div className="mt-3 flex justify-end">
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
                      {" · suggested via "}
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
      {mintOpen && dialogMode && <MintConceptDialog
        vocabularies={reference.destination_vocabularies} domains={reference.domains}
        initialDomain={form.domain_id} initialName={form.source_code_description || form.source_code}
        sourceCode={form.source_code} sourceVocabulary={form.source_vocabulary_id}
        onClose={() => setMintOpen(false)}
        onSelect={concept => { applyConcept(concept); setMintOpen(false); }}
      />}
    </div>
  );
}
