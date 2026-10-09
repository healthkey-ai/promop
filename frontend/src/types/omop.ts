import type { NormalizedMeasurement } from '@/utils/normalizedLabs';
/** Row types returned by the five OMOP clinical CRUD endpoints. */

export interface ConditionRow {
  condition_occurrence_id: number;
  person: number;
  condition_concept: number | null;
  condition_start_date: string | null;
  condition_start_datetime: string | null;
  condition_end_date: string | null;
  condition_end_datetime: string | null;
  condition_type_concept: number | null;
  condition_status_concept: number | null;
  stop_reason: string | null;
  condition_source_value: string | null;
  condition_source_concept: number | null;
  condition_status_source_value: string | null;
  concept_name?: string | null;
  is_erroneous: boolean;
  erroneous_reason: string | null;
}

export interface DrugExposureRow {
  drug_exposure_id: number;
  person: number;
  drug_concept: number | null;
  drug_exposure_start_date: string | null;
  drug_exposure_start_datetime: string | null;
  drug_exposure_end_date: string | null;
  drug_exposure_end_datetime: string | null;
  drug_type_concept: number | null;
  stop_reason: string | null;
  quantity: number | null;
  days_supply: number | null;
  route_concept: number | null;
  lot_number: string | null;
  drug_source_value: string | null;
  drug_source_concept: number | null;
  route_source_value: string | null;
  dose_unit_source_value: string | null;
  concept_name?: string | null;
  is_erroneous: boolean;
  erroneous_reason: string | null;
}

export interface MeasurementRow {
  normalized?: NormalizedMeasurement | null;
  measurement_id: number;
  person: number;
  measurement_concept: number | null;
  measurement_date: string | null;
  measurement_datetime: string | null;
  measurement_type_concept: number | null;
  operator_concept: number | null;
  value_as_number: number | null;
  value_as_string: string | null;
  value_as_concept: number | null;
  unit_concept: number | null;
  range_low: number | null;
  range_high: number | null;
  measurement_source_value: string | null;
  measurement_source_concept: number | null;
  unit_source_value: string | null;
  value_source_value: string | null;
  concept_name?: string | null;
  is_erroneous: boolean;
  erroneous_reason: string | null;
}

export interface ObservationRow {
  observation_id: number;
  person: number;
  observation_concept: number | null;
  observation_date: string | null;
  observation_datetime: string | null;
  observation_type_concept: number | null;
  value_as_number: number | null;
  value_as_string: string | null;
  value_as_concept: number | null;
  qualifier_concept: number | null;
  unit_concept: number | null;
  observation_source_value: string | null;
  observation_source_concept: number | null;
  unit_source_value: string | null;
  qualifier_source_value: string | null;
  value_source_value: string | null;
  concept_name?: string | null;
  is_erroneous: boolean;
  erroneous_reason: string | null;
}

export interface ProcedureRow {
  procedure_occurrence_id: number;
  person: number;
  procedure_concept: number | null;
  procedure_date: string | null;
  procedure_datetime: string | null;
  procedure_end_date: string | null;
  procedure_end_datetime: string | null;
  procedure_type_concept: number | null;
  modifier_concept: number | null;
  quantity: number | null;
  procedure_source_value: string | null;
  procedure_source_concept: number | null;
  modifier_source_value: string | null;
  concept_name?: string | null;
  is_erroneous: boolean;
  erroneous_reason: string | null;
}
