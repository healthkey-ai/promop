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
    patient_info: { person_id: 12, height: 170, weight: 60, bmi: 20.76 }, write_enabled: true,
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
