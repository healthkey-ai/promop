/**
 * What a device's code says about its stored rows (#2835): how many are
 * accepted (at an approved mapping's concept), provisional (given a concept by
 * a proposed mapping) or unresolved (stored at concept 0, waiting for a
 * mapping). Counts and dates only.
 */
export interface SourceCodeWearable {
  device: "garmin" | "apple" | "android" | "oura";
  /** "Garmin", "Apple Health", "Health Connect" or "Oura Ring". */
  source: string;
  /** The vendor-neutral metric, e.g. "steps"; '' when unknown. */
  metric_key: string;
  rows: number;
  accepted_rows: number;
  provisional_rows: number;
  unresolved_rows: number;
  /** The lowest confidence percent among the provisional rows; null when there are none. */
  confidence: number | null;
  first_date: string | null;
  last_date: string | null;
  status: "accepted" | "provisional" | "unresolved" | "mixed" | "none";
  /** The 30-day record metrics this code feeds. */
  fields: { name: string; label: string }[];
}

export interface PatientSourceCode {
  source_code_id?: number;
  source_value: string;
  omop_table: string;
  concept_id: number;
  concept_name: string | null;
  row_count: number;
  mapping_id: number | null;
  mapping_status: "approved" | "proposed" | "unmapped";
  mapping_target_concept_id: number | null;
  mapping_target_concept_name: string | null;
  source_vocabulary_id: string;
  source_code: string;
  source_unit: string;
  example_quantity: string;
  based_on?: string[];
  extracted_source_unit?: string;
  extracted_example_quantity?: string;
  source_unit_override?: string | null;
  example_quantity_override?: string | null;
  source_metadata: Record<string, unknown>;
  /** The mapping's description, else the source's own name from source_metadata. */
  source_code_description?: string;
  mapping_origin?: string;
  mapping_origin_system?: string;
  mapping_created_by?: string;
  mapping_destination_concept_code?: string;
  mapping_destination_vocabulary_id?: string;
  /** Present on a wearable device's code only; absent on a hospital code and an older server. */
  wearable?: SourceCodeWearable;
}

/** A source code that is a wearable device's. */
export type WearableSourceCode = PatientSourceCode & { wearable: SourceCodeWearable };

export function isWearableSourceCode(code: PatientSourceCode): code is WearableSourceCode {
  return code.wearable != null;
}

export interface SourceCodesSummary {
  total: number;
  unmapped: number;
  proposed: number;
  approved: number;
}

export interface SourceCodesResponse {
  person_id: number;
  source_codes: PatientSourceCode[];
  summary: SourceCodesSummary;
}

export interface ResolveResult {
  resolved: number;
  skipped: number;
  already_resolved: number;
}

export interface ResolveCodeDetail {
  source_value: string;
  table: string;
  rows: number;
  outcome: "resolved" | "created" | "provisional" | "reverted" | "skipped" | "already_resolved";
  reason?: string;
  target?: string;
  target_concept_id?: number;
}

export interface ResolveRunStatus {
  run_id: string;
  state: "pending" | "running" | "completed" | "failed";
  total: number;
  done: number;
  resolved: number;
  created: number;
  provisional: number;
  reverted: number;
  skipped: number;
  already_resolved: number;
  errors: number;
  /** Of `errors`: patients whose rows were resolved and whose record could not
   *  then be derived. Only a patient's own Generate OMOP derives (#2822). */
  record_refresh_errors?: number;
  /** Of `errors`: patients another resolver was holding, whose rows this run left alone. */
  busy_errors?: number;
  code_details: ResolveCodeDetail[];
}
