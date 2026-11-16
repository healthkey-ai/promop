/**
 * The federated form mounted read-only (the widget default).
 *
 * A read-only mount shows the record; it does not make every stored field look
 * computed, and it does not lock the controls that only navigate or display.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { AxiosInstance } from 'axios';
import { QueryClient } from '@tanstack/react-query';
import PatientInfo from './PatientInfo';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { resetClinicalTransport } from '@/api/clinicalTransport';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

// Hemoglobin has enough results for the Labs tab to offer its trend chart.
vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ isLoading: false, grouped: new Map([['718-7', {
    sourceValue: '718-7', unit: 'g/dL', values: [12.1, 12.4, 12.9].map((value, index) => ({
      value, measured_at: `2026-0${index + 1}-01`, unit: 'g/dL', source: '718-7',
    })),
  }]]) })),
}));

const writable = (extra: Record<string, unknown> = {}) =>
  ({ kind: 'direct', writable: true, target: 'patient_record', ...extra });

/** Every field the sweep visits is writable for this caller, so anything that
 *  stays disabled is disabled by the read-only mount and nothing else. */
const DESCRIPTORS = {
  email: writable({ control: 'email', label: 'Email' }),
  phone_number: writable({ control: 'text' }),
  date_of_birth: writable({ control: 'date' }),
  facility_name: writable({ control: 'text' }),
  smoking_status: writable({ control: 'select', options: [{ value: 'Never' }, { value: 'Former' }] }),
  hemoglobin_g_dl: writable({ control: 'number', projection: { omop_table: 'measurement', source_value: '718-7' } }),
  flipi_score_options: writable(),
  gelf_criteria_options: writable(),
  sct_date: writable({ control: 'date' }),
  therapy_lines_count: writable({ control: 'number' }),
  tp53_disruption: writable({ control: 'boolean' }),
  genetic_mutations: { kind: 'direct', writable: true, target: 'genomics' },
};

const FINDING = { id: 123, provenance: 'asserted', gene: 'BRCA1', variant: 'c.68_69delAG',
  origin: 'Germline', interpretation: 'Pathogenic' };
const UPLOAD = { id: 9, device_type: 'apple', filename: 'export.zip', samples_created: 3,
  uploaded_at: '2026-08-01T00:00:00Z', sample_summary: [{ metric: 'steps', date: '2026-08-01', value: 9000 }] };

const PATIENT_INFO = {
  person_id: 261,
  disease: 'Multiple Myeloma',
  email: 'howell@example.org',
  phone_number: '555-0100',
  no_pre_existing_conditions: true,
  preexisting_conditions: [] as string[],
};

let client: AxiosInstance;
let disease = PATIENT_INFO.disease;
/** The record read's write gate: false is a caller the server denies writes. */
let writeEnabled = true;

beforeEach(() => {
  vi.clearAllMocks();
  disease = PATIENT_INFO.disease;
  writeEnabled = true;
  __resetWritableFieldsCache();
  resetClinicalTransport();
  client = {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({ data: {
          patient_info: { ...PATIENT_INFO, disease }, patient_name: 'Alishia Tawny Howell',
          write_enabled: writeEnabled,
        } });
      }
      if (url.includes('genomics-catalog')) return Promise.resolve({ data: { markers: [], attributes: [] } });
      if (url.includes('genomics-legacy-cytogenetics')) {
        return Promise.resolve({ data: { results: [], next_cursor: null, legacy_summary: null } });
      }
      if (url.endsWith('/genomics/')) return Promise.resolve({ data: [FINDING] });
      if (url.includes('wearable-uploads')) return Promise.resolve({ data: [UPLOAD] });
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

async function renderReadOnly(federated = true, readOnly = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc}
    federated={federated} readOnly={readOnly} />);
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

/** Controls inside the tab panel a user can still operate. */
function operableControls(): HTMLElement[] {
  const panel = document.querySelector('[data-patient-info-panel]');
  expect(panel).not.toBeNull();
  return [...panel!.querySelectorAll<HTMLElement>(
    'input, select, textarea, button, [role="combobox"], [role="checkbox"], [contenteditable="true"]')]
    .filter((control) => {
      const field = control as HTMLInputElement;
      if (field.disabled || field.readOnly || field.type === 'hidden') return false;
      if (control.getAttribute('aria-disabled') === 'true') return false;
      return !control.closest('fieldset[disabled]');
    });
}

function controlName(control: HTMLElement): string {
  return (control.getAttribute('aria-label') ?? control.textContent ?? '').trim().replace(/\s+/g, ' ');
}

/** What a read-only reader may still operate: each one opens or navigates,
 *  none of them writes. */
const NON_EDITING = [
  /^View$/,
  /^View trend chart for /,
  /^Labs tab$/,
  /^Apple ?export\.zip/,
];

async function visit(label: string) {
  const nav = screen.getByRole('navigation', { name: 'Patient info tabs' });
  fireEvent.click(within(nav).getByRole('button', { name: label, exact: true }));
  await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: label })).toBeInTheDocument());
}

describe('read-only federated PatientInfo controls', () => {
  it('keeps navigation and view controls usable', async () => {
    await renderReadOnly(false);

    await visit('Labs');
    expect(await screen.findByRole('button', { name: /View trend chart for Hemoglobin/ })).toBeEnabled();

    await visit('Genomics');
    const view = await screen.findByRole('button', { name: 'View' });
    expect(view).toBeEnabled();
    fireEvent.click(view);
    const details = await screen.findByRole('dialog', { name: 'Genomic finding details' });
    expect(screen.queryByRole('button', { name: 'Edit result' })).not.toBeInTheDocument();
    fireEvent.keyDown(details, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await visit('Wearable');
    const expand = await screen.findByRole('button', { name: /export\.zip/ });
    expect(expand).toBeEnabled();
    fireEvent.click(expand);
    expect(await screen.findByRole('columnheader', { name: 'Metric' })).toBeInTheDocument();

    await visit('History');
    const labsLink = await screen.findByRole('button', { name: 'Labs tab' });
    expect(labsLink).toBeEnabled();
    fireEvent.click(labsLink);
    await waitFor(() => expect(screen.getByRole('heading', { level: 2, name: 'Labs' })).toBeInTheDocument());
  });

  it.each([
    ['Follicular Lymphoma', 'a read-only mount'],
    ['Multiple Myeloma', 'a read-only mount'],
    ['Follicular Lymphoma', 'a caller without write access'],
    ['Multiple Myeloma', 'a caller without write access'],
  ])('leaves nothing editable enabled on any tab (%s, %s)', async (name, mode) => {
    disease = name;
    const mount = mode === 'a read-only mount';
    writeEnabled = mount;
    await renderReadOnly(false, mount);
    const diseaseTab = name;
    const tabs = ['General', diseaseTab, 'Treatment', 'Labs', 'Genomics', 'Behavior', 'Wearable', 'History'];
    for (const tab of tabs) {
      await visit(tab);
      // Let each tab's own descriptor/data reads settle before sweeping.
      await waitFor(() => expect(screen.queryByText(/Loading/)).not.toBeInTheDocument());
      const editable = operableControls()
        .map(controlName)
        .filter((label) => !NON_EDITING.some((pattern) => pattern.test(label)));
      expect({ tab, editable }).toEqual({ tab, editable: [] });
    }
  });
});
