/**
 * The federated form's tab order (#5610).
 *
 * Genomics must sit directly after Labs and directly before Behavior, the panel
 * must match the highlighted label, and the `?tab=` deep links must keep
 * landing on the panel they name. The deep link used to index a second copy of
 * the order, so these tests read the strip itself rather than a constant.
 */
import { render, screen, within, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { AxiosInstance } from 'axios';
import { QueryClient } from '@tanstack/react-query';
import PatientInfo from './PatientInfo';
import { federatedTabKeys, tabIndexFor } from './tabOrder';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { resetClinicalTransport } from '@/api/clinicalTransport';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ grouped: new Map(), isLoading: false })),
}));

const DESCRIPTORS = {
  email: { kind: 'direct', writable: true, target: 'patient_record', control: 'email' },
};

function patientInfo(disease: string) {
  return {
    person_id: 261,
    disease,
    email: 'howell@example.org',
    no_pre_existing_conditions: true,
    preexisting_conditions: [] as string[],
  };
}

let client: AxiosInstance;

function makeClient(disease: string) {
  return {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({
          data: {
            patient_info: patientInfo(disease),
            user: { name: 'Alishia Howell', email: 'howell@example.org' },
            patient_name: 'Alishia Tawny Howell',
          },
        });
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
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  resetClinicalTransport();
});

afterEach(() => {
  resetClinicalTransport();
  window.history.replaceState({}, '', '/');
});

async function renderForm(options: { disease?: string; federated?: boolean } = {}) {
  client = makeClient(options.disease ?? 'Multiple Myeloma');
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc} federated={options.federated ?? true} />);
  await waitFor(() => expect(screen.getByRole('navigation', { name: 'Patient info tabs' })).toBeInTheDocument());
}

/** The strip's labels, in render order. */
function tabLabels(): string[] {
  const nav = screen.getByRole('navigation', { name: 'Patient info tabs' });
  return within(nav).getAllByRole('button').map((button) => button.textContent ?? '');
}

/** The tab the strip highlights, i.e. the one whose panel is rendered. */
function activeLabel(): string {
  const nav = screen.getByRole('navigation', { name: 'Patient info tabs' });
  const active = within(nav).getAllByRole('button')
    .filter((button) => button.className.includes('border-primary'));
  expect(active).toHaveLength(1);
  return active[0].textContent ?? '';
}

function panelHeading(label: string) {
  return screen.getByRole('heading', { level: 2, name: label });
}

describe('federatedTabKeys', () => {
  it.each([
    ['with the disease tab', true, true],
    ['without the disease tab', false, true],
    ['native, with the disease tab', true, false],
    ['native, without the disease tab', false, false],
  ])('places genomics after labs and before behavior %s', (_name, showDiseaseTab, federated) => {
    const keys = federatedTabKeys(showDiseaseTab, federated);
    expect(keys.indexOf('genomics')).toBe(keys.indexOf('labs') + 1);
    expect(keys.indexOf('behavior')).toBe(keys.indexOf('genomics') + 1);
    expect(keys[0]).toBe('general');
    expect(keys.at(-1)).toBe('history');
    expect(keys.includes('wearable')).toBe(!federated);
    expect(keys.includes('disease')).toBe(showDiseaseTab);
  });
});

describe('tabIndexFor', () => {
  it('resolves current keys and legacy aliases against the same order', () => {
    expect(tabIndexFor('genomics', true, true)).toBe(federatedTabKeys(true, true).indexOf('genomics'));
    expect(tabIndexFor('blood', true, true)).toBe(tabIndexFor('labs', true, true));
    expect(tabIndexFor('multiple_myeloma', true, true)).toBe(1);
    expect(tabIndexFor('genomics', false, true)).toBe(
      federatedTabKeys(false, true).indexOf('genomics'),
    );
  });

  it('returns -1 for an absent or unknown tab', () => {
    expect(tabIndexFor(null, true, true)).toBe(-1);
    expect(tabIndexFor('no-such-tab', true, true)).toBe(-1);
  });
});

describe('federated patient form tab strip', () => {
  it('renders Genomics between Labs and Behavior when a disease tab is shown', async () => {
    await renderForm({ disease: 'Multiple Myeloma' });
    expect(tabLabels()).toEqual([
      'General', 'Multiple Myeloma', 'Treatment', 'Labs', 'Genomics', 'Behavior', 'History',
    ]);
  });

  it('keeps the order when no disease-specific tab is shown', async () => {
    await renderForm({ disease: 'Something Else' });
    expect(tabLabels()).toEqual([
      'General', 'Treatment', 'Labs', 'Genomics', 'Behavior', 'History',
    ]);
  });

  it('keeps Wearable in place for the native, non-federated form', async () => {
    await renderForm({ disease: 'Multiple Myeloma', federated: false });
    expect(tabLabels()).toEqual([
      'General', 'Multiple Myeloma', 'Treatment', 'Labs', 'Genomics', 'Behavior', 'Wearable', 'History',
    ]);
  });

  it.each(['Genomics', 'Labs', 'Behavior'])('opens the %s panel whose label it highlights', async (label) => {
    await renderForm();
    const nav = screen.getByRole('navigation', { name: 'Patient info tabs' });
    within(nav).getByRole('button', { name: label, exact: true }).click();
    await waitFor(() => expect(panelHeading(label)).toBeInTheDocument());
    expect(activeLabel()).toBe(label);
  });
});

describe('federated patient form tab deep links', () => {
  it.each([
    ['genomics', 'Genomics'],
    ['labs', 'Labs'],
    ['behavior', 'Behavior'],
    // Historical alias: `?tab=blood` still means Labs.
    ['blood', 'Labs'],
  ])('?tab=%s opens the %s panel', async (requested, expected) => {
    window.history.replaceState({}, '', `/?tab=${requested}`);
    await renderForm();
    await waitFor(() => expect(panelHeading(expected)).toBeInTheDocument());
    expect(activeLabel()).toBe(expected);
  });

  it('?tab=genomics still lands on Genomics when the disease tab shifts every index', async () => {
    window.history.replaceState({}, '', '/?tab=genomics');
    await renderForm({ disease: 'Follicular Lymphoma' });
    await waitFor(() => expect(panelHeading('Genomics')).toBeInTheDocument());
    expect(activeLabel()).toBe('Genomics');
    expect(tabLabels().indexOf('Genomics')).toBe(tabLabels().indexOf('Labs') + 1);
  });
});
