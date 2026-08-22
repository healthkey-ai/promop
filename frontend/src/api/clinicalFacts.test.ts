/**
 * Writing a clinical value means writing an OMOP fact, never patching the
 * projection. A correction supersedes rather than overwrites.
 */
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { writeClinicalFact, today } from './clinicalFacts';
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
