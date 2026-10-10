/**
 * Shared types, constants, and helpers used by both CodeMappingPage and
 * EditMappingDialog. Extracted to avoid circular imports and duplication.
 */

import type { SourceEvidence, SourceUnitEvidence } from "./SourceEvidencePanel";

// ── Row & form types ────────────────────────────────────────────────

export interface CodeMappingRow {
  mapping_id: number | null;
  domain_id?: string;
  source_vocabulary_id: string;
  source_code: string;
  source_code_description: string;
  organization_id?: number | null;
  organization_slug?: string;
  organization_name?: string;
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
  suggested_action?: string;
  umls_cui: string;
  created_by: string;
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
  source_evidence?: SourceEvidence;
  source_unit?: string;
  example_quantity?: string;
  source_metadata?: Record<string, unknown>;
  destination_unit_concept_id?: number | null;
}

export interface ConceptResult {
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

export interface SearchScope {
  retired: boolean;
  nonStandard: boolean;
}

export const DEFAULT_SEARCH_SCOPE: SearchScope = { retired: false, nonStandard: false };

export interface DestinationOption extends Omit<ConceptResult, "concept_id"> {
  concept_id: number | null;
  selectable: boolean;
  origins: string[];
  selected: boolean;
}

export interface VocabularyRef {
  vocabulary_id: string;
  vocabulary_name: string;
  is_local?: boolean;
}

export interface DomainRef {
  domain_id: string;
  label: string;
}

export interface SourceCodeSystemRef {
  vocabulary_id: string;
  label: string;
}

export interface SourceVocabularyTab {
  vocabulary_id: string;
  label: string;
  is_standard: boolean;
}

export interface Reference {
  suggest_max_per_run?: number;
  release_commit?: string;
  domains: DomainRef[];
  source_code_systems_by_domain: Record<string, SourceCodeSystemRef[]>;
  source_catalog_vocabularies?: string[];
  destination_vocabularies: VocabularyRef[];
  omop_tables: Record<string, string>;
  source_vocabulary_tabs?: SourceVocabularyTab[];
}

export interface RepointResult {
  rows_updated: number;
  persons_marked_stale: number;
  rows_collapsed: number;
}

export interface MappingForm {
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
  source_unit: string;
  source_metadata_notes: string;
  destination_unit_concept_id: string;
  destination_unit_concept_code: string;
  destination_unit_concept_name: string;
  unit_match_type: "exact" | "close" | "none" | "";
}

// ── Constants ───────────────────────────────────────────────────────

export const emptyForm: MappingForm = {
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
  source_unit: "",
  source_metadata_notes: "",
  destination_unit_concept_id: "",
  destination_unit_concept_code: "",
  destination_unit_concept_name: "",
  unit_match_type: "",
};

export const emptyReference: Reference = {
  domains: [],
  source_code_systems_by_domain: {},
  source_catalog_vocabularies: [],
  destination_vocabularies: [],
  omop_tables: {},
};

export const statusClass: Record<string, string> = {
  proposed: "bg-amber-100 text-amber-800",
  approved: "bg-green-100 text-green-800",
  rejected: "bg-red-100 text-red-800",
  unmapped: "bg-amber-100 text-amber-800",
};

export const strategyLabel: Record<string, string> = {
  umls: "UMLS",
  vectors: "Vectors",
  lexical: "Lexical",
  semantic: "Vectors",  // legacy alias
  metadata: "Source metadata",
};

/** Pipeline order, which is also the order the checkboxes read in. */
export const STRATEGY_LABELS = {
  umls: "UMLS",
  lexical: "Lexical",
  vectors: "Vectors",
} as const;

/**
 * OMOP domain -> the clinical table its facts land in. Only a fallback: the
 * reference endpoint is authoritative, and hardcoding the mapping in the
 * frontend is what this table exists to avoid. It covers the window before
 * the first fetch resolves.
 */
export const DOMAIN_TO_TABLE: Record<string, string> = {
  Condition: "condition",
  Drug: "drug_exposure",
  Measurement: "measurement",
  Observation: "observation",
  Procedure: "procedure",
};

// Long enough that a word typed at normal speed is one request, short enough
// that the pause is not felt once the curator stops.
export const CONCEPT_SEARCH_DEBOUNCE_MS = 250;
export const SUGGEST_POLL_INTERVAL_MS = 1000;
// Consecutive, not cumulative: a run lasting minutes may lose the odd poll.
export const SUGGEST_POLL_MAX_FAILURES = 5;
// Allow the worker's default 15-minute limit plus time waiting in the queue.
export const SUGGEST_POLL_TIMEOUT_MS = 20 * 60 * 1000;

/** Tooltip copy, verbatim from the design (plan section 3.1). */
export const TIP = {
  domain:
    "What kind of fact this is. Chosen first: it decides which code systems are offered and which OMOP table the fact lands in.",
  source_code_system:
    "The external code system the value arrived in -- NDC or ATC for drugs, ICD-10-CM or SNOMED for conditions. Leave as None for uncoded data; a parsed paper lab or a phrase from a note has no code system, which is normal.",
  source_code_value:
    "Exactly what appears in the source data -- the code if there is one, otherwise the raw text.",
  source_description:
    "Human-readable description of the source code, where the source supplies one.",
  destination_concept_id:
    "The OMOP concept this source code means. Type an id directly or pick one from the search above.",
  destination_concept_name:
    "Name of the destination concept. Editable only for a HealthKey-minted concept; Athena concepts are named by Athena.",
  destination_concept_code:
    "The destination concept's own code in its vocabulary, e.g. 33358-3.",
  destination_vocabulary_id:
    "Vocabulary the destination concept belongs to -- SNOMED, LOINC, or an HK-* vocabulary when we minted it.",
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
    "Proposed is awaiting review. Approving also re-points the clinical rows already stored. Rejected hides the row behind a filter. Only org admins and staff can approve mappings -- doctors and analysts may propose mappings for review.",
  status_new:
    "A new mapping always starts as Proposed. Only org admins and staff can approve it once reviewed -- approval is what rewrites the clinical rows already stored.",
  notes: "Why this decision was made, for the next curator who opens the row.",
  ranker: "Which AI model ranks the candidates. Anthropic uses Claude, Jev uses the Typesafe SystemOne API. Both runs both concurrently and picks the higher-confidence winner.",
} as const;

// ── Helpers ─────────────────────────────────────────────────────────

export function omopTableFor(reference: Reference, domainId: string): string {
  if (!domainId) return "";
  return reference.omop_tables[domainId] || DOMAIN_TO_TABLE[domainId] || "";
}

export function retirementDetail(row: CodeMappingRow | null): string {
  return row?.source_retirement_evidence?.join("; ")
    || (row?.source_retired === false ? "No retirement indication in the loaded source metadata."
      : "No source retirement metadata available. Missing metadata does not mean the code is retired.");
}

/**
 * The sign-off half of the provenance line: " . approved by ada@x on 2026-08-31".
 *
 * Empty until a mapping has actually been approved.
 */
export function approvalNote(row: CodeMappingRow): string {
  if (row.status !== "approved") return "";
  if (!row.reviewer && !row.reviewed_at) return "";
  const who = row.reviewer ? ` by ${row.reviewer}` : "";
  const when = row.reviewed_at
    ? ` on ${new Date(row.reviewed_at).toLocaleDateString()}`
    : "";
  return ` \u00b7 approved${who}${when}`;
}

export function buildEditForm(row: CodeMappingRow, reference: Reference): MappingForm {
  const domainId = row.domain_id || row.destination_domain_id || "";
  return {
    domain_id: domainId,
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
    source_unit: row.source_unit || "",
    source_metadata_notes: row.source_metadata && Object.keys(row.source_metadata).length
      ? Object.entries(row.source_metadata).map(([k, v]) => `${k}: ${v}`).join("\n")
      : "",
    destination_unit_concept_id: row.destination_unit_concept_id ? String(row.destination_unit_concept_id) : "",
    destination_unit_concept_code: "",
    destination_unit_concept_name: "",
    unit_match_type: "",
  };
}

/** Progress of one queued Suggest run, as /suggest-runs/<id>/ reports it. */
export type SuggestRunProgress = {
  activity?: CandidateActivity[];
  run_id: string;
  state: "queued" | "running" | "success" | "failure";
  total: number;
  retrieved: number;
  done: number;
  /** New destinations written -- what the run achieved, and the headline number. */
  destinations: number;
  /** Codes still awaiting a suggestion on this tab once the run finished. */
  remaining: number;
  strategy_counts: Record<string, number>;
  landed_in: Record<string, number>;
  error: string;
  ranking_model?: string;
};

/**
 * Extract the best usable UCUM unit string from source evidence units.
 * Picks the highest-count non-suppressed unit, preferring normalized > code > display.
 * Returns "" when no usable unit exists.
 */
export function dominantSourceUnit(units: SourceUnitEvidence[] | undefined): string {
  if (!units || units.length === 0) return "";
  const eligible = units.filter((u) => !u.suppressed);
  if (eligible.length === 0) return "";
  // Already sorted by count descending from the backend, but be explicit.
  const best = eligible.reduce((a, b) => (b.count > a.count ? b : a));
  return best.normalized || best.code || best.display || "";
}

// Re-export the CandidateActivity type from SuggestCandidates for convenience.
import type { CandidateActivity } from "./SuggestCandidates";
export type { CandidateActivity };
