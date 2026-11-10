/**
 * The federated form mounted read-only (the widget default).
 *
 * A read-only mount shows the record; it does not make every stored field look
 * computed, and it does not lock the controls that only navigate or display.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
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
  email: { kind: 'direct', writable: true, target: 'patient_record', control: 'email', label: 'Email' },
  phone_number: { kind: 'direct', writable: true, target: 'patient_record', control: 'text' },
};

const PATIENT_INFO = {
  person_id: 261,
  disease: 'Multiple Myeloma',
  email: 'howell@example.org',
  phone_number: '555-0100',
  no_pre_existing_conditions: true,
  preexisting_conditions: [] as string[],
};

let client: AxiosInstance;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  resetClinicalTransport();
  client = {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({ data: {
          patient_info: { ...PATIENT_INFO }, patient_name: 'Alishia Tawny Howell',
        } });
      }
      if (url.includes('genomics-catalog')) return Promise.resolve({ data: { markers: [], attributes: [] } });
      if (url.includes('genomics-legacy-cytogenetics')) {
        return Promise.resolve({ data: { results: [], next_cursor: null, legacy_summary: null } });
      }
      return Promise.resolve({ data: [] });
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn(() => Promise.resolve({ data: {} })),
  } as unknown as AxiosInstance;
});

afterEach(() => {
  resetClinicalTransport();
  window.history.replaceState({}, '', '/');
});

async function renderReadOnly() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc} federated readOnly />);
  await waitFor(() => expect(screen.getByRole('navigation', { name: 'Patient info tabs' })).toBeInTheDocument());
}

describe('read-only federated PatientInfo', () => {
  it('shows stored fields without calling them derived', async () => {
    await renderReadOnly();
    const email = await waitFor(() => {
      const row = document.querySelector('[data-field="email"]');
      expect(row).not.toBeNull();
      return row as HTMLElement;
    });

    expect(within(email).getByText('howell@example.org', { exact: false })).toBeVisible();
    expect(screen.queryByText(/Derived value/)).not.toBeInTheDocument();
  });
});
