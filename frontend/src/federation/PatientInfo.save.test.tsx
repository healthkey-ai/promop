/**
 * The federated view saves through the same split as the provider editor.
 *
 * It renders the very same tab components — BloodTab, LabsTab — but had its own
 * `doSave` that PATCHed the whole record and never wrote an OMOP fact at all. So
 * every clinical edit here was a write to an OMOP-mapped column:
 *
 *   OMOP-mapped PatientRecord fields are read-only. Write a complete clinical
 *   fact to the appropriate OMOP resource, then rederive the record.
 *
 * Sharing the tabs but not the write path is what let one half get fixed while
 * the other stayed broken, so these pin the behaviour independently rather than
 * trusting that the two implementations stay in step.
 */
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { AxiosInstance } from 'axios';
import { QueryClient } from '@tanstack/react-query';
import PatientInfo from './PatientInfo';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { resetClinicalTransport } from '@/api/clinicalTransport';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

const DESCRIPTORS = {
  anc_thousand_per_ul: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 3013650, code: '751-8', value_kind: 'number',
    type_concept_id: 32856, source_value: '751-8',
  },
  absolute_neutrophile_count: {
    kind: 'alias', writable: false, canonical: 'anc_thousand_per_ul',
    reason: 'Mirrors anc_thousand_per_ul; edit that field instead.',
  },
};

const PATIENT_INFO = {
  person_id: 261,
  anc_thousand_per_ul: 3.1,
  absolute_neutrophile_count: '3.10',
  id: 7,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1,
  email: 'howell@example.org',
};

let client: AxiosInstance;
let qc: QueryClient;

/** Every request the host client sees, so a test can assert on the ones the
 *  component chose to make rather than on a single mocked method. */
let posts: Array<[string, unknown]>;
let patches: Array<[string, unknown]>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  resetClinicalTransport();
  posts = [];
  patches = [];

  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  client = {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({
          data: {
            patient_info: { ...PATIENT_INFO },
            user: { name: 'Alishia Howell', email: 'howell@example.org' },
            patient_name: 'Alishia Tawny Howell',
          },
        });
      }
      // measurement lookup during supersede
      return Promise.resolve({ data: [] });
    }),
    post: vi.fn((url: string, body: unknown) => {
      posts.push([url, body]);
      return Promise.resolve({ data: { measurement_id: 1 } });
    }),
    patch: vi.fn((url: string, body: unknown) => {
      patches.push([url, body]);
      return Promise.resolve({ data: {} });
    }),
  } as unknown as AxiosInstance;
});

afterEach(() => {
  vi.useRealTimers();
  resetClinicalTransport();
});

async function renderAndLoad() {
  render(
    <PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc} />,
  );
  await waitFor(() => expect(screen.getByText('Blood')).toBeInTheDocument());
}

async function openBloodTab() {
  fireEvent.click(screen.getByRole('button', { name: 'Blood' }));
  await waitFor(() => expect(screen.getByDisplayValue('3.1')).toBeInTheDocument());
}

async function editAndSave(displayValue: string, next: string) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  fireEvent.change(screen.getByDisplayValue(displayValue), { target: { value: next } });
  await act(async () => { vi.advanceTimersByTime(2100); });
}

describe('federated PatientInfo save', () => {
  it('fetches the descriptor through the host client, not the app singleton', async () => {
    // The host injects a client carrying its own auth and origin. Reaching for
    // the standalone app's axios instance would send the request unauthenticated
    // to whatever origin the remote was served from — and a descriptor that fails
    // to load renders every clinical field read-only.
    await renderAndLoad();
    await openBloodTab();
    await waitFor(() =>
      expect(client.get).toHaveBeenCalledWith(
        '/api/v1/patient-records/writable-fields/',
      ),
    );
  });

  it('writes a clinical edit as an OMOP measurement', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(posts.length).toBeGreaterThan(0));
    const [url, body] = posts.at(-1)!;
    expect(url).toBe('/api/v1/measurements/');
    expect(body).toMatchObject({
      person: 261,
      measurement_source_value: '751-8',
      value_as_number: 5.5,
    });
  });

  it('does not PATCH the record for a clinical-only edit', async () => {
    // This is the bug: the whole record went into the PATCH, so the edited
    // column — OMOP-mapped — was refused with a 405.
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(posts.length).toBeGreaterThan(0));
    const recordPatches = patches.filter(([u]) => u.includes('/patient-info/me/'));
    expect(recordPatches).toEqual([]);
  });

  it('never sends a mapped field or an alias when it does PATCH', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    const [, body] = patches.find(([u]) => u.includes('/patient-info/me/'))!;
    for (const f of Object.keys(DESCRIPTORS)) {
      expect(body).not.toHaveProperty(f);
    }
  });

  it('never sends lifecycle columns', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    const [, body] = patches.find(([u]) => u.includes('/patient-info/me/'))!;
    for (const f of ['id', 'created_at', 'updated_at', 'derivation_version']) {
      expect(body).not.toHaveProperty(f);
    }
  });

  it('sends only the changed projection field', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    const [, body] = patches.find(([u]) => u.includes('/patient-info/me/'))!;
    expect(body).toEqual({ email: 'a.howell@example.org' });
  });

  it('attempts nothing when the descriptor cannot be fetched', async () => {
    (client.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) return Promise.reject(new Error('offline'));
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({
          data: {
            patient_info: { ...PATIENT_INFO },
            user: { name: 'Alishia Howell', email: 'howell@example.org' },
            patient_name: 'Alishia Tawny Howell',
          },
        });
      }
      return Promise.resolve({ data: [] });
    });

    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    expect(patches.filter(([u]) => u.includes('/patient-info/me/'))).toEqual([]);
    expect(posts).toEqual([]);
  });
});
