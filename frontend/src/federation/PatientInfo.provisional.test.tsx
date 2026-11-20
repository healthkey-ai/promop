/**
 * Provisional values in the federated (host app) patient view (#2659).
 *
 * The host shows a patient their own record, with a proposal laid over a field
 * the approved record leaves empty and listed in `provisional_fields`. The view
 * marks it, never saves it as approved untouched, and saves it as the patient's
 * answer once touched.
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

vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ grouped: new Map(), isLoading: false })),
}));

const DESCRIPTORS = {
  anc_thousand_per_ul: {
    kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number',
    projection: {
      omop_table: 'measurement', concept_id: 3013650, code: '751-8',
      type_concept_id: 32856, source_value: '751-8',
    },
  },
  email: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'string' },
};

const PATIENT_INFO = {
  person_id: 261,
  anc_thousand_per_ul: '3.1',                       // a proposal
  provisional_fields: { anc_thousand_per_ul: 77 },
  id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1,
  email: 'howell@example.org',
};

let client: AxiosInstance;
let qc: QueryClient;
let patches: Array<[string, Record<string, unknown>]>;
// What the server holds: a save settles the marks of the fields it wrote, and the next
// read says so. The PATCH response itself carries none, as the real one does not.
let server: Record<string, unknown>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  resetClinicalTransport();
  patches = [];
  server = { ...PATIENT_INFO, provisional_fields: { ...PATIENT_INFO.provisional_fields } };
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client = {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({
          data: {
            patient_info: { ...server },
            user: { name: 'Alishia Howell', email: 'howell@example.org' },
            patient_name: 'Alishia Tawny Howell',
          },
        });
      }
      return Promise.resolve({ data: [] });
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn((url: string, body: Record<string, unknown>) => {
      patches.push([url, body]);
      const { rejected_proposals: _declared, ...written } = body;
      const marks = { ...(server.provisional_fields as Record<string, number>) };
      for (const field of Object.keys(written)) delete marks[field];
      server = { ...server, ...written, provisional_fields: marks };
      const { provisional_fields: _marks, ...response } = server;
      return Promise.resolve({
        data: { patient_info: response, patient_name: 'Alishia Tawny Howell' },
      });
    }),
  } as unknown as AxiosInstance;
});

afterEach(() => {
  vi.useRealTimers();
  resetClinicalTransport();
});

async function renderAndOpenLabs() {
  render(<PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc} />);
  await waitFor(() => expect(screen.getByText('Labs')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
  await waitFor(() => expect(screen.getByDisplayValue('3.1')).toBeInTheDocument());
}

describe('federated PatientInfo provisional values', () => {
  it('marks a proposed field in the patient\'s wording', async () => {
    await renderAndOpenLabs();

    const badge = screen.getByTestId('provisional-anc_thousand_per_ul');
    expect(badge).toHaveTextContent('Provisional 77%');
    expect(badge.title).toMatch(/Not yet confirmed/);
  });

  it('never saves an untouched proposal as approved', async () => {
    await renderAndOpenLabs();
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('howell@example.org'), { target: { value: 'new@example.org' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches.length).toBeGreaterThan(0));
    const body = patches.at(-1)![1];
    expect(body).toMatchObject({ email: 'new@example.org' });
    expect('anc_thousand_per_ul' in body || 'provisional_fields' in body).toBe(false);
  });

  it('saves a touched proposal as the patient\'s answer and drops its badge', async () => {
    await renderAndOpenLabs();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('3.1'), { target: { value: '4' } });

    expect(screen.queryByTestId('provisional-anc_thousand_per_ul')).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches.length).toBeGreaterThan(0));
    expect(patches.at(-1)![1]).toEqual({ anc_thousand_per_ul: 4 });
    // The PATCH response has no marks: the badge must not come back with the merge.
    await waitFor(() => expect(screen.queryByTestId('provisional-anc_thousand_per_ul')).not.toBeInTheDocument());
    expect(screen.queryByTestId('provisional-anc_thousand_per_ul')).not.toBeInTheDocument();
    // A save that wrote a marked field reads the record again, so the marks that went with
    // it (a unit, a calculation, the disease group) follow the server's answer.
    await waitFor(() => expect((client.get as ReturnType<typeof vi.fn>).mock.calls
      .filter(([url]: [string]) => url.includes('/patient-info/me/')).length).toBeGreaterThan(1));
  });

  it('rejects a proposal by clearing it, naming only the cleared marked field (#2671)', async () => {
    await renderAndOpenLabs();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('3.1'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    fireEvent.change(screen.getByDisplayValue('howell@example.org'), { target: { value: '' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches.length).toBeGreaterThan(0));
    const body = patches.at(-1)![1];
    // The email was cleared too but showed no proposal: it is sent, and not declared.
    expect(body).toEqual({ anc_thousand_per_ul: null, email: '', rejected_proposals: ['anc_thousand_per_ul'] });
    fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
    await waitFor(() => expect(screen.queryByTestId('provisional-anc_thousand_per_ul')).not.toBeInTheDocument());
  });

  it('declares nothing when a marked field is saved with a value', async () => {
    await renderAndOpenLabs();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('3.1'), { target: { value: '5' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches.length).toBeGreaterThan(0));
    expect('rejected_proposals' in patches.at(-1)![1]).toBe(false);
  });
});
