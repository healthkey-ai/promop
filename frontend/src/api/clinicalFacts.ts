import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

/**
 * Write a clinical value as an OMOP fact.
 *
 * PatientRecord is derived and has no writable clinical columns, so an edit is a
 * write to an OMOP clinical endpoint followed by derivation, which fires from
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

const CLINICAL_TARGETS = {
  measurement: {
    base: '/v1/measurements/',
    idField: 'measurement_id',
    conceptField: 'measurement_concept',
    dateField: 'measurement_date',
    typeField: 'measurement_type_concept',
    sourceField: 'measurement_source_value',
    storesValue: true,
    storesUnit: true,
  },
  observation: {
    base: '/v1/observations/',
    idField: 'observation_id',
    conceptField: 'observation_concept',
    dateField: 'observation_date',
    typeField: 'observation_type_concept',
    sourceField: 'observation_source_value',
    storesValue: true,
    storesUnit: true,
  },
  condition: {
    base: '/v1/conditions/',
    idField: 'condition_occurrence_id',
    conceptField: 'condition_concept',
    dateField: 'condition_start_date',
    typeField: 'condition_type_concept',
    sourceField: 'condition_source_value',
    storesValue: false,
    storesUnit: false,
  },
  drug_exposure: {
    base: '/v1/drug-exposures/',
    idField: 'drug_exposure_id',
    conceptField: 'drug_concept',
    dateField: 'drug_exposure_start_date',
    typeField: 'drug_type_concept',
    sourceField: 'drug_source_value',
    storesValue: false,
    storesUnit: false,
  },
  procedure: {
    base: '/v1/procedures/',
    idField: 'procedure_occurrence_id',
    conceptField: 'procedure_concept',
    dateField: 'procedure_date',
    typeField: 'procedure_type_concept',
    sourceField: 'procedure_source_value',
    storesValue: false,
    storesUnit: false,
  },
} as const;

type ClinicalTarget = keyof typeof CLINICAL_TARGETS;

function clinicalTarget(target: FieldDescriptor['target']): ClinicalTarget | null {
  return target && target in CLINICAL_TARGETS
    ? target as ClinicalTarget
    : null;
}

function isEmptyOccurrenceValue(value: unknown): boolean {
  return value === '' || value == null || value === false;
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
  // Refuse a target this cannot write, rather than falling through.
  //
  // The check used to be `!descriptor.target`, so `target: 'person'` passed it and
  // then failed the `=== 'measurement'` test, landing in the observation branch:
  // a profile edit POSTed an Observation whose concept, type and source value were
  // all undefined, and never touched Person. Sixteen writable fields — gender,
  // race, ethnicity, the six location columns — take that target.
  const target = clinicalTarget(descriptor.target);
  if (target === null) {
    throw new Error(
      `${field} writes to ${descriptor.target}, not an OMOP fact — use writeFieldValue`,
    );
  }
  const cfg = CLINICAL_TARGETS[target];

  // Find a fact already recorded for this analyte on this date.
  //
  // `person_id` is the only filter this endpoint honours — `person` and a
  // source-value param are ignored, and an ignored filter returns the WHOLE
  // measurement table. Matching on date alone across that would supersede an
  // unrelated patient's unrelated result, so the analyte and person are both
  // re-checked here rather than trusted to the query. The server already
  // excludes entered-in-error rows; the is_erroneous check is belt and braces.
  let supersededId: number | null = null;
  try {
    const existing = await clinicalClient().get(clinicalUrl(cfg.base), {
      params: { person_id: personId },
    });
    const rows = Array.isArray(existing.data)
      ? existing.data
      : existing.data?.results ?? [];
    const sameDay = rows.find(
      (r: Record<string, unknown>) =>
        String(r.person) === String(personId) &&
        r[cfg.sourceField] === descriptor.source_value &&
        r[cfg.dateField] === date &&
        !r.is_erroneous,
    );
    if (sameDay) {
      supersededId = sameDay[cfg.idField] as number;
    }
  } catch {
    // A failed lookup must not block the write. Worst case we insert a second
    // row for the day, which is recoverable; losing the edit is not.
    supersededId = null;
  }

  if (supersededId != null) {
    await clinicalClient().patch(clinicalUrl(`${cfg.base}${supersededId}/`), {
      is_erroneous: true,
      erroneous_reason: 'Superseded by a corrected value entered in the patient editor',
    });
  }

  if (!cfg.storesValue && isEmptyOccurrenceValue(value)) {
    return { supersededId, createdId: null };
  }

  const payload: Record<string, unknown> = {
    person: personId,
    [cfg.conceptField]: descriptor.concept_id,
    [cfg.dateField]: date,
    [cfg.typeField]: descriptor.type_concept_id,
    [cfg.sourceField]: descriptor.source_value,
  };
  if (cfg.storesValue) {
    Object.assign(payload, valueFields(descriptor, value));
  }
  if (cfg.storesUnit && descriptor.unit_concept_id) {
    payload.unit_concept = descriptor.unit_concept_id;
  }
  if (cfg.storesUnit && descriptor.unit) {
    payload.unit_source_value = descriptor.unit;
  }

  const created = await clinicalClient().post(clinicalUrl(cfg.base), payload);
  const createdId = (created.data?.[cfg.idField] ?? null) as number | null;
  return { supersededId, createdId };
}

/**
 * Write a profile field to the Person record.
 *
 * Demographics are stored as a resolved concept plus the raw text, and the
 * endpoint does that resolution — so the payload key is the PatientRecord field
 * name, not either Person column. `payload_field` carries it; `person_field`
 * beside it is prose documenting the columns behind the value ("gender_concept +
 * gender_source_value", "Location.city") and is not a key.
 */
export async function writeProfileField(
  personId: number | string,
  field: string,
  descriptor: FieldDescriptor,
  value: unknown,
): Promise<void> {
  if (!descriptor?.writable || descriptor.target !== 'person') {
    throw new Error(`${field} is not a writable profile field`);
  }
  const key = descriptor.payload_field ?? field;
  await clinicalClient().patch(clinicalUrl(`/v1/persons/${personId}/`), {
    [key]: value === '' ? null : value,
  });
}

/**
 * Write one edited field to wherever the descriptor says it lives.
 *
 * Editors should call this rather than picking a writer themselves: `writable`
 * alone does not say where a value goes, and treating every writable field as an
 * OMOP fact is what sent profile edits to the observation endpoint.
 */
export async function writeFieldValue(
  personId: number | string,
  field: string,
  descriptor: FieldDescriptor,
  value: unknown,
  date?: string,
): Promise<void> {
  if (descriptor?.target === 'person') {
    await writeProfileField(personId, field, descriptor, value);
    return;
  }
  await writeClinicalFact(personId, field, descriptor, value, date ?? today());
}
