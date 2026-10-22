import { afterEach, expect, it, vi } from 'vitest';
import { patientFormRuntime, retainPatientFormRuntime } from './patientFormRuntime';

afterEach(() => { vi.useRealTimers(); });

it('shares only the same immutable API/credential/patient context', () => {
  const context = { apiBase: '/api/federation', token: 'self', impToken: 'patient-a', sessionKey: 'runtime-identity' };
  const original = patientFormRuntime(context);
  expect(patientFormRuntime({ ...context })).toBe(original);
  expect(patientFormRuntime({ ...context, apiBase: '/api/federation/' })).toBe(original);
  for (const change of [{ token: 'new-token' }, { impToken: 'patient-b' },
    { apiBase: 'https://other.example.test/api' }, { sessionKey: 'runtime-other' }]) {
    const other = patientFormRuntime({ ...context, ...change });
    expect(other.store).not.toBe(original.store);
    expect(other.queryClient).not.toBe(original.queryClient);
    expect(other.client).not.toBe(original.client);
  }
  expect(original.client.defaults.headers['X-Imp-Token']).toBe('patient-a');
  expect(original.client.defaults.headers.Authorization).toBe('Token self');
});

it('retires an idle detached context but never a pending queue before it drains', async () => {
  vi.useFakeTimers();
  const context = { apiBase: '/api', token: 'captured', sessionKey: 'runtime-retirement' };
  const runtime = patientFormRuntime(context);
  const release = retainPatientFormRuntime(context);
  vi.spyOn(runtime.client, 'get').mockImplementation(async (url) => ({ data: url.includes('writable-fields')
    ? { weight: { writable: true, target: 'patient_record' } }
    : { patient_info: { person_id: 12, weight: 60 } } }));
  let finish!: (result: unknown) => void;
  const patch = vi.spyOn(runtime.client, 'patch').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  await runtime.store.load();
  runtime.store.enqueue('weight', 80);
  release();
  await vi.advanceTimersByTimeAsync(31_000);
  expect(patientFormRuntime(context)).toBe(runtime);
  expect(patch).toHaveBeenCalledOnce();
  finish({ data: { patient_info: { person_id: 12, weight: 80 }, write: { applied: ['weight'] } } });
  await vi.advanceTimersByTimeAsync(0);
  expect(runtime.store.getSnapshot().values.weight).toBe(80);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(patientFormRuntime(context)).not.toBe(runtime);
});
