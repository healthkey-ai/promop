/** @vitest-environment jsdom */
import { act, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PatientFormStore } from './patientFormStore';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Mounted {
  client: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> };
  store: PatientFormStore;
}

const runtimes = new Map<string, Mounted>();
const disposers: Array<() => void> = [];

function runtimeFor(apiBase = '') {
  const existing = runtimes.get(apiBase);
  if (existing) return existing;
  const client = {
    get: vi.fn(async (url: string) => ({
      data: url.includes('writable-fields')
        ? { therapies: { writable: false, target: 'episode' } }
        : {
            patient_info: {
              person_id: 12,
              disease: 'Multiple Myeloma',
              therapy_lines_count: '1',
              therapy_history_complete: false,
              lines_of_therapy: [
                { line: 1, episode_id: 11, regimen: 'VRd', start_date: '2024-01-01' },
              ],
            },
            write_enabled: true,
          },
    })),
    post: vi.fn(async (_url: string, body: unknown) => ({
      data: { therapy_history_complete: true, patient_info: {}, ...(body as object) },
    })),
  };
  const store = new PatientFormStore(client as never, apiBase);
  const mounted = { client, store };
  runtimes.set(apiBase, mounted);
  return mounted;
}

vi.mock('./patientFormRuntime', () => ({
  patientFormRuntime: (opts: { apiBase?: string }) => {
    const mounted = runtimeFor(opts.apiBase ?? '');
    return { client: mounted.client, queryClient: new QueryClient(), store: mounted.store };
  },
  retainPatientFormRuntime: () => () => {},
}));

vi.mock('./injectStyles', () => ({ injectStyles: () => {} }));

import { mountTherapyLines } from './widget.patient-info';

afterEach(async () => {
  await act(async () => {
    for (const dispose of disposers.splice(0)) dispose();
  });
  document.body.innerHTML = '';
  runtimes.clear();
});

const complete = (root: HTMLElement) =>
  root.querySelector<HTMLInputElement>(
    'input[aria-label="Treatment history is complete"]',
  )!;

describe('mountTherapyLines', () => {
  it('writes through the client of its own mount, not a shared transport', async () => {
    const rootA = document.createElement('div');
    const rootB = document.createElement('div');
    document.body.append(rootA, rootB);

    const a = runtimeFor('/api/a');
    const b = runtimeFor('/api/b');
    await a.store.load();
    await b.store.load();
    await act(async () => {
      disposers.push(mountTherapyLines(rootA, { apiBase: '/api/a', readOnly: false }));
      disposers.push(mountTherapyLines(rootB, { apiBase: '/api/b', readOnly: false }));
    });
    await act(async () => {});

    fireEvent.click(complete(rootA));
    await waitFor(() => expect(a.client.post).toHaveBeenCalledWith(
      '/api/a/v1/therapy-history/completeness/',
      { person: 12, complete: true },
    ));
    expect(b.client.post).not.toHaveBeenCalled();

    fireEvent.click(complete(rootB));
    await waitFor(() => expect(b.client.post).toHaveBeenCalledWith(
      '/api/b/v1/therapy-history/completeness/',
      { person: 12, complete: true },
    ));
  });
});
