import type { AxiosInstance } from 'axios';

import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import type { TherapyRegimen } from '@/types/therapy';

/** Which connection the therapy helpers talk through.
 *
 * The standalone app leaves this out and uses the module-level transport, which
 * its provider owns. A federated host passes its own authenticated client and
 * base path instead: two mounted editors can then never inherit each other's
 * connection, which a single module-level transport cannot promise.
 */
export interface TherapyConnection {
  client: AxiosInstance;
  /** Prefix before `/v1/…`; `""` when the client's baseURL ends at the API root. */
  basePath?: string;
}

function target(path: string, connection?: TherapyConnection) {
  if (!connection) return { client: clinicalClient(), url: clinicalUrl(path) };
  const base = (connection.basePath ?? '').replace(/\/+$/, '');
  return { client: connection.client, url: `${base}${path}` };
}

export interface DrugConcept {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  concept_class_id?: string;
  standard_concept?: string | null;
}

export interface TherapyLineDrug {
  /** Existing administration, scoped by the server to the edited line. */
  drug_exposure_id?: number;
  concept_id: number;
  source_value?: string | null;
  start_date?: string;
  end_date?: string | null;
  maintenance?: boolean;
}

export interface TherapyLinePayload {
  person: number;
  line_number: number;
  start_date?: string | null;
  end_date?: string | null;
  drugs: TherapyLineDrug[];
  /** Record/keep the line without claiming which drugs were given. */
  composition_unknown?: boolean;
  regimen_concept_id?: number | null;
  outcome?: string | null;
  intent?: string | null;
  discontinuation_reason?: string | null;
  progression_date?: string | null;
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
  /** False when the line is recorded without its drug composition (#1924). */
  composition_known?: boolean;
  drugs?: Array<DrugConcept & TherapyLineDrug>;
  progression_date?: string | null;
}

export interface TherapyOutcomeChoice { code: string; value: string; label: string }

export async function listTherapyOutcomes(
  disease?: string,
  connection?: TherapyConnection,
): Promise<TherapyOutcomeChoice[]> {
  const { client, url } = target('/v1/therapy-outcomes/', connection);
  const response = await client.get(url, { params: { disease } });
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
export async function searchDrugConcepts(
  query: string,
  connection?: TherapyConnection,
): Promise<DrugConcept[]> {
  // The server rejects shorter queries — a trigram is 3 characters, so anything
  // less cannot use the index and would seq-scan the concept table.
  if (query.trim().length < 3) return [];
  const { client, url } = target('/v1/concepts/search/', connection);
  const resp = await client.get(url, {
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
  connection?: TherapyConnection,
): Promise<TherapyRegimen[]> {
  if (query.trim().length < 2) return [];
  const params: Record<string, string> = { search: query.trim() };
  if (disease) params.disease = disease;
  if (round) params.round = round;
  const { client, url } = target('/v1/therapy-regimens/', connection);
  const resp = await client.get(url, { params });
  return (resp.data ?? []) as TherapyRegimen[];
}

/** List all regimens for a disease+round combination (no search query needed). */
export async function listTherapyRegimens(
  disease: string,
  round?: string,
  connection?: TherapyConnection,
): Promise<TherapyRegimen[]> {
  const params: Record<string, string> = { disease };
  if (round) params.round = round;
  const { client, url } = target('/v1/therapy-regimens/', connection);
  const resp = await client.get(url, { params });
  return (resp.data ?? []) as TherapyRegimen[];
}

/** Fetch a single regimen with its component drugs and their classes. */
export async function getTherapyRegimenDetail(
  code: string,
  connection?: TherapyConnection,
): Promise<TherapyRegimen> {
  const { client, url } = target(`/v1/therapy-regimens/${code}/`, connection);
  const resp = await client.get(url);
  return resp.data as TherapyRegimen;
}

export async function authorTherapyLine(
  payload: TherapyLinePayload,
  connection?: TherapyConnection,
): Promise<TherapyLineResult> {
  const { client, url } = target('/v1/therapy-lines/', connection);
  const resp = await client.post(url, payload);
  return resp.data as TherapyLineResult;
}

export async function updateTherapyLine(
  episodeId: number,
  payload: TherapyLinePayload,
  connection?: TherapyConnection,
): Promise<TherapyLineResult> {
  const { client, url } = target(`/v1/therapy-lines/${episodeId}/`, connection);
  const resp = await client.patch(url, payload);
  return resp.data as TherapyLineResult;
}

/** Withdraw the claim that this line happened (not the same as clearing its drugs). */
export async function retractTherapyLine(
  episodeId: number,
  connection?: TherapyConnection,
): Promise<TherapyLineResult> {
  const { client, url } = target(`/v1/therapy-lines/${episodeId}/`, connection);
  const resp = await client.delete(url);
  return resp.data as TherapyLineResult;
}

/** Confirm, or withdraw, that the recorded lines are the patient's full history. */
export async function setTherapyHistoryCompleteness(
  personId: number,
  complete: boolean,
  connection?: TherapyConnection,
): Promise<{ therapy_history_complete: boolean | null; patient_info: Record<string, unknown> }> {
  const { client, url } = target('/v1/therapy-history/completeness/', connection);
  const resp = await client.post(url, {
    person: personId,
    complete,
  });
  return resp.data;
}
