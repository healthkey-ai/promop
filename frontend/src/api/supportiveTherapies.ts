import { clinicalClient, clinicalUrl } from './clinicalTransport';

export interface SupportiveTherapyCourse {
  id: number;
  person: number;
  regimen_code: string;
  regimen_title: string;
  start_date: string | null;
  end_date: string | null;
  intent: string;
  discontinuation_reason: string;
}

export type SupportiveTherapyPayload = Omit<SupportiveTherapyCourse, 'id' | 'regimen_title'>;

export async function saveSupportiveTherapy(payload: SupportiveTherapyPayload, id?: number) {
  const response = id
    ? await clinicalClient().patch(clinicalUrl(`/v1/supportive-therapies/${id}/`), payload)
    : await clinicalClient().post(clinicalUrl('/v1/supportive-therapies/'), payload);
  return response.data as { course: SupportiveTherapyCourse; patient_info: Record<string, unknown> };
}
