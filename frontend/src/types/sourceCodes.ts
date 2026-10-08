export interface PatientSourceCode {
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

export interface ResolveRunStatus {
  run_id: string;
  state: "pending" | "running" | "completed" | "failed";
  total: number;
  done: number;
  resolved: number;
  errors: number;
}
