import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';
import { PatientFormStore } from './patientFormStore';

const fields = {
  height: { writable: true, target: 'patient_record' },
  weight: { writable: true, target: 'patient_record' },
  bmi: { writable: false, inputs: ['height', 'weight'], bindings: ['bmiMin'] },
  genetic_mutations: { writable: true, target: 'genomics' },
};
function setup() {
  const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? fields : {
    patient_info: { person_id: 12, height: 170, weight: 60, bmi: 20.76,
      no_pre_existing_conditions: true, preexisting_conditions: [] }, write_enabled: true,
  } }));
  const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
    patient_info: { person_id: 12, height: 170, weight: 60, ...edits, bmi: 24.22 },
    write: { applied: Object.keys(edits), rejected: {} },
  } }));
  const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
  return { store, get, patch };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('planned-therapy options from a host catalog', () => {
  function setupCatalog(regimens: unknown) {
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) return { data: {
        planned_therapies: { writable: true, target: 'patient_record', multiple: true,
          options_source: 'therapy_regimens' } } };
      if (url.includes('therapy-regimens')) return { data: regimens };
      return { data: { patient_info: { person_id: 12, disease: 'Multiple Myeloma',
        therapy_lines_count: '1' }, write_enabled: true } };
    });
    return new PatientFormStore({ get, patch: vi.fn() } as unknown as AxiosInstance, '/api');
  }

  it('stores the host value while showing the host label', async () => {
    const store = setupCatalog([{ code: 'r_chop', title: 'R-CHOP', value: 'r_chop' }]);
    await store.load();

    expect(store.getSnapshot().fields.planned_therapies.options)
      .toEqual([{ value: 'r_chop', label: 'R-CHOP' }]);
  });

  it('explains an empty catalog instead of leaving a blank picker', async () => {
    const store = setupCatalog([]);
    await store.load();

    expect(store.getSnapshot().fields.planned_therapies.options).toEqual([]);
    expect(store.getSnapshot().fields.planned_therapies.options_note)
      .toMatch(/No planned therapies are available/);
  });

  it('offers a retry when the catalog request fails', async () => {
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) return { data: {
        planned_therapies: { writable: true, target: 'patient_record',
          options_source: 'therapy_regimens' } } };
      if (url.includes('therapy-regimens')) throw new Error('catalog down');
      return { data: { patient_info: { person_id: 12, disease: 'Multiple Myeloma' },
        write_enabled: true } };
    });
    const store = new PatientFormStore({ get, patch: vi.fn() } as unknown as AxiosInstance, '/api');
    await store.load();

    expect(store.getSnapshot().fields.planned_therapies.retryable).toBe(true);
    expect(store.getSnapshot().fields.planned_therapies.reason).toMatch(/Could not load/);
  });
});

