import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => ({ get: mocks.get }),
  clinicalUrl: (path: string) => path,
}));

import { __resetWritableFieldsCache, fetchWritableFields } from './useWritableFields';

describe('the native writable-fields fetch', () => {
  beforeEach(() => {
    __resetWritableFieldsCache();
    mocks.get.mockReset();
  });

  it('accepts schema v1 and strips the envelope', async () => {
    mocks.get.mockResolvedValue({
      data: {
        __descriptor__: { schema_version: 1 },
        weight: { writable: true, target: 'patient_record' },
      },
    });

    const fields = await fetchWritableFields(12);

    expect(fields.weight).toEqual({ writable: true, target: 'patient_record' });
    expect((fields as Record<string, unknown>).__descriptor__).toBeUndefined();
  });

  it('accepts a legacy mapping without an envelope', async () => {
    mocks.get.mockResolvedValue({ data: { weight: { writable: true } } });

    await expect(fetchWritableFields(12)).resolves.toEqual({
      weight: { writable: true },
    });
  });

  it('rejects a contract this build does not know and does not cache it', async () => {
    mocks.get.mockResolvedValue({
      data: {
        __descriptor__: { schema_version: 2 },
        weight: { writable: true, target: 'patient_record' },
      },
    });

    await expect(fetchWritableFields(12)).rejects.toThrow(/too old/i);

    mocks.get.mockResolvedValue({ data: { weight: { writable: true } } });
    await expect(fetchWritableFields(12)).resolves.toEqual({ weight: { writable: true } });
    expect(mocks.get).toHaveBeenCalledTimes(2);
  });
});
