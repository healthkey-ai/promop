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

describe('load failures', () => {
  function failing(failure: (url: string) => unknown) {
    const get = vi.fn(async (url: string) => {
      const error = failure(url);
      if (error) throw error;
      return { data: url.includes('writable-fields') ? fields
        : { patient_info: { person_id: 12, weight: 60 }, write_enabled: true } };
    });
    return new PatientFormStore({ get, patch: vi.fn() } as unknown as AxiosInstance, '/api');
  }
  const notFound = { response: { status: 404 } };

  it('says no record is linked when the record read answers 404', async () => {
    const store = failing((url) => url.includes('patient-info/me') && notFound);
    await store.load();
    expect(store.getSnapshot().error).toBe('No patient record linked to this account.');
    expect(store.getSnapshot().writable).toBe(false);
  });

  it('keeps the generic message for other failures, including a descriptor 404', async () => {
    for (const failure of [
      (url: string) => url.includes('patient-info/me') && { response: { status: 500 } },
      (url: string) => url.includes('patient-info/me') && new Error('offline'),
      (url: string) => url.includes('writable-fields') && notFound,
    ]) {
      const store = failing(failure);
      await store.load();
      expect(store.getSnapshot().error)
        .toBe('Could not load the patient record and field permissions. Please retry.');
    }
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
  it('refuses a descriptor version this build does not understand', async () => {
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
      __descriptor__: { schema_version: 2 },
      weight: { writable: true, target: 'patient_record' },
    } : { patient_info: { person_id: 12, weight: 60 }, write_enabled: true } }));
    const store = new PatientFormStore({ get } as unknown as AxiosInstance, '/api');

    await store.load();

    const state = store.getSnapshot();
    expect(state.error).toMatch(/too old/i);
    expect(state.writable).toBe(false);
    expect(state.fields).toEqual({});
  });
  it('refuses a pending batch when the descriptor version changes on refresh', async () => {
    let version = 1;
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
      __descriptor__: { schema_version: version },
      weight: { writable: true, target: 'patient_record' },
    } : { patient_info: { person_id: 12, weight: 60 }, write_enabled: true } }));
    const patch = vi.fn(async () => ({ data: { patient_info: {}, write: { applied: [] } } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();
    store.enqueue('weight', 90);

    version = 2;
    await store.load();
    await vi.advanceTimersByTimeAsync(600);

    expect(patch).not.toHaveBeenCalled();
    expect(store.getSnapshot().error).toMatch(/too old/i);
    expect(store.getSnapshot().errors.weight).toMatch(/too old/i);
    expect(store.getSnapshot().pending).toEqual({});
  });
  it('keeps the host listener through a bypass write after unmount', async () => {
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);

    store.beginBypassWrite();
    detach(); // the tab unmounted while the bypass write was running
    store.announceUpdated();
    store.endBypassWrite();
    await vi.runAllTimersAsync();

    expect(updated).toHaveBeenCalledOnce();
  });
  it('announces a write that bypassed the queue', async () => {
    // Attestations, therapy lines and genomics findings never enter the PATCH
    // queue. The host still has to invalidate its own queries after one.
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);

    store.announceUpdated();
    await vi.runAllTimersAsync();
    expect(updated).toHaveBeenCalledOnce();
    expect(updated.mock.calls[0][0].patient_info.person_id).toBe(12);
    detach();
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

  it('refuses the next dated save until a failed descriptor refresh is retried', async () => {
    let latest = '2026-08-01';
    let metadataFails = false;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) {
        if (metadataFails) throw new Error('metadata unavailable');
        return { data: { hemoglobin_g_dl: { writable: true, target: 'patient_record',
          dated: true, latest_date: latest, projection: { omop_table: 'measurement' } } } };
      }
      return { data: { patient_info: { person_id: 12, hemoglobin_g_dl: 12.5 }, write_enabled: true } };
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, hemoglobin_g_dl: 13 },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    // The save lands, but the refresh that would move the correction target does not.
    latest = '2026-09-01';
    metadataFails = true;
    store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);

    expect(store.getSnapshot().datesStale).toBe(true);
    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-08-01');

    // A second dated save must not be queued against that stale date.
    store.enqueue('hemoglobin_g_dl', { value: 14, date: '2026-08-01' });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(store.getSnapshot().errors.hemoglobin_g_dl).toMatch(/Retry/);

    // Retrying the refresh clears both the flag and the stale target.
    metadataFails = false;
    await store.reloadDates();
    expect(store.getSnapshot().datesStale).toBe(false);
    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-09-01');

    store.enqueue('hemoglobin_g_dl', { value: 14, date: '2026-09-01' });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledTimes(2);
  });

  it('holds a dated edit queued behind an in-flight save once the refresh fails', async () => {
    let latest = '2026-08-01';
    let metadataFails = false;
    let resolvePatch!: (payload: unknown) => void;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) {
        if (metadataFails) throw new Error('metadata unavailable');
        return { data: { hemoglobin_g_dl: { writable: true, target: 'patient_record',
          dated: true, latest_date: latest, projection: { omop_table: 'measurement' } } } };
      }
      return { data: { patient_info: { person_id: 12, hemoglobin_g_dl: 12.5 }, write_enabled: true } };
    });
    const patch = vi.fn(() => new Promise<unknown>((resolve) => { resolvePatch = resolve; }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();

    // The first save is still in flight, so the refresh has not failed yet and
    // the queue accepts the second dated edit. That is the window this guards.
    store.enqueue('hemoglobin_g_dl', { value: 14, date: '2026-08-03' });
    metadataFails = true;
    resolvePatch({ data: { patient_info: { person_id: 12, hemoglobin_g_dl: 13 },
      write: { applied: ['hemoglobin_g_dl'], rejected: {} } } });
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledOnce();                 // the held edit never went out
    expect(store.getSnapshot().pending).toEqual({});
    expect(store.getSnapshot().errors.hemoglobin_g_dl).toMatch(/Retry/);

    // A retry refreshes the dates but does not re-send an unconfirmed date.
    metadataFails = false;
    latest = '2026-08-03';
    await store.reloadDates();
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(store.getSnapshot().datesStale).toBe(false);
    expect(store.getSnapshot().errors.hemoglobin_g_dl).toBeUndefined();
    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-08-03');
  });

  it('does not dispatch a dated edit ahead of an overlapping refresh', async () => {
    const fields = { hemoglobin_g_dl: { writable: true, target: 'patient_record',
      dated: true, latest_date: '2026-08-01', projection: { omop_table: 'measurement' } } };
    let metadataCalls = 0;
    const refreshes: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    const get = vi.fn((url: string) => {
      if (url.includes('writable-fields')) {
        metadataCalls += 1;
        if (metadataCalls === 1) return Promise.resolve({ data: fields });
        return new Promise((resolve, reject) => { refreshes.push({ resolve, reject }); });
      }
      return Promise.resolve({ data: {
        patient_info: { person_id: 12, hemoglobin_g_dl: 12.5 }, write_enabled: true } });
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, hemoglobin_g_dl: 13 },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    store.enqueue('hemoglobin_g_dl', { value: 13, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(refreshes).toHaveLength(1);              // the post-save refresh is pending
    // Let the flush actually reach its wait, so the retry below supersedes the
    // attempt it is already awaiting — the window this guards.
    await vi.advanceTimersByTimeAsync(0);

    store.enqueue('hemoglobin_g_dl', { value: 14, date: '2026-08-03' });
    void store.reloadDates();                        // a retry supersedes it
    expect(refreshes).toHaveLength(2);

    refreshes[0].reject(new Error('superseded'));
    await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledOnce();            // still waiting on the newest refresh

    refreshes[1].reject(new Error('metadata unavailable'));
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledOnce();            // the held date was never sent
    expect(store.getSnapshot().datesStale).toBe(true);
    expect(store.getSnapshot().errors.hemoglobin_g_dl).toMatch(/Retry/);
  });

  it('holds a stale dated pair without sending its unit alone', async () => {
    const fields = {
      hemoglobin_level: { writable: true, target: 'patient_record', dated: true,
        latest_date: '2026-08-01', projection: { omop_table: 'measurement' } },
      hemoglobin_level_units: { writable: false, kind: 'selectable',
        qualifies: 'hemoglobin_level',
        unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }] },
    };
    let metadataCalls = 0;
    const refreshes: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    const get = vi.fn((url: string) => {
      if (url.includes('writable-fields')) {
        metadataCalls += 1;
        if (metadataCalls === 1) return Promise.resolve({ data: fields });
        return new Promise((resolve, reject) => { refreshes.push({ resolve, reject }); });
      }
      return Promise.resolve({ data: {
        patient_info: { person_id: 12, hemoglobin_level: 12,
          hemoglobin_level_units: 'G/DL' }, write_enabled: true } });
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, hemoglobin_level: 12 },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    store.enqueue('hemoglobin_level', { value: 12, date: '2026-08-02' });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
    expect(refreshes).toHaveLength(1);

    // Accepted before the refresh fails, so the unit is queued beside the date.
    store.enqueuePair('hemoglobin_level', { value: 13, date: '2026-08-03' },
      'hemoglobin_level_units', 'G/L');
    refreshes[0].reject(new Error('metadata unavailable'));
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledOnce();
    expect(store.getSnapshot().pending.hemoglobin_level).toBeUndefined();
    expect(store.getSnapshot().pending.hemoglobin_level_units).toBeUndefined();
    expect(store.getSnapshot().errors.hemoglobin_level).toMatch(/Retry/);
  });

  it('does not dispatch ahead of a refresh that supersedes the awaited one', async () => {
    const fields = { height: { writable: true, target: 'patient_record', control: 'number' } };
    let metadataCalls = 0;
    const refreshes: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    const get = vi.fn((url: string) => {
      if (url.includes('writable-fields')) {
        metadataCalls += 1;
        if (metadataCalls === 1) return Promise.resolve({ data: fields });
        return new Promise((resolve, reject) => { refreshes.push({ resolve, reject }); });
      }
      return Promise.resolve({ data: {
        patient_info: { person_id: 12, height: 170 }, write_enabled: true } });
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, ...edits }, write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    void store.reloadDates();                        // a host retry is pending
    expect(refreshes).toHaveLength(1);
    store.enqueue('height', 180);
    await vi.advanceTimersByTimeAsync(500);          // flush is now waiting on the retry
    expect(patch).not.toHaveBeenCalled();

    void store.reloadDates();                        // superseded while it waits
    expect(refreshes).toHaveLength(2);
    refreshes[0].reject(new Error('superseded'));
    await vi.advanceTimersByTimeAsync(0);
    expect(patch).not.toHaveBeenCalled();            // still waiting on the newest refresh

    refreshes[1].resolve({ data: fields });
    await vi.advanceTimersByTimeAsync(500);
    expect(patch).toHaveBeenCalledOnce();
  });

  it('does not overtake a refresh started in the same turn as flush', async () => {
    const fields = { height: { writable: true, target: 'patient_record', control: 'number' } };
    let metadataCalls = 0;
    const refreshes: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    const get = vi.fn((url: string) => {
      if (url.includes('writable-fields')) {
        metadataCalls += 1;
        if (metadataCalls === 1) return Promise.resolve({ data: fields });
        return new Promise((resolve, reject) => { refreshes.push({ resolve, reject }); });
      }
      return Promise.resolve({ data: {
        patient_info: { person_id: 12, height: 170 }, write_enabled: true } });
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, ...edits }, write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    store.enqueue('height', 180);
    const saving = store.flush();                    // nothing is in flight yet
    void store.reloadDates();                        // starts before flush resumes
    expect(refreshes).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(0);            // let flush resume
    expect(patch).not.toHaveBeenCalled();            // the refresh is still pending

    refreshes[0].resolve({ data: fields });
    await saving;
    await vi.advanceTimersByTimeAsync(0);
    expect(patch).toHaveBeenCalledOnce();
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
  it('refuses a descriptor version this build does not understand', async () => {
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
      __descriptor__: { schema_version: 2 },
      weight: { writable: true, target: 'patient_record' },
    } : { patient_info: { person_id: 12, weight: 60 }, write_enabled: true } }));
    const store = new PatientFormStore({ get } as unknown as AxiosInstance, '/api');

    await store.load();

    const state = store.getSnapshot();
    expect(state.error).toMatch(/too old/i);
    expect(state.writable).toBe(false);
    expect(state.fields).toEqual({});
  });
  it('refuses a pending batch when the descriptor version changes on refresh', async () => {
    let version = 1;
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? {
      __descriptor__: { schema_version: version },
      weight: { writable: true, target: 'patient_record' },
    } : { patient_info: { person_id: 12, weight: 60 }, write_enabled: true } }));
    const patch = vi.fn(async () => ({ data: { patient_info: {}, write: { applied: [] } } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();
    store.enqueue('weight', 90);

    version = 2;
    await store.load();
    await vi.advanceTimersByTimeAsync(600);

    expect(patch).not.toHaveBeenCalled();
    expect(store.getSnapshot().error).toMatch(/too old/i);
    expect(store.getSnapshot().errors.weight).toMatch(/too old/i);
    expect(store.getSnapshot().pending).toEqual({});
  });
  it('keeps the host listener through a bypass write after unmount', async () => {
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);

    store.beginBypassWrite();
    detach(); // the tab unmounted while the bypass write was running
    store.announceUpdated();
    store.endBypassWrite();
    await vi.runAllTimersAsync();

    expect(updated).toHaveBeenCalledOnce();
  });
  it('announces a write that bypassed the queue', async () => {
    // Attestations, therapy lines and genomics findings never enter the PATCH
    // queue. The host still has to invalidate its own queries after one.
    const { store } = setup();
    await store.load();
    const updated = vi.fn();
    const detach = store.onUpdated(updated);

    store.announceUpdated();
    await vi.runAllTimersAsync();
    expect(updated).toHaveBeenCalledOnce();
    expect(updated.mock.calls[0][0].patient_info.person_id).toBe(12);
    detach();
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

describe('planned-therapy options survive a dated descriptor reload', () => {
  it('keeps the catalog options after a dated save', async () => {
    let writableReads = 0;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) {
        writableReads += 1;
        return { data: {
          planned_therapies: { writable: true, target: 'patient_record', multiple: true,
            options_source: 'therapy_regimens' },
          hemoglobin_g_dl: { writable: true, target: 'patient_record', dated: true,
            latest_date: writableReads === 1 ? '2026-07-04' : '2026-08-01' },
        } };
      }
      if (url.includes('therapy-regimens')) return { data: [
        { code: 'r_chop', title: 'R-CHOP', value: 'r_chop' },
      ] };
      return { data: { patient_info: { person_id: 12, disease: 'Multiple Myeloma',
        therapy_lines_count: '1' }, write_enabled: true } };
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, disease: 'Multiple Myeloma',
        therapy_lines_count: '1', ...edits },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();

    expect(store.getSnapshot().fields.planned_therapies.options)
      .toEqual([{ value: 'r_chop', label: 'R-CHOP' }]);
    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-07-04');

    store.enqueue('hemoglobin_g_dl', { value: 12.5, date: '2026-07-04' });
    await store.flush();

    expect(patch).toHaveBeenCalled();
    expect(writableReads).toBe(2);
    expect(store.getSnapshot().fields.hemoglobin_g_dl.latest_date).toBe('2026-08-01');
    expect(store.getSnapshot().fields.planned_therapies.options)
      .toEqual([{ value: 'r_chop', label: 'R-CHOP' }]);
  });
});

describe('planned-therapy options follow a disease change', () => {
  it('re-enriches the catalog after an ordinary disease PATCH', async () => {
    let catalogCalls = 0;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) return { data: {
        planned_therapies: { writable: true, target: 'patient_record', multiple: true,
          options_source: 'therapy_regimens' },
        disease: { writable: true, target: 'patient_record' },
      } };
      if (url.includes('therapy-regimens')) {
        catalogCalls += 1;
        return { data: catalogCalls === 1
          ? [{ code: 'r_chop', title: 'R-CHOP', value: 'r_chop' }]
          : [{ code: 'fcr', title: 'FCR', value: 'fcr' }] };
      }
      return { data: { patient_info: { person_id: 12, disease: 'Multiple Myeloma',
        therapy_lines_count: '1' }, write_enabled: true } };
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, disease: 'CLL', therapy_lines_count: '1', ...edits },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();
    expect(store.getSnapshot().fields.planned_therapies.options)
      .toEqual([{ value: 'r_chop', label: 'R-CHOP' }]);

    store.enqueue('disease', 'CLL');
    await store.flush();

    expect(patch).toHaveBeenCalled();
    expect(catalogCalls).toBe(2);
    const catalogRequests = get.mock.calls.filter(
      ([url]) => String(url).includes('therapy-regimens'));
    expect(catalogRequests).toHaveLength(2);
    expect(catalogRequests[1][1]).toEqual({
      params: { disease: 'C2987', round: 'second_line_therapy' },
    });
    expect(store.getSnapshot().fields.planned_therapies.options)
      .toEqual([{ value: 'fcr', label: 'FCR' }]);
  });

  it('disables stale planned therapies when the context refresh fails', async () => {
    let failWritable = false;
    const get = vi.fn(async (url: string) => {
      if (url.includes('writable-fields')) {
        if (failWritable) throw new Error('metadata down');
        return { data: {
          planned_therapies: { writable: true, target: 'patient_record', multiple: true,
            options_source: 'therapy_regimens' },
          disease: { writable: true, target: 'patient_record' },
        } };
      }
      if (url.includes('therapy-regimens')) return { data: [
        { code: 'r_chop', title: 'R-CHOP', value: 'r_chop' },
      ] };
      return { data: { patient_info: { person_id: 12, disease: 'Multiple Myeloma',
        therapy_lines_count: '1' }, write_enabled: true } };
    });
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, disease: 'CLL', therapy_lines_count: '1', ...edits },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    await store.load();
    failWritable = true;

    store.enqueue('disease', 'CLL');
    await store.flush();

    expect(store.getSnapshot().fields.planned_therapies.writable).toBe(false);
    expect(store.getSnapshot().fields.planned_therapies.retryable).toBe(true);

    failWritable = false;
    await store.reloadDates();
    expect(store.getSnapshot().fields.planned_therapies.writable).toBe(true);
  });
});

