import api from '@/api/axios';

export interface DestinationConcept {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  domain_id: string;
  concept_class_id: string;
  standard_concept: string | null;
  invalid_reason?: string | null;
  measurement_type?: 'qualitative' | 'quantitative';
  suggested_unit?: string;
}

export interface SavedMapping {
  mapping_id: number;
  destination_concept_id: number | null;
  status: string;
  mapping_origin?: string;
}

export async function searchDestinationConcepts(
  query: string,
  vocabulary: string,
  signal: AbortSignal,
  scope = { retired: false, nonStandard: false },
): Promise<DestinationConcept[]> {
  const params: Record<string, string> = { q: query.trim(), limit: '25' };
  if (vocabulary) params.vocabulary_id = vocabulary;
  if (scope.retired) params.include_retired = 'true';
  if (scope.nonStandard) params.include_non_standard = 'true';
  const { data } = await api.get('/v1/concepts/search/', { params, signal });
  return data.results || data || [];
}

export function destinationError(error: unknown): string {
  const response = (error as { response?: { status?: number; data?: { detail?: string; locked_by?: string } } })?.response;
  if (response?.status === 423) return `Locked by ${response.data?.locked_by || 'another user'}. Try again after they finish.`;
  return response?.data?.detail || (error instanceof Error ? error.message : 'Could not save the destination. Please try again.');
}

/** Lock only around the write, then release on success or failure. */
export async function saveMappingDestination(
  mappingId: number,
  conceptId: number,
  approve = false,
  expectedDestinationId?: number | null,
): Promise<SavedMapping> {
  const path = `/v1/code-mappings/${mappingId}/`;
  await api.post(`${path}lock/`);
  try {
    const { data: current } = await api.get<SavedMapping>(path);
    if (current.status === 'approved' || current.mapping_origin === 'athena') {
      throw new Error('This mapping is already approved. Open the full editor to review it.');
    }
    if (expectedDestinationId !== undefined && current.destination_concept_id !== expectedDestinationId) {
      throw new Error('The destination changed while you were searching. Close and reopen the picker to review the latest mapping.');
    }
    // A partial write preserves the source, domain, notes and other curation.
    const { data } = await api.patch<SavedMapping>(path, {
      destination_concept_id: conceptId,
      status: approve ? 'approved' : 'proposed',
    });
    return data;
  } finally {
    // A temporary unlock failure must not misreport a successful save. The
    // server also expires abandoned locks, as it does for the full editor.
    await api.delete(`${path}lock/`).catch(() => undefined);
  }
}
