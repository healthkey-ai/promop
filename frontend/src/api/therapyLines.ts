import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import type { TherapyRegimen } from '@/types/therapy';

export interface DrugConcept {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  concept_class_id?: string;
  standard_concept?: string | null;
}

export interface TherapyLineDrug {
  concept_id: number;
  source_value?: string | null;
}

export interface TherapyLinePayload {
  person: number;
  line_number: number;
  start_date?: string | null;
  end_date?: string | null;
  drugs: TherapyLineDrug[];
  regimen_concept_id?: number | null;
  outcome?: string | null;
  intent?: string | null;
  discontinuation_reason?: string | null;
}

export interface TherapyLineResult {
  episode_id: number;
  line_number: number;
  created: boolean;
  drug_exposure_ids: number[];
  drugs_created: number;
  patient_info: Record<string, unknown>;
}

export interface EditableTherapyLine {
  line: number;
  episode_id?: number | null;
  start_date?: string | null;
  end_date?: string | null;
  outcome?: string | null;
  intent?: string | null;
  discontinuation_reason?: string | null;
  regimen?: string | null;
  regimen_concept_id?: number | null;
  drugs?: Array<DrugConcept & { source_value?: string | null }>;
}

export interface TherapyOutcomeChoice { code: string; value: string; label: string }

export async function listTherapyOutcomes(disease?: string): Promise<TherapyOutcomeChoice[]> {
  const response = await clinicalClient().get(clinicalUrl('/v1/therapy-outcomes/'), { params: { disease } });
  return response.data;
}

export const THERAPY_INTENT_CHOICES: Array<{ value: string; label: string }> = [
  { value: 'Curative', label: 'Curative' },
  { value: 'Palliative', label: 'Palliative' },
  { value: 'Adjuvant', label: 'Adjuvant' },
  { value: 'Neoadjuvant', label: 'Neoadjuvant' },
  { value: 'Maintenance', label: 'Maintenance' },
  { value: 'Salvage', label: 'Salvage' },
];

export const DISCONTINUATION_REASON_CHOICES: Array<{ value: string; label: string }> = [
  { value: 'Progressive Disease', label: 'Progressive Disease' },
  { value: 'Toxicity', label: 'Toxicity' },
  { value: 'Patient Decision', label: 'Patient Decision' },
  { value: 'Completed Protocol', label: 'Completed Protocol' },
  { value: 'Other', label: 'Other' },
];

/** Ingredients only: a line is the drugs given, not their branded pack sizes. */
export async function searchDrugConcepts(query: string): Promise<DrugConcept[]> {
  // The server rejects shorter queries — a trigram is 3 characters, so anything
  // less cannot use the index and would seq-scan the concept table.
  if (query.trim().length < 3) return [];
  const resp = await clinicalClient().get(clinicalUrl('/v1/concepts/search/'), {
    params: {
      q: query.trim(),
      vocabulary_id: 'RxNorm',
      concept_class_id: 'Ingredient',
      standard_concept: 'S',
      page_size: 20,
    },
  });
  return (resp.data?.results ?? resp.data ?? []) as DrugConcept[];
}

/** Search therapy regimens by name, optionally filtered by disease and round. */
export async function searchTherapyRegimens(
  query: string,
  disease?: string,
  round?: string,
): Promise<TherapyRegimen[]> {
  if (query.trim().length < 2) return [];
  const params: Record<string, string> = { search: query.trim() };
  if (disease) params.disease = disease;
  if (round) params.round = round;
  const resp = await clinicalClient().get(clinicalUrl('/v1/therapy-regimens/'), { params });
  return (resp.data ?? []) as TherapyRegimen[];
}

/** List all regimens for a disease+round combination (no search query needed). */
export async function listTherapyRegimens(
  disease: string,
  round?: string,
): Promise<TherapyRegimen[]> {
  const params: Record<string, string> = { disease };
  if (round) params.round = round;
  const resp = await clinicalClient().get(clinicalUrl('/v1/therapy-regimens/'), { params });
  return (resp.data ?? []) as TherapyRegimen[];
}

/** Fetch a single regimen with its component drugs and their classes. */
export async function getTherapyRegimenDetail(code: string): Promise<TherapyRegimen> {
  const resp = await clinicalClient().get(clinicalUrl(`/v1/therapy-regimens/${code}/`));
  return resp.data as TherapyRegimen;
}

export async function authorTherapyLine(
  payload: TherapyLinePayload,
): Promise<TherapyLineResult> {
  const resp = await clinicalClient().post(
    clinicalUrl('/v1/therapy-lines/'),
    payload,
  );
  return resp.data as TherapyLineResult;
}

export async function updateTherapyLine(
  episodeId: number,
  payload: TherapyLinePayload,
): Promise<TherapyLineResult> {
  const resp = await clinicalClient().patch(
    clinicalUrl(`/v1/therapy-lines/${episodeId}/`),
    payload,
  );
  return resp.data as TherapyLineResult;
}
