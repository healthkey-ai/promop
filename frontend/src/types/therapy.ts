/** Therapy reference data types — matches API response shapes. */

export interface TherapyClassDetail {
  code: string;
  title: string;
  concept_id?: number | null;
}

export interface TherapyComponentDetail {
  code: string;
  title: string;
  concept_id?: number | null;
  /** Resolved from linked Concept row (detail endpoint only). */
  concept_name?: string;
  concept_code?: string;
  vocabulary_id?: string | null;
  classes?: TherapyClassDetail[];
}

export interface TherapyRegimen {
  code: string;
  title: string;
  concept_id?: number | null;
  components?: TherapyComponentDetail[];
}
