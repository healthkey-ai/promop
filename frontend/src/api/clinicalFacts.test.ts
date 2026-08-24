/**
 * Writing a clinical value means writing an OMOP fact, never patching the
 * projection. A correction supersedes rather than overwrites.
 */
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { writeClinicalFact, today, writeFieldValue } from './clinicalFacts';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
vi.mock('@/api/axios', () => ({
  default: {
    get: (...a: unknown[]) => mockGet(...a),
    post: (...a: unknown[]) => mockPost(...a),
    patch: (...a: unknown[]) => mockPatch(...a),
  },
}));

const HGB: FieldDescriptor = {
  kind: 'editable', writable: true, target: 'measurement',
  concept_id: 3000963, code: '718-7', value_kind: 'number',
  unit: 'g/dL', unit_concept_id: 8713, type_concept_id: 32856,
  source_value: '718-7',
};

const OBS: FieldDescriptor = {
  kind: 'editable', writable: true, target: 'observation',
  concept_id: 42, code: '408729009', value_kind: 'string',
  type_concept_id: 32856, source_value: '408729009',
};

const CONDITION: FieldDescriptor = {
  kind: 'editable', writable: true, target: 'condition',
  concept_id: 201826, code: 'C50', value_kind: 'string',
  type_concept_id: 32817, source_value: 'primary-cancer',
};

const DRUG: FieldDescriptor = {
  kind: 'editable', writable: true, target: 'drug_exposure',
  concept_id: 19026972, code: '337535', value_kind: 'string',
  type_concept_id: 32817, source_value: 'maintenance-drug',
};

const PROCEDURE: FieldDescriptor = {
  kind: 'editable', writable: true, target: 'procedure',
  concept_id: 4273629, code: 'SCT', value_kind: 'string',
  type_concept_id: 32817, source_value: 'stem-cell-transplant',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockGet.mockResolvedValue({ data: [] });
  mockPost.mockResolvedValue({ data: { measurement_id: 900 } });
  mockPatch.mockResolvedValue({ data: {} });
});

