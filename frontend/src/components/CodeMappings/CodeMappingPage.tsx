import ConceptToCodeTab from "./ConceptToCodeTab";
import CodeMappingUploadDialog, { type CodeMappingUploadResult } from "./CodeMappingUploadDialog";
import PageTitle from '@/components/Branding/PageTitle';
import InlineDestinationPicker from "./InlineDestinationPicker";
import SuggestCandidates from "./SuggestCandidates";
import EditMappingDialog from "./EditMappingDialog";
import ConceptInputDetails from "@/components/UI/ConceptInputDetails";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Check, ChevronDown, ChevronRight, Download, Pencil, Plus, Search, Sparkles, Upload } from "lucide-react";
import api from "@/api/axios";
import { useAuth } from "@/hooks/useAuth";
import { confirmUnitOverride, unitConfirmation } from "./unitConfirmation";
import {
  type CodeMappingRow,
  type Reference,
  type RepointResult,
  type SuggestRunProgress,
  emptyReference,
  statusClass,
  strategyLabel,
  STRATEGY_LABELS,
  SUGGEST_POLL_INTERVAL_MS,
  SUGGEST_POLL_MAX_FAILURES,
  SUGGEST_POLL_TIMEOUT_MS,
} from "./codeMappingTypes";

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

// Shared types and constants are imported from codeMappingTypes.ts above.