describe('source-pair writes (#5700)', () => {
  const pairFields = {
    hemoglobin_level: {
      writable: true, target: 'patient_record', control: 'number',
      unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }],
    },
    hemoglobin_level_units: {
      writable: false, kind: 'selectable', qualifies: 'hemoglobin_level',
      unit_options: [{ value: 'G/DL', label: 'g/dL' }, { value: 'G/L', label: 'g/L' }],
    },
    height: { writable: true, target: 'patient_record', control: 'number' },
    height_units: {
      writable: false, kind: 'selectable', qualifies: 'height',
      unit_options: [{ value: 'cm', label: 'cm' }],
    },
  };
  function pairSetup() {
    const get = vi.fn(async (url: string) => ({ data: url.includes('writable-fields') ? pairFields : {
      patient_info: { person_id: 12, hemoglobin_level: 120, hemoglobin_level_units: 'G/DL' },
      write_enabled: true,
    } }));
    const patch = vi.fn(async (_url: string, edits: Record<string, unknown>) => ({ data: {
      patient_info: { person_id: 12, ...edits, hemoglobin_g_dl: 12 },
      write: { applied: Object.keys(edits), rejected: {} },
    } }));
    const store = new PatientFormStore({ get, patch } as unknown as AxiosInstance, '/api');
    return { store, patch };
  }

  it('sends the value and its unit in the same PATCH', async () => {
    const { store, patch } = pairSetup();
    await store.load();

    store.enqueuePair('hemoglobin_level', 120, 'hemoglobin_level_units', 'G/L');
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledWith('/api/v1/patient-records/12/',
      { hemoglobin_level: 120, hemoglobin_level_units: 'G/L' },
      { headers: { 'X-Patient-Form': '1' } });
    expect(store.getSnapshot().pending).toEqual({});
  });

  it('sends a dated pair as one PATCH', async () => {
    const { store, patch } = pairSetup();
    await store.load();

    store.enqueuePair('hemoglobin_level', { value: 120, date: '2026-08-01' },
      'hemoglobin_level_units', 'G/L');
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledWith('/api/v1/patient-records/12/',
      { hemoglobin_level: { value: 120, date: '2026-08-01' },
        hemoglobin_level_units: 'G/L' },
      { headers: { 'X-Patient-Form': '1' } });
  });

  it('refuses a companion entry that does not qualify the field', async () => {
    const { store, patch } = pairSetup();
    await store.load();

    store.enqueuePair('hemoglobin_level', 120, 'hemoglobin_level_units', 'G/L');
    // A unit descriptor must name the value it qualifies; a mismatched pair is
    // a client error, not two half-writes that corrupt both fields.
    store.enqueuePair('hemoglobin_level', 999, 'height_units', 'cm');
    await vi.advanceTimersByTimeAsync(500);

    expect(patch).toHaveBeenCalledOnce();
    expect(patch.mock.calls[0][1]).toEqual({
      hemoglobin_level: 120, hemoglobin_level_units: 'G/L',
    });
  });

  it('reports a refusal on both halves without keeping a half-saved pair', async () => {
    const { store, patch } = pairSetup();
    await store.load();
    patch.mockRejectedValueOnce({ response: { data: { rejected: {
      hemoglobin_level: 'Hemoglobin is outside the storable range.',
      hemoglobin_level_units: 'Unsupported unit.',
    } } } });

    store.enqueuePair('hemoglobin_level', 120, 'hemoglobin_level_units', 'G/L');
    await vi.advanceTimersByTimeAsync(500);

    expect(store.getSnapshot().pending).toEqual({});
    expect(store.getSnapshot().values.hemoglobin_level).toBe(120);
    expect(store.getSnapshot().values.hemoglobin_level_units).toBe('G/DL');
    expect(store.getSnapshot().errors.hemoglobin_level).toMatch(/outside the storable range/);
    expect(store.getSnapshot().errors.hemoglobin_level_units).toMatch(/Unsupported unit/);
  });
});
