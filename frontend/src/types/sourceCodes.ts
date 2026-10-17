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
  outcome: "resolved" | "created" | "skipped" | "already_resolved";
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
  skipped: number;
  already_resolved: number;
  errors: number;
  code_details: ResolveCodeDetail[];
}
