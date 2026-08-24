import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

/**
 * Authoring a line of therapy.
 *
 * Every therapy field on PatientRecord is inferred from an Episode grouping the
 * drug exposures given in a line, so none of them can be written directly — the
 * treatment tab is read-only for that reason. This is the one write that moves
 * them, and the server does the CDM work behind it.
 */

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
  regimen?: string | null;
  drugs?: Array<DrugConcept & { source_value?: string | null }>;
}

/**
 * Outcome values the server can code.
 *
 * `episode_service.OUTCOME_SNOMED_CODES` keys on the bare phrase, while the tab's
 * display constant carries the abbreviation ("Complete Response (CR)"). Sending
 * the label would still store the text but would miss the SNOMED code, so the
 * value sent and the value shown are kept separate here.
 */
export const THERAPY_OUTCOME_CHOICES: Array<{ value: string; label: string }> = [
  { value: 'Complete Response', label: 'Complete Response (CR)' },
  { value: 'Partial Response', label: 'Partial Response (PR)' },
  { value: 'Stable Disease', label: 'Stable Disease (SD)' },
  { value: 'Progressive Disease', label: 'Progressive Disease (PD)' },
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