describe('shared patient-form queue', () => {
  it('loads caller/patient-specific descriptors and fails closed for unsupported writes', async () => {
    const { store, get, patch } = setup();
    await store.load();
    expect(get).toHaveBeenLastCalledWith('/api/v1/patient-records/writable-fields/', {
      params: { person_id: 12 }, headers: { 'X-Patient-Form': '1' },
    });
    expect(store.resolve('bmiMin')).toBe('bmi');
    store.enqueue('bmi', 4); store.enqueue('unknown', 1); store.enqueue('genetic_mutations', []);
    await vi.advanceTimersByTimeAsync(600);
    expect(patch).not.toHaveBeenCalled();
  });
  it('batches two rapid field edits after 500ms and uses the server computed result', async () => {
    const { store, patch } = setup();
    await store.load();
    store.enqueue('height', 180); store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(499);
    expect(patch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(patch).toHaveBeenCalledOnce();
    expect(patch).toHaveBeenCalledWith('/api/v1/patient-records/12/', { height: 180, weight: 80 },
      { headers: { 'X-Patient-Form': '1' } });
    expect(store.getSnapshot().values.bmi).toBe(24.22);
    expect(store.getSnapshot().pending).toEqual({});
  });
  it('serializes writes and retains a newer edit until its own PATCH settles', async () => {
    const { store, patch } = setup();
    await store.load();
    let settle!: (value: Awaited<ReturnType<typeof patch>>) => void;
    patch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }));
    store.enqueue('weight', 70);
    await vi.advanceTimersByTimeAsync(500);
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(store.getSnapshot().pending.weight).toBe(80);
    settle({ data: { patient_info: { person_id: 12, weight: 70, height: 170, bmi: 24.22 },
      write: { applied: ['weight'], rejected: {} } } });
    await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1]).toEqual({ weight: 80 });
    expect(store.getSnapshot().values.weight).toBe(80);
  });
  it('does not let an in-flight PATCH resurrect a withdrawn conditions answer', async () => {
    const { store, patch } = setup();
    await store.load();
    let settle!: (value: Awaited<ReturnType<typeof patch>>) => void;
    patch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }));
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    store.applyServerValues({ no_pre_existing_conditions: null, preexisting_conditions: [] });
    settle({ data: {
      patient_info: { person_id: 12, height: 170, weight: 80, bmi: 24.22,
        no_pre_existing_conditions: true, preexisting_conditions: [] },
      write: { applied: ['weight'], rejected: {} },
    } });
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().values.no_pre_existing_conditions).toBeNull();
  });
  it('rolls failed PATCH back and exposes an error', async () => {
    const { store, patch } = setup();
    await store.load(); patch.mockRejectedValueOnce(new Error('offline'));
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    expect(store.getSnapshot().pending).toEqual({});
    expect(store.getSnapshot().values.weight).toBe(60);
    expect(store.getSnapshot().errors.weight).toContain('Could not save');
  });
  it('does not mistake partial 200 success for acknowledgement of every field', async () => {
    const { store, patch } = setup();
    await store.load();
    patch.mockResolvedValueOnce({ data: { patient_info: { person_id: 12, height: 170, weight: 80, bmi: 24.22 },
      write: { applied: ['weight'], rejected: { height: 'Clearing is not supported.' } } } });
    store.enqueue('weight', 80); store.enqueue('height', null);
    await vi.advanceTimersByTimeAsync(500);
    expect(store.getSnapshot().values.height).toBe(170);
    expect(store.getSnapshot().errors.height).toBe('Clearing is not supported.');
    expect(store.getSnapshot().errors.weight).toBeUndefined();
  });
  it('drains queued followups after every component has unsubscribed', async () => {
    const { store, patch } = setup();
    await store.load();
    let reject!: (error: Error) => void;
    patch.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const unsubscribe = store.subscribe(() => {});
    store.enqueue('weight', 70); await vi.advanceTimersByTimeAsync(500);
    store.enqueue('weight', 80); unsubscribe();
    reject(new Error('offline')); await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().values.weight).toBe(80);
    expect(store.getSnapshot().errors).toEqual({});
  });
  it('isolates patients and transports instead of retargeting an old queued batch', async () => {
    const first = setup(); const second = setup();
    await Promise.all([first.store.load(), second.store.load()]);
    first.store.enqueue('weight', 90);
    await vi.advanceTimersByTimeAsync(500);
    expect(first.patch).toHaveBeenCalledOnce();
    expect(second.patch).not.toHaveBeenCalled();
    expect(second.store.getSnapshot().values.weight).toBe(60);
  });
  it('still invalidates the host after its root unmounts during a save', async () => {
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);
    store.enqueue('weight', 80);
    detach();
    await vi.advanceTimersByTimeAsync(500);
    expect(updated).toHaveBeenCalledOnce();
    store.enqueue('weight', 90);
    await vi.advanceTimersByTimeAsync(500);
    expect(updated).toHaveBeenCalledOnce();
  });
});

describe('dated result edits', () => {
  function setupDated() {
    let latest = '2026-08-01';
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
      hemoglobin_g_dl: { writable: true, target: 'patient_record', dated: true,
        latest_date: latest, projection: { omop_table: 'measurement' } },
    } : { patient_info: { person_id: 12, hemoglobin_g_dl: 12.5 }, write_enabled: true } }));
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, hemoglobin_g_dl: 13 },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    return {
      store: new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api'),
      patch,
      setLatest: (value: string) => { latest = value; },
    };
  }

  it('sends the value and its clinical date together', async () => {
    const { store, patch } = setupDated();
    await store.load();

    store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledWith('/api/v1/patient-records/12/',
      { hemoglobin_g_dl: { value: 13, date: '2026-08-02' } },
      { headers: { 'X-Patient-Form': '1' } });
  });

  it('takes the correction target from the server, not the submitted batch', async () => {
    const { store, setLatest } = setupDated();
    await store.load();
    // Another session added a newer result while this save was in flight.
    setLatest('2026-09-01');

    store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);

    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-09-01');
  });
});