describe('writeClinicalFact', () => {
  it('posts a complete measurement, not a PatientRecord patch', async () => {
    await writeClinicalFact(3542, 'hemoglobin_g_dl', HGB, 12.5, '2026-08-21');

    expect(mockPost).toHaveBeenCalledWith('/v1/measurements/', {
      person: 3542,
      measurement_concept: 3000963,
      measurement_date: '2026-08-21',
      measurement_type_concept: 32856,
      measurement_source_value: '718-7',
      value_as_number: 12.5,
      unit_concept: 8713,
      unit_source_value: 'g/dL',
    });
  });

  it('routes an observation-domain field to the observation endpoint', async () => {
    mockPost.mockResolvedValue({ data: { observation_id: 7 } });

    await writeClinicalFact(1, 'concomitant_medication_details', OBS, 'aspirin', '2026-08-21');

    expect(mockPost).toHaveBeenCalledWith(
      '/v1/observations/',
      expect.objectContaining({
        observation_concept: 42,
        observation_date: '2026-08-21',
        observation_source_value: '408729009',
        value_as_string: 'aspirin',
      }),
    );
  });

  it('routes a condition-domain mapping to condition_occurrence', async () => {
    mockPost.mockResolvedValue({ data: { condition_occurrence_id: 33 } });

    const res = await writeClinicalFact(1, 'disease', CONDITION, 'Breast Cancer', '2026-08-21');

    expect(mockPost).toHaveBeenCalledWith('/v1/conditions/', {
      person: 1,
      condition_concept: 201826,
      condition_start_date: '2026-08-21',
      condition_type_concept: 32817,
      condition_source_value: 'primary-cancer',
    });
    expect(res.createdId).toBe(33);
  });

  it('routes a drug-domain mapping to drug_exposure', async () => {
    mockPost.mockResolvedValue({ data: { drug_exposure_id: 44 } });

    await writeClinicalFact(1, 'maintenance_therapy', DRUG, 'lenalidomide', '2026-08-21');

    expect(mockPost).toHaveBeenCalledWith('/v1/drug-exposures/', {
      person: 1,
      drug_concept: 19026972,
      drug_exposure_start_date: '2026-08-21',
      drug_type_concept: 32817,
      drug_source_value: 'maintenance-drug',
    });
  });

  it('routes a procedure-domain mapping to procedure_occurrence', async () => {
    mockPost.mockResolvedValue({ data: { procedure_occurrence_id: 55 } });

    await writeClinicalFact(1, 'stem_cell_transplant', PROCEDURE, 'Yes', '2026-08-21');

    expect(mockPost).toHaveBeenCalledWith('/v1/procedures/', {
      person: 1,
      procedure_concept: 4273629,
      procedure_date: '2026-08-21',
      procedure_type_concept: 32817,
      procedure_source_value: 'stem-cell-transplant',
    });
  });

  it('defaults the event date to today', async () => {
    await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 9);

    const [, body] = mockPost.mock.calls.at(-1)!;
    expect(body.measurement_date).toBe(today());
  });

  it('supersedes a same-day value instead of overwriting it', async () => {
    mockGet.mockResolvedValue({
      data: [{ measurement_id: 500, person: 1, measurement_source_value: '718-7',
               measurement_date: '2026-08-21', is_erroneous: false }],
    });

    const res = await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 13.1, '2026-08-21');

    expect(mockPatch).toHaveBeenCalledWith('/v1/measurements/500/', {
      is_erroneous: true,
      erroneous_reason: expect.stringContaining('Superseded'),
    });
    expect(mockPost).toHaveBeenCalled();      // replacement still inserted
    expect(res.supersededId).toBe(500);
  });

  it('supersedes an existing same-day condition occurrence', async () => {
    mockGet.mockResolvedValue({
      data: [{ condition_occurrence_id: 501, person: 1, condition_source_value: 'primary-cancer',
               condition_start_date: '2026-08-21', is_erroneous: false }],
    });

    const res = await writeClinicalFact(1, 'disease', CONDITION, 'Breast Cancer', '2026-08-21');

    expect(mockPatch).toHaveBeenCalledWith('/v1/conditions/501/', {
      is_erroneous: true,
      erroneous_reason: expect.stringContaining('Superseded'),
    });
    expect(res.supersededId).toBe(501);
  });

  it('clears an occurrence-style field without creating a replacement row', async () => {
    mockGet.mockResolvedValue({
      data: [{ condition_occurrence_id: 501, person: 1, condition_source_value: 'primary-cancer',
               condition_start_date: '2026-08-21', is_erroneous: false }],
    });

    const res = await writeClinicalFact(1, 'disease', CONDITION, '', '2026-08-21');

    expect(mockPatch).toHaveBeenCalledWith('/v1/conditions/501/', {
      is_erroneous: true,
      erroneous_reason: expect.stringContaining('Superseded'),
    });
    expect(mockPost).not.toHaveBeenCalled();
    expect(res).toEqual({ supersededId: 501, createdId: null });
  });

  it('does not create an occurrence-style row for an empty value with nothing to clear', async () => {
    const res = await writeClinicalFact(1, 'disease', CONDITION, null, '2026-08-21');

    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
    expect(res).toEqual({ supersededId: null, createdId: null });
  });

  it('leaves a value on a different date alone', async () => {
    mockGet.mockResolvedValue({
      data: [{ measurement_id: 500, person: 1, measurement_source_value: '718-7',
               measurement_date: '2026-01-01', is_erroneous: false }],
    });

    const res = await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 13.1, '2026-08-21');

    expect(mockPatch).not.toHaveBeenCalled();
    expect(res.supersededId).toBeNull();
  });

  it('does not re-supersede a row already marked erroneous', async () => {
    mockGet.mockResolvedValue({
      data: [{ measurement_id: 500, person: 1, measurement_source_value: '718-7',
               measurement_date: '2026-08-21', is_erroneous: true }],
    });

    await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 13.1, '2026-08-21');

    expect(mockPatch).not.toHaveBeenCalled();
  });

  it('still writes when the lookup fails', async () => {
    // Losing the edit is worse than a second row for the day.
    mockGet.mockRejectedValue(new Error('boom'));

    await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 13.1, '2026-08-21');

    expect(mockPost).toHaveBeenCalled();
  });

  it('handles a paginated list response', async () => {
    mockGet.mockResolvedValue({
      data: { results: [{ measurement_id: 77, person: 1, measurement_source_value: '718-7',
                         measurement_date: '2026-08-21' }] },
    });

    const res = await writeClinicalFact(1, 'hemoglobin_g_dl', HGB, 1, '2026-08-21');

    expect(res.supersededId).toBe(77);
  });

  it('refuses a field the server did not mark writable', async () => {
    const ro: FieldDescriptor = { kind: 'computed', writable: false };

    await expect(writeClinicalFact(1, 'bmi', ro, 22)).rejects.toThrow(/not writable/);
    expect(mockPost).not.toHaveBeenCalled();
  });
});


