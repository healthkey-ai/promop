import api from '@/api/axios';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

/**
 * Write a clinical value as an OMOP fact.
 *
 * PatientRecord is derived and has no writable clinical columns, so an edit is a
 * write to `measurement` or `observation` followed by derivation, which fires from
 * the row's post_save signal. Nothing here touches PatientRecord.
 */

/** The event date for a manual edit. Defaults to today; the caller may override,
 *  because a result drawn last month and entered today would otherwise sort as
 *  the most recent value — and "most recent" is what eligibility screening reads. */
export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function valueFields(descriptor: FieldDescriptor, value: unknown) {
  if (descriptor.value_kind === 'number') {
    return { value_as_number: value === '' || value == null ? null : Number(value) };
  }
  // Booleans and coded answers both land in value_as_string here. value_as_concept
  // needs a resolved concept for the ANSWER, not just the question, and the
  // descriptor does not carry an answer set yet — writing an unresolved concept
  // would be worse than keeping the raw text, which derivation already reads.
  return { value_as_string: value == null ? null : String(value) };
}

export interface WriteResult {
  supersededId: number | null;
  createdId: number | null;
}

/**
 * Write `value` for `field`, superseding any fact already recorded on that date.
 *
 * Corrections mark the prior row `is_erroneous` and insert the replacement beside
 * it rather than overwriting: the entered-in-error columns exist for exactly this,
 * the audit trail keeps what was superseded, and the upsert key includes the value
 * so an overwrite was never on offer anyway.
 *
 * Superseding is done here rather than server-side on purpose. The OMOP endpoints
 * are shared with ETL and FHIR ingest, where two results for one analyte on one day
 * are legitimately two results, not a correction. Only an editing UI knows the
 * second one means "I got it wrong".
 */
export async function writeClinicalFact(
  personId: number | string,
  field: string,
  descriptor: FieldDescriptor,
  value: unknown,
  date: string = today(),
): Promise<WriteResult> {
  if (!descriptor?.writable || !descriptor.target) {
    throw new Error(`${field} is not writable`);
  }
  const isMeasurement = descriptor.target === 'measurement';
  const base = isMeasurement ? '/v1/measurements/' : '/v1/observations/';
  const dateField = isMeasurement ? 'measurement_date' : 'observation_date';
  const sourceField = isMeasurement
    ? 'measurement_source_value'
    : 'observation_source_value';
  const conceptField = isMeasurement ? 'measurement_concept' : 'observation_concept';
  const typeField = isMeasurement
    ? 'measurement_type_concept'
    : 'observation_type_concept';

  // Find a fact already recorded for this analyte on this date.
  let supersededId: number | null = null;
  try {
    const existing = await api.get(base, {
      params: { person: personId, [sourceField]: descriptor.source_value },
    });
    const rows = Array.isArray(existing.data)
      ? existing.data
      : existing.data?.results ?? [];
    const sameDay = rows.find(
      (r: Record<string, unknown>) => r[dateField] === date && !r.is_erroneous,
    );
    if (sameDay) {
      supersededId = (sameDay.measurement_id ?? sameDay.observation_id) as number;
    }
  } catch {
    // A failed lookup must not block the write. Worst case we insert a second
    // row for the day, which is recoverable; losing the edit is not.
    supersededId = null;
  }

  if (supersededId != null) {
    await api.patch(`${base}${supersededId}/`, {
      is_erroneous: true,
      erroneous_reason: 'Superseded by a corrected value entered in the patient editor',
    });
  }

  const payload: Record<string, unknown> = {
    person: personId,
    [conceptField]: descriptor.concept_id,
    [dateField]: date,
    [typeField]: descriptor.type_concept_id,
    [sourceField]: descriptor.source_value,
    ...valueFields(descriptor, value),
  };
  if (isMeasurement && descriptor.unit_concept_id) {
    payload.unit_concept = descriptor.unit_concept_id;
  }
  if (isMeasurement && descriptor.unit) {
    payload.unit_source_value = descriptor.unit;
  }

  const created = await api.post(base, payload);
  const createdId =
    (created.data?.measurement_id ?? created.data?.observation_id ?? null) as
      | number
      | null;
  return { supersededId, createdId };
}