describe('shared patient-form queue', () => {
  it('loads caller/patient-specific descriptors and fails closed for unsupported writes', async () => {
    const { store, get, patch } = setup();
    await store.load();
    expect(get).toHaveBeenLastCalledWith('/api/v1/patient-records/writable-fields/', {
      params: { person_id: 12 }, headers: { 'X-Patient-Form': '1' },
    });
    expect(store.resolve('bmiMin')).toBe('bmi');
    store.enqueue('bmi', 4); store.enqueue('unknown', 1); store.enqueue('genetic_mutations', []);
    await vi.advanceTimersByTimeAsync(600);
    expect(patch).not.toHaveBeenCalled();
  });
  it('batches two rapid field edits after 500ms and uses the server computed result', async () => {
    const { store, patch } = setup();
    await store.load();
    store.enqueue('height', 180); store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(499);
    expect(patch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(patch).toHaveBeenCalledOnce();
    expect(patch).toHaveBeenCalledWith('/api/v1/patient-records/12/', { height: 180, weight: 80 },
      { headers: { 'X-Patient-Form': '1' } });
    expect(store.getSnapshot().values.bmi).toBe(24.22);
    expect(store.getSnapshot().pending).toEqual({});
  });
  it('serializes writes and retains a newer edit until its own PATCH settles', async () => {
    const { store, patch } = setup();
    await store.load();
    let settle!: (value: Awaited<ReturnType<typeof patch>>) => void;
    patch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }));
    store.enqueue('weight', 70);
    await vi.advanceTimersByTimeAsync(500);
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(store.getSnapshot().pending.weight).toBe(80);
    settle({ data: { patient_info: { person_id: 12, weight: 70, height: 170, bmi: 24.22 },
      write: { applied: ['weight'], rejected: {} } } });
    await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledTimes(2);
    expect(patch.mock.calls[1][1]).toEqual({ weight: 80 });
    expect(store.getSnapshot().values.weight).toBe(80);
  });
  it('does not let an in-flight PATCH resurrect a withdrawn conditions answer', async () => {
    const { store, patch } = setup();
    await store.load();
    let settle!: (value: Awaited<ReturnType<typeof patch>>) => void;
    patch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve; }));
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    store.applyServerValues({ no_pre_existing_conditions: null, preexisting_conditions: [] });
    settle({ data: {
      patient_info: { person_id: 12, height: 170, weight: 80, bmi: 24.22,
        no_pre_existing_conditions: true, preexisting_conditions: [] },
      write: { applied: ['weight'], rejected: {} },
    } });
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().values.no_pre_existing_conditions).toBeNull();
  });
  it('rolls failed PATCH back and exposes an error', async () => {
    const { store, patch } = setup();
    await store.load(); patch.mockRejectedValueOnce(new Error('offline'));
    store.enqueue('weight', 80);
    await vi.advanceTimersByTimeAsync(500);
    expect(store.getSnapshot().pending).toEqual({});
    expect(store.getSnapshot().values.weight).toBe(60);
    expect(store.getSnapshot().errors.weight).toContain('Could not save');
  });
  it('does not mistake partial 200 success for acknowledgement of every field', async () => {
    const { store, patch } = setup();
    await store.load();
    patch.mockResolvedValueOnce({ data: { patient_info: { person_id: 12, height: 170, weight: 80, bmi: 24.22 },
      write: { applied: ['weight'], rejected: { height: 'Clearing is not supported.' } } } });
    store.enqueue('weight', 80); store.enqueue('height', null);
    await vi.advanceTimersByTimeAsync(500);
    expect(store.getSnapshot().values.height).toBe(170);
    expect(store.getSnapshot().errors.height).toBe('Clearing is not supported.');
    expect(store.getSnapshot().errors.weight).toBeUndefined();
  });
  it('drains queued followups after every component has unsubscribed', async () => {
    const { store, patch } = setup();
    await store.load();
    let reject!: (error: Error) => void;
    patch.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const unsubscribe = store.subscribe(() => {});
    store.enqueue('weight', 70); await vi.advanceTimersByTimeAsync(500);
    store.enqueue('weight', 80); unsubscribe();
    reject(new Error('offline')); await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().values.weight).toBe(80);
    expect(store.getSnapshot().errors).toEqual({});
  });
  it('isolates patients and transports instead of retargeting an old queued batch', async () => {
    const first = setup(); const second = setup();
    await Promise.all([first.store.load(), second.store.load()]);
    first.store.enqueue('weight', 90);
    await vi.advanceTimersByTimeAsync(500);
    expect(first.patch).toHaveBeenCalledOnce();
    expect(second.patch).not.toHaveBeenCalled();
    expect(second.store.getSnapshot().values.weight).toBe(60);
  });
  it('still invalidates the host after its root unmounts during a save', async () => {
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);
    store.enqueue('weight', 80);
    detach();
    await vi.advanceTimersByTimeAsync(500);
    expect(updated).toHaveBeenCalledOnce();
    store.enqueue('weight', 90);
    await vi.advanceTimersByTimeAsync(500);
    expect(updated).toHaveBeenCalledOnce();
  });
});