describe('writeClinicalFact — supersede targeting', () => {
  /* Running it against a real server exposed what mocks hid: the endpoint honours
     only `person_id`, and silently ignores anything else — an ignored filter
     returns the entire measurement table, 274k rows across every patient. Matching
     on date alone there would flag a stranger's unrelated result as erroneous. */

  it('queries by person_id, the only filter the endpoint honours', async () => {
    await writeClinicalFact(258, 'hemoglobin_g_dl', HGB, 13.4, '2026-08-21');

    expect(mockGet).toHaveBeenCalledWith('/v1/measurements/', {
      params: { person_id: 258 },
    });
  });

  it('never supersedes another patient\'s row', async () => {
    mockGet.mockResolvedValue({
      data: [{ measurement_id: 999, person: 77, measurement_source_value: '718-7',
               measurement_date: '2026-08-21', is_erroneous: false }],
    });

    const res = await writeClinicalFact(258, 'hemoglobin_g_dl', HGB, 13.4, '2026-08-21');

    expect(mockPatch).not.toHaveBeenCalled();
    expect(res.supersededId).toBeNull();
  });

  it('never supersedes a different analyte on the same date', async () => {
    mockGet.mockResolvedValue({
      data: [{ measurement_id: 999, person: 258, measurement_source_value: '777-3',
               measurement_date: '2026-08-21', is_erroneous: false }],
    });

    const res = await writeClinicalFact(258, 'hemoglobin_g_dl', HGB, 13.4, '2026-08-21');

    expect(mockPatch).not.toHaveBeenCalled();
    expect(res.supersededId).toBeNull();
  });

  it('supersedes the right row when the response holds many patients', async () => {
    mockGet.mockResolvedValue({
      data: [
        { measurement_id: 1, person: 77, measurement_source_value: '718-7',
          measurement_date: '2026-08-21', is_erroneous: false },
        { measurement_id: 2, person: 258, measurement_source_value: '777-3',
          measurement_date: '2026-08-21', is_erroneous: false },
        { measurement_id: 3, person: 258, measurement_source_value: '718-7',
          measurement_date: '2026-08-21', is_erroneous: false },
      ],
    });

    const res = await writeClinicalFact(258, 'hemoglobin_g_dl', HGB, 13.4, '2026-08-21');

    expect(res.supersededId).toBe(3);
  });
});

describe('writeFieldValue — routing by target', () => {
  const PROFILE = {
    kind: 'profile', writable: true, target: 'person',
    person_field: 'gender_concept + gender_source_value',
    payload_field: 'gender', value_kind: 'string',
  } as unknown as FieldDescriptor;

  it('sends a profile field to the persons endpoint, not an observation', async () => {
    // This POSTed an Observation with concept, type and source value all
    // undefined, and never touched Person. `target: 'person'` passed the
    // `!descriptor.target` guard and then fell through to the observation branch.
    await writeFieldValue(261, 'gender', PROFILE, 'female');

    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).toHaveBeenCalledWith('/v1/persons/261/', { gender: 'female' });
  });

  it('keys on payload_field, not on the prose in person_field', async () => {
    const city = {
      kind: 'profile', writable: true, target: 'person',
      person_field: 'Location.city', payload_field: 'city', value_kind: 'string',
    } as unknown as FieldDescriptor;
    await writeFieldValue(261, 'city', city, 'Boston');

    expect(mockPatch).toHaveBeenCalledWith('/v1/persons/261/', { city: 'Boston' });
  });

  it('clears with null rather than empty string', async () => {
    await writeFieldValue(261, 'gender', PROFILE, '');
    expect(mockPatch).toHaveBeenCalledWith('/v1/persons/261/', { gender: null });
  });

  it('still routes a measurement to the OMOP endpoint', async () => {
    await writeFieldValue(261, 'hemoglobin_g_dl', HGB, 12.5, '2026-08-21');
    expect(mockPost).toHaveBeenCalledWith('/v1/measurements/', expect.objectContaining({
      person: 261, measurement_source_value: '718-7',
    }));
  });

  it('refuses a target writeClinicalFact cannot write instead of mis-posting', async () => {
    await expect(
      writeClinicalFact(261, 'gender', PROFILE, 'female'),
    ).rejects.toThrow(/writes to person/);
    expect(mockPost).not.toHaveBeenCalled();
  });
});
