/**
 * PatientRecord-first writes in the federated view.
 *
 * All edits go through the PatientRecord PATCH. The backend handles OMOP
 * projection for fields with approved mappings. Profile fields (person target)
 * go to the persons endpoint.
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
    kind: 'direct', writable: true, target: 'patient_record',
    value_kind: 'number',
    projection: {
      omop_table: 'measurement', concept_id: 3013650, code: '751-8',
      type_concept_id: 32856, source_value: '751-8',
    },
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
      // The /me/ PATCH returns the same wrapped shape as GET: the full record
      // under patient_info plus the display name at the top level.
      const merged = { ...PATIENT_INFO, ...(body as Record<string, unknown>) };
      return Promise.resolve({
        data: {
          patient_info: merged,
          patient_name: 'Alishia Tawny Howell',
        },
      });
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
    await renderAndLoad();
    await openBloodTab();
    await waitFor(() =>
      expect(client.get).toHaveBeenCalledWith(
        '/api/v1/patient-records/writable-fields/',
        expect.anything(),
      ),
    );
  });

  it('writes a clinical edit through the PatientRecord PATCH', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    const [, body] = patches.find(([u]) => u.includes('/patient-info/me/'))!;
    expect(body).toMatchObject({ anc_thousand_per_ul: 5.5 });
    // No separate OMOP endpoint post
    expect(posts).toEqual([]);
  });

  it('sends the clinical edit in the PATCH, not to a separate OMOP endpoint', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    // No measurement/observation POST should happen
    expect(posts).toEqual([]);
  });

  it('never sends a mapped field or alias when it does PATCH', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    const [, body] = patches.find(([u]) => u.includes('/patient-info/me/'))!;
    expect(body).not.toHaveProperty('absolute_neutrophile_count');
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

  it('retains the saved value in the input after the PATCH completes (#1083)', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() =>
      expect(patches.some(([u]) => u.includes('/patient-info/me/'))).toBe(true),
    );
    expect(screen.getByDisplayValue('a.howell@example.org')).toBeInTheDocument();
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