const EXPORT_COLUMNS: (keyof CodeMappingRow)[] = [
  "organization_slug", "organization_name", "source_vocabulary_id", "source_code", "source_code_description",
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
  // ICD10CM and ICD10 are now separate tabs — no alias needed.
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


/** Highest occurrence count first: the code seen 400 times is worth more of a curator's time. */
const byOccurrence = (a: CodeMappingRow, b: CodeMappingRow) =>
  (b.occurrence_count || 0) - (a.occurrence_count || 0)
  || (a.source_code || "").localeCompare(b.source_code || "");



type BrowseResponse = {
  // Source rows matching all active filters, before pagination or label grouping.
  total?: number;
  results: CodeMappingRow[];
  duplicates: CodeMappingRow[];
  tabs: { vocabulary_id: string; label: string; is_standard: boolean; proposed: number; approved: number; athena: number }[];
  selected_source: string;
  pages: Record<MappingSection, { page: number; page_size: number; total: number }>;
  rejected_count: number;
  provenances: { origin_system: string; count: number }[];
  selected_provenance: string;
  organizations: { organization_id: number | null; slug: string; name: string; count: number }[];
  selected_organization: string;
  groups: Partial<Record<MappingSection, GroupEntry[]>>;
  rollup: boolean;
};

/** One review row standing for every vendor code sharing a label. */
type GroupEntry = {
  label: string | null;
  description: string;
  members: number;
  seen: number;
  proposed: number;
  destination_concept_id: number | null;
  destination_concept_name: string | null;
  destination_concept_code: string | null;
  destination_vocabulary_id: string | null;
  mixed_destinations: boolean;
  status: string | null;
  mixed_statuses: boolean;
  suggested_action?: string;
  mapping_id: number | null;
};

/** The key the members endpoint takes: a label, or ':<pk>' for an unlabelled row. */
const groupKey = (entry: GroupEntry) => entry.label ?? `:${entry.mapping_id}`;
/** Cache key. A label whose codes span Unmapped and Mapped is two entries, and
 *  the server scopes members by section -- keying on the label alone would let
 *  one entry show the other's rows. */
const expansionKey = (entry: GroupEntry, section: MappingSection) => `${section}|${groupKey(entry)}`;
const sectionNames: MappingSection[] = ["Unmapped", "Mapped", "Rejected", "Athena Mapped"];
const DEFAULT_SECTION_SORT: SectionSort = { column: "occurrence_count", descending: true };
// A blank origin_system is a real value -- enqueue_unmapped_source_codes writes
// it for every newly queued code -- but "" is already the select's "no filter"
// value, so filtering to blank needs a sentinel the server translates back.
const BLANK_PROVENANCE = "__blank__";
const GLOBAL_ORGANIZATION = "__none__";

export default function CodeMappingPage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  const canApprove = !!(currentUser?.is_staff || currentUser?.is_org_admin);
  // ?direction=reverse&concept=<id>, from the field mapper's {codes} button:
  // open Concept → Source Code on that concept's source codes.
  const [searchParams] = useSearchParams();
  const linkedConcept = Number(searchParams.get('concept'));
  // Held in state and cleared once opened: switching direction remounts the
  // reverse view, which must not reopen the concept each time.
  const [linkedConceptId, setLinkedConceptId] = useState<number | undefined>(
    Number.isInteger(linkedConcept) && linkedConcept > 0 ? linkedConcept : undefined);
  const [direction, setDirection] = useState<'forward' | 'reverse'>(
    searchParams.get('direction') === 'reverse' ? 'reverse' : 'forward');
  const [reverseWriting, setReverseWriting] = useState(false);
  const [browse, setBrowse] = useState<BrowseResponse | null>(null);
  const [pages, setPages] = useState<Partial<Record<MappingSection, number>>>({});
  const loadSequence = useRef(0);
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [rows, setRows] = useState<CodeMappingRow[]>([]);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const referenceCache = useRef<Reference | null>(null);
  const [accuracy, setAccuracy] = useState<AccuracyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [error, setError] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  // `null` means no choice has been made, so use the work-prioritized default.
  // The empty string is a real vocabulary ID: it represents the Uncoded tab.
  const [activeVocabulary, setActiveVocabulary] = useState<string | null>(null);
  const [unmappedCollapsed, setUnmappedCollapsed] = useState(false);
  const [mappedCollapsed, setMappedCollapsed] = useState(true);
  const [rejectedCollapsed, setRejectedCollapsed] = useState(true);
  const [athenaCollapsed, setAthenaCollapsed] = useState(true);
  // Every section defaults to Seen descending (#1575), stated explicitly so
  // the header shows which column is sorted. The server applies the same
  // default for a caller that sends no order.
  const [sectionSorts, setSectionSorts] = useState<Partial<Record<MappingSection, SectionSort>>>(
    () => sectionNames.reduce<Partial<Record<MappingSection, SectionSort>>>(
      (sorts, section) => ({ ...sorts, [section]: DEFAULT_SECTION_SORT }), {}),
  );
  const [provenanceFilter, setProvenanceFilter] = useState("");
  const [organizationFilter, setOrganizationFilter] = useState("");
  const [rollup, setRollup] = useState(false);
  const [seenOnly, setSeenOnly] = useState(true);
  // key -> members, or null while the fetch is in flight.
  const [expandedGroups, setExpandedGroups] = useState<Record<string, CodeMappingRow[] | null>>({});
  const [groupRunning, setGroupRunning] = useState<string | null>(null);
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
    suggestRunRef.current = null;
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => { setDebouncedSearch(searchQuery); setPages({}); }, 250);
    return () => window.clearTimeout(timer);
  }, [searchQuery]);

  const fetchAll = useCallback(async () => {
    if (direction === 'reverse') return;
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError("");
    const params: Record<string, string | number> = {
      browse: 1, search: debouncedSearch, seen_only: seenOnly ? "1" : "0",
    };
    if (activeVocabulary !== null) params.source = activeVocabulary;
    if (provenanceFilter) params.provenance = provenanceFilter;
    if (organizationFilter) params.organization = organizationFilter;
    if (rollup) params.rollup = 1;
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
  }, [activeVocabulary, debouncedSearch, pages, sectionSorts, provenanceFilter, organizationFilter, rollup, seenOnly, direction]);

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
    const countedRows = seenOnly ? rows.filter((row) => row.occurrence_count > 0) : rows;
    countedRows.forEach((row) => {
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
      proposed: countedRows.filter((r) => r.status === "proposed").length,
      approved: countedRows.filter((r) => r.status === "approved").length,
      athena: countedRows.filter((r) => r.mapping_origin === "athena").length,
    });
    return result;
  }, [rows, reference, browse, seenOnly]);

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
      const key = JSON.stringify([row.organization_id ?? null, vocabulary, code]);
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
    if (!row.occurrence_count) setSeenOnly(false);
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
      if (seenOnly && !(row.occurrence_count > 0)) return false;
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
        row.organization_name,
        row.organization_slug,
        row.destination_concept_name,
        row.destination_concept_code,
        String(row.destination_concept_id),
      ].some((value) => (value || "").toLowerCase().includes(q));
    });
  }, [rows, searchQuery, selectedVocabulary, browse, seenOnly]);

  const allCount = browse?.total ?? (browse
    ? sectionNames.reduce((total, section) => total + (browse.pages[section]?.total ?? 0), 0)
    : visibleRows.length);

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
  const showOrganizationColumn = selectedVocabulary === "EPIC" || selectedVocabulary === "CERNER"
    || visibleRows.some((row) => !!row.organization_id);
  // The debounced query is what the server has answered, so the message
  // describes the rows on screen rather than re-announcing every keystroke.
  const crossTabSearch = !overallTab && debouncedSearch.trim() !== "";
  // Server-supplied, and taken from the whole tab rather than the filtered
  // rows, so picking one option does not remove the rest.
  const provenanceOptions = browse?.provenances ?? [];
  const organizationOptions = browse?.organizations ?? [];
  const provenanceValue = (origin: string) => origin || BLANK_PROVENANCE;

  const loadGroup = useCallback(async (entry: GroupEntry, section: MappingSection) => {
    const cacheKey = expansionKey(entry, section);
    setExpandedGroups((current) => ({ ...current, [cacheKey]: null }));
    // A tab switch clears the cache; without this, a fetch already in flight
    // resolves afterwards and re-inserts the previous tab's rows.
    const sequence = loadSequence.current;
    try {
      const { data } = await api.get<{ results: CodeMappingRow[] }>("/v1/code-mappings/group/", {
        params: {
          label: groupKey(entry), section,
          seen_only: seenOnly ? "1" : "0",
          // The server picks the default tab until one is clicked, so sending
          // activeVocabulary would fetch across every vocabulary on first load.
          ...(selectedVocabulary !== null ? { source: selectedVocabulary } : {}),
          ...(debouncedSearch ? { search: debouncedSearch } : {}),
          ...(provenanceFilter ? { provenance: provenanceFilter } : {}),
          ...(organizationFilter ? { organization: organizationFilter } : {}),
        },
      });
      if (sequence !== loadSequence.current) return;
      setExpandedGroups((current) => ({ ...current, [cacheKey]: data.results || [] }));
    } catch {
      if (sequence !== loadSequence.current) return;
      // Leave it collapsed rather than showing an empty group, which would
      // read as "this label has no codes".
      setExpandedGroups(({ [cacheKey]: _failed, ...rest }) => rest);
      setError("Could not load the codes in that group.");
    }
  }, [selectedVocabulary, debouncedSearch, provenanceFilter, organizationFilter, seenOnly]);

  const toggleGroup = useCallback((entry: GroupEntry, section: MappingSection) => {
    const cacheKey = expansionKey(entry, section);
    if (cacheKey in expandedGroups) {
      setExpandedGroups(({ [cacheKey]: _removed, ...rest }) => rest);
      return;
    }
    void loadGroup(entry, section);
  }, [expandedGroups, loadGroup]);

  const runGroupJob = async (entry: GroupEntry, action: "suggest" | "approve" | "reject") => {
    const key = expansionKey(entry, "Unmapped");
    if (action !== "suggest" && !window.confirm(
      `${action === "approve" ? "Approve" : "Reject"} ${entry.proposed.toLocaleString()} still-proposed code(s) in this label group?`,
    )) return;
    setGroupRunning(key);
    setError("");
    setBanner(null);
    try {
      const payload = {
        label: groupKey(entry), source: selectedVocabulary,
        seen_only: seenOnly ? "1" : "0",
        ...(provenanceFilter ? { provenance: provenanceFilter } : {}),
        ...(organizationFilter ? { organization: organizationFilter } : {}),
        ...(action === "suggest" ? {
          strategies: Object.entries(strategies).filter(([, enabled]) => enabled).map(([name]) => name),
          ranking_model: rankingModel,
        } : {
          action,
          ...(action === "approve" ? { destination_concept_id: entry.destination_concept_id } : {}),
        }),
      };
      const endpoint = action === "suggest"
        ? "/v1/code-mappings/group/suggest/"
        : "/v1/code-mappings/group/action/";
      let started: SuggestRunProgress;
      try {
        ({ data: started } = await api.post<SuggestRunProgress>(endpoint, payload));
      } catch (err) {
        const warning = unitConfirmation(err);
        if (!warning) throw err;
        if (!confirmUnitOverride(warning)) return;
        ({ data: started } = await api.post<SuggestRunProgress>(endpoint, {
          ...payload, confirm_unit_mismatch: true,
        }));
      }
      suggestRunRef.current = started.run_id;
      setSuggestRun(started);
      const finished = await pollSuggestRun(started);
      if (finished.state === "failure") throw new Error(finished.error || "Group job failed.");
      await refreshCurrent.current();
      setExpandedGroups({});
      const proposedRejection = finished.activity?.some((event) => event.action === "reject");
      setBanner(
        action === "suggest"
          ? proposedRejection
            ? `Proposed rejection for ${finished.done} eligible code(s); confirm it on the group row.`
            : finished.destinations
              ? `Suggested once and applied the destination to ${finished.destinations} eligible code(s).`
              : "Group Suggest found no safe destination; no mapping was changed."
          : `${action === "approve" ? "Approved" : "Rejected"} ${finished.done} eligible code(s).`,
      );
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail || (err instanceof Error ? err.message : "Could not update the label group."));
    } finally {
      setGroupRunning(null);
    }
  };
  // A filter can outlive the values that produced it -- the rows carrying it
  // get approved away, or the server's default tab moves before any tab has
  // been clicked. Keep it listed and keep the control mounted, or there is no
  // way left to clear it and the queue reads as empty for no visible reason.
  const provenanceOrphaned = provenanceFilter !== ""
    && !provenanceOptions.some((option) => provenanceValue(option.origin_system) === provenanceFilter);
  const showProvenanceFilter = provenanceOptions.length > 1 || provenanceFilter !== "";

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

  const openNewDialog = () => {
    setInlineMappingId(null);
    setSelectedRow(null);
    setDialogMode("new");
  };

  const openEditDialog = (row: CodeMappingRow) => {
    setInlineMappingId(null);
    setSelectedRow(row);
    setDialogMode("edit");
  };

  const closeDialog = () => {
    setDialogMode(null);
    setSelectedRow(null);
  };

  const applySavedMapping = (saved: CodeMappingRow) => {
    if (!saved?.mapping_id || !saved.status) return;
    // Reflect a successful server write immediately, never a speculative
    // approval. Background reload reconciles ordering, totals and other users.
    setRows((current) => current.map((row) => row.mapping_id === saved.mapping_id ? saved : row));
    // Under rollup the queue rows live in the expanded groups, not in `rows`
    // or `browse.results` -- both of which the server leaves empty. Without
    // this, approving an expanded member leaves it showing its old status and
    // the curator's own write looks like it did nothing.
    setExpandedGroups((current) => {
      let touched = false;
      const next = Object.fromEntries(Object.entries(current).map(([key, members]) => {
        if (!members?.some((row) => row.mapping_id === saved.mapping_id)) return [key, members];
        touched = true;
        return [key, members.map((row) => row.mapping_id === saved.mapping_id ? saved : row)];
      }));
      return touched ? next : current;
    });
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
      const payload = {
        domain_id: row.domain_id || row.destination_domain_id || "",
        source_vocabulary_id: row.source_vocabulary_id,
        source_code: row.source_code,
        source_code_description: row.source_code_description,
        destination_concept_id: row.destination_concept_id,
        destination_vocabulary_id: row.destination_vocabulary_id,
        omop_table: row.destination_omop_table,
        status: row.status === "approved" ? "proposed" : "approved",
        notes: row.notes,
      };
      let resp;
      try {
        resp = await api.patch(`/v1/code-mappings/${row.mapping_id}/`, payload);
      } catch (err) {
        const warning = unitConfirmation(err);
        if (!warning) throw err;
        if (!confirmUnitOverride(warning)) return;
        resp = await api.patch(`/v1/code-mappings/${row.mapping_id}/`, {
          ...payload, confirm_unit_mismatch: true,
        });
      }
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

  /** Callback for EditMappingDialog onSaved. */
  const handleDialogSaved = (saved: CodeMappingRow, repoint: RepointResult | null) => {
    applySavedMapping(saved);
    void refreshCurrent.current();
    if (repoint && repoint.rows_updated) {
      setBanner(
        `${saved.source_code}: updated ${repoint.rows_updated} row(s) across `
        + `${repoint.persons_marked_stale} patient(s)`
        + (repoint.rows_collapsed ? `, ${repoint.rows_collapsed} duplicate(s) collapsed` : "")
        + ". Patient records queued for re-derivation.",
      );
    }
  };

  /** Callback for EditMappingDialog onDeleted. */
  const handleDialogDeleted = (row: CodeMappingRow) => {
    applySavedMapping(row);
    void refreshCurrent.current();
  };

  const renderTable = (sectionRows: CodeMappingRow[], emptyText: string, section: MappingSection, { hideStatus = false }: { hideStatus?: boolean } = {}) => {
    const colCount = 7 + (showSystemColumn ? 1 : 0) + (showOrganizationColumn ? 1 : 0) + (hideStatus ? 0 : 2);
    const sort = sectionSorts[section];
    // Present only when the server grouped this response; otherwise the flat
    // queue renders exactly as before.
    const groupEntries = browse?.rollup ? browse.groups[section] ?? [] : null;
    const pagination = browse?.pages[section];
    const header = (label: string, column: SortColumn) => {
      const canSortGroup = column === "occurrence_count" || column === "source_code_description";
      const sortable = groupEntries === null || canSortGroup;
      return (
      <th className="px-4 py-3 font-semibold" aria-label={column === "occurrence_count" ? label : undefined}
        aria-sort={!sortable || sort?.column !== column ? "none" : sort.descending ? "descending" : "ascending"}>
        {column === "occurrence_count" && <label className="mb-1 flex items-center gap-1 whitespace-nowrap text-xs font-normal normal-case tracking-normal">
          <input type="checkbox" checked={seenOnly}
            aria-label={`${section}: only codes with Seen greater than zero`}
            title="Filter all source tabs and sections to codes with Seen > 0"
            onChange={(event) => { setPages({}); setExpandedGroups({}); setSeenOnly(event.target.checked); }} />
          &gt; 0 only
        </label>}
        {sortable ? (
          <button type="button" title={`Sort ${section} by ${label}`} className="inline-flex items-center gap-1 hover:underline focus:outline-2"
            onClick={() => { setPages((previous) => ({ ...previous, [section]: 1 })); setSectionSorts((previous) => ({ ...previous, [section]: {
              column, descending: previous[section]?.column === column ? !previous[section]?.descending : false,
            } })); }}>
            {label}<span aria-hidden="true">{sort?.column === column ? (sort.descending ? "↓" : "↑") : "↕"}</span>
          </button>
        ) : (
          <span>{label}</span>
        )}
      </th>
      );
    };
    // Extracted so the rollup can render the same row under a group entry.
    // A member row must behave exactly like a queue row -- same click to
    // edit, same lock badge, same inline destination picker -- or expanding
    // a group would quietly become a read-only view.
    const renderRow = (row: CodeMappingRow) => (
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
            {showOrganizationColumn && (
              <td className="px-4 py-3 text-xs text-slate-700">{row.organization_name || "Global / unattributed"}</td>
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
    );
    return (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <table aria-label={`${section} mappings`} className="w-full border-collapse text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600">
          <tr>
            {header("Source code", "source_code")}
            {showSystemColumn && <th className="px-4 py-3 font-semibold">System</th>}
            {showOrganizationColumn && <th className="px-4 py-3 font-semibold">Organization</th>}
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
          {groupEntries
            ? groupEntries.map((entry) => {
              const cacheKey = expansionKey(entry, section);
              const open = cacheKey in expandedGroups;
              const members = expandedGroups[cacheKey];
              return (
                <Fragment key={cacheKey}>
                  <tr className="bg-slate-50/60">
                    <td className="px-4 py-3">
                      <button type="button"
                        aria-expanded={open}
                        aria-label={`${open ? "Collapse" : "Expand"} ${entry.description || "unlabelled"}`}
                        onClick={() => toggleGroup(entry, section)}
                        className="inline-flex items-center gap-2 text-left font-medium text-slate-900 hover:underline">
                        <span aria-hidden="true" className="inline-block w-3">{open ? "▾" : "▸"}</span>
                        <span className="font-mono text-xs">
                          {entry.members === 1 ? "1 code" : `${entry.members.toLocaleString()} codes`}
                        </span>
                      </button>
                    </td>
                    {showSystemColumn && <td className="px-4 py-3" />}
                    {showOrganizationColumn && <td className="px-4 py-3" />}
                    <td className="px-4 py-3 text-right font-mono text-xs text-slate-700">{entry.seen.toLocaleString()}</td>
                    <td className="px-4 py-3 text-xs font-medium text-slate-900">{entry.description || "—"}</td>
                    <td className="px-4 py-3" />
                    <td className="px-4 py-3">
                      {/* Naming one side of a disagreement is worse than
                          naming neither, so a mixed group names no concept. */}
                      {entry.mixed_destinations ? (
                        <span className="text-xs font-medium text-amber-700">Mixed destinations</span>
                      ) : entry.destination_concept_name ? (
                        <>
                          <div className="font-medium text-slate-950">{entry.destination_concept_name}</div>
                          <div className="font-mono text-xs text-slate-500">
                            {entry.destination_vocabulary_id}:{entry.destination_concept_code}
                          </div>
                        </>
                      ) : <span className="text-xs text-slate-500">—</span>}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-900">
                      {entry.mixed_destinations ? "" : entry.destination_concept_id ?? ""}
                    </td>
                    <td className="px-4 py-3" />
                    {!hideStatus && <td className="px-4 py-3 text-xs text-slate-700">
                      {entry.suggested_action === "reject"
                        ? <span className="font-medium text-amber-700">Reject suggested</span>
                        : entry.mixed_statuses ? "Mixed" : entry.status || "—"}
                    </td>}
                    {!hideStatus && <td className="px-4 py-3">
                      {section === "Unmapped" && entry.proposed > 0 && (
                        <div className="flex flex-col gap-1">
                          <button type="button" disabled={groupRunning === cacheKey}
                            onClick={() => void runGroupJob(entry, "suggest")}
                            className="rounded border border-slate-300 px-2 py-1 text-xs hover:bg-white disabled:opacity-50">
                            Suggest group
                          </button>
                          {entry.destination_concept_id && !entry.mixed_destinations && canApprove && (
                            <button type="button" disabled={groupRunning === cacheKey}
                              onClick={() => void runGroupJob(entry, "approve")}
                              className="rounded border border-green-300 px-2 py-1 text-xs text-green-800 hover:bg-green-50 disabled:opacity-50">
                              Approve {entry.proposed.toLocaleString()}
                            </button>
                          )}
                          <button type="button" disabled={groupRunning === cacheKey}
                            onClick={() => void runGroupJob(entry, "reject")}
                            className="rounded border border-red-200 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-50">
                            {entry.suggested_action === "reject" ? "Confirm reject" : "Reject group"}
                          </button>
                        </div>
                      )}
                    </td>}
                  </tr>
                  {open && members === null && (
                    <tr><td colSpan={colCount} className="px-4 py-3 text-center text-xs text-slate-500" role="status">Loading codes…</td></tr>
                  )}
                  {open && members && members.map(renderRow)}
                </Fragment>
              );
            })
            : (browse ? sectionRows : sortMappingRows(sectionRows, sort)).map(renderRow)}
          {(groupEntries ? groupEntries.length === 0 : sectionRows.length === 0) && (
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

  const directionControls = <div role="group" aria-label="Mapping direction" className="mb-5 flex gap-2 border-b border-slate-200 pb-3">
    {([['forward', 'Source Code → Concept'], ['reverse', 'Concept → Source Code']] as const).map(([value, label]) =>
      <button key={value} type="button" disabled={reverseWriting} aria-pressed={direction === value}
        onClick={() => setDirection(value)}
        className={`rounded px-3 py-2 text-sm font-medium disabled:opacity-50 ${direction === value ? 'bg-slate-950 text-white' : 'text-slate-600 hover:bg-slate-100'}`}>{label}</button>)}
  </div>;

  const releaseMarker = reference.release_commit ? (
    <span
      className="text-[10px] font-normal tracking-wide text-slate-400"
      title={`Deployed commit ${reference.release_commit}`}
      aria-label={`Deployed release ${reference.release_commit}`}
    >
      release {reference.release_commit.slice(0, 8)}
    </span>
  ) : null;

  if (direction === 'reverse') return <div className="min-h-screen bg-slate-50 p-6"><div className="mx-auto max-w-7xl">
    <div className="mb-5">
      <PageTitle className="text-2xl font-semibold text-slate-950" meta={releaseMarker}>Code Mapping</PageTitle>
    </div>
    {directionControls}
    <ConceptToCodeTab canApprove={canApprove} onWritingChange={setReverseWriting} initialConceptId={linkedConceptId}
      onInitialConceptHandled={() => setLinkedConceptId(undefined)} />
  </div></div>;

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
              <PageTitle className="text-2xl font-semibold text-slate-950" meta={releaseMarker}>Code Mapping</PageTitle>
              <p className="text-sm text-slate-600">
                Source codes from FHIR, paper labs and notes, mapped to destination OMOP concepts
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setUploadOpen(true)}
              className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-100">
              <Upload size={16} />
              Upload
            </button>
            <button
              onClick={openNewDialog}
              className="inline-flex items-center gap-2 rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
            >
              <Plus size={16} />
              New Mapping
            </button>
          </div>
        </div>

        {directionControls}
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
          <div className="flex flex-col gap-2 sm:flex-row">
            <label className="relative block flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <input
                aria-label="Search mappings"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search source codes, destination concepts, or OMOP IDs"
                className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 text-sm text-slate-950 outline-none focus:border-slate-700"
              />
            </label>
            {/* Provenance is a filter rather than the sort it used to be
                (#1575): it finds curator-edited rows in one click however many
                there are, and leaves the queue in Seen order. */}
            <label className="inline-flex h-10 shrink-0 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-700">
              <input type="checkbox" checked={rollup}
                onChange={(e) => {
                  setPages({});
                  setExpandedGroups({});
                  setSectionSorts((previous) => sectionNames.reduce<Partial<Record<MappingSection, SectionSort>>>((next, section) => {
                    const current = previous[section];
                    next[section] = current && (current.column === "occurrence_count" || current.column === "source_code_description")
                      ? current : DEFAULT_SECTION_SORT;
                    return next;
                  }, {}));
                  setRollup(e.target.checked);
                }} />
              {/* One decision per label rather than per vendor code: albumin
                  arrives under 2,557 of them. */}
              Group by label
            </label>
            {showProvenanceFilter && (
              <select
                aria-label="Filter by provenance"
                value={provenanceFilter}
                onChange={(e) => { setPages({}); setProvenanceFilter(e.target.value); }}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-950 outline-none focus:border-slate-700 sm:w-64"
              >
                <option value="">All provenance</option>
                {provenanceOptions.map((option) => (
                  <option key={provenanceValue(option.origin_system)} value={provenanceValue(option.origin_system)}>
                    {(option.origin_system || "No provenance")} ({option.count})
                  </option>
                ))}
                {provenanceOrphaned && (
                  <option value={provenanceFilter}>
                    {(provenanceFilter === BLANK_PROVENANCE ? "No provenance" : provenanceFilter)} (0)
                  </option>
                )}
              </select>
            )}
            {organizationOptions.length > 1 && (
              <select
                aria-label="Filter by organization"
                value={organizationFilter}
                onChange={(e) => { setPages({}); setExpandedGroups({}); setOrganizationFilter(e.target.value); }}
                className="h-10 rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-950 outline-none focus:border-slate-700 sm:w-64"
              >
                <option value="">All organizations</option>
                {organizationOptions.map((option) => (
                  <option key={option.slug || GLOBAL_ORGANIZATION} value={option.slug || GLOBAL_ORGANIZATION}>
                    {option.name} ({option.count})
                  </option>
                ))}
              </select>
            )}
          </div>
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
                  // Provenance values differ per tab; a stale filter would
                  // show an empty tab with no visible reason.
                  setProvenanceFilter("");
                  setOrganizationFilter("");
                  setExpandedGroups({});
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
          <span className="ml-1 text-sm font-normal text-slate-500" data-testid="all-mappings-count">({allCount})</span>
          <DownloadMenu rows={visibleRows} section={`All-${selectedVocabulary}`} />
          {browse && <span className="ml-3 text-xs text-slate-500">Downloads include loaded rows only.</span>}
        </div>

        <section className="mb-6">
          <button
            type="button"
            onClick={() => setUnmappedCollapsed((v) => !v)}
            className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
          >
            {unmappedCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
            Unmapped <span className="font-normal text-slate-500">({browse?.pages.Unmapped.total ?? unmappedRows.length}{browse?.rollup ? " groups" : ""})</span>
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
            Mapped <span className="font-normal text-slate-500">({browse?.pages.Mapped.total ?? mappedRows.length}{browse?.rollup ? " groups" : ""})</span>
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
              Rejected <span className="font-normal text-slate-500">({browse?.pages.Rejected?.total ?? rejectedRows.length}{browse?.rollup ? " groups" : ""})</span>
            </button>
            <DownloadMenu rows={rejectedRows} section="Rejected" />
            {!rejectedCollapsed && renderTable(rejectedRows, "No rejected mappings in this vocabulary.", "Rejected")}
          </section>
        )}

        {(athenaRows.length > 0 || (browse?.pages["Athena Mapped"]?.total ?? 0) > 0) && (
          <section>
            <button
              type="button"
              onClick={() => setAthenaCollapsed((v) => !v)}
              className="mb-2 inline-flex items-center gap-1 text-sm font-semibold uppercase tracking-wide text-slate-700"
            >
              {athenaCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              Athena Mapped <span className="font-normal text-slate-500">({browse?.pages["Athena Mapped"].total ?? athenaRows.length}{browse?.rollup ? " groups" : ""})</span>
            </button>
            <DownloadMenu rows={athenaRows} section="Athena Mapped" />
            {!athenaCollapsed && renderTable(athenaRows, "No Athena mappings in this vocabulary.", "Athena Mapped", { hideStatus: true })}
          </section>
        )}
      </div>

      {uploadOpen && <CodeMappingUploadDialog
        defaultProvenance={currentUser?.email || ""}
        onClose={() => setUploadOpen(false)}
        onUploaded={async (result: CodeMappingUploadResult) => {
          setUploadOpen(false);
          setPages({});
          setBanner(result.duplicate
            ? `This CSV was already uploaded. ${result.total} rows; no counts were added twice.`
            : `Uploaded ${result.total} rows: ${result.inserted} inserted, ${result.updated} updated, ${result.unchanged} unchanged.`);
          await refreshCurrent.current();
        }}
      />}

      <EditMappingDialog
        open={dialogMode !== null}
        onClose={closeDialog}
        mode={dialogMode || "new"}
        row={selectedRow}
        reference={reference}
        canApprove={canApprove}
        strategies={strategies}
        rankingModel={rankingModel}
        onStrategiesChange={setStrategies}
        onRankingModelChange={setRankingModel}
        onSaved={handleDialogSaved}
        onDeleted={handleDialogDeleted}
        onBanner={setBanner}
      />
    </div>
  );
}
