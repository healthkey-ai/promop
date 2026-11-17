/**
 * Add Field on the patient tabs (#721): a link to Field Concept Mapping at
 * the top and bottom of each tab that holds PatientRecord fields, for staff
 * and org admins outside patient mode, and the fields added that way shown
 * last on their tab.
 */
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import PatientDetail, { PatientDetailRoute } from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { __resetDiseaseCategoryCaches } from '@/hooks/useDiseaseCategories';
import type { User } from '@/hooks/useAuth';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

// The tabs' own fields are not what is under test.
vi.mock('@/components/PatientInfo/tabs/GeneralTab', () => ({ default: () => <div>general fields</div> }));
vi.mock('@/components/PatientInfo/tabs/DiseaseTab', () => ({ default: () => <div>disease fields</div> }));
vi.mock('@/components/PatientInfo/tabs/TreatmentTab', () => ({ default: () => <div>treatment fields</div> }));
vi.mock('@/components/PatientInfo/tabs/LabsTab', () => ({ default: () => <div>lab fields</div> }));
vi.mock('@/components/PatientInfo/tabs/GenomicsTab', () => ({ default: () => <div>genomics fields</div> }));
vi.mock('@/components/PatientInfo/tabs/BehaviorTab', () => ({ default: () => <div>behavior fields</div> }));
vi.mock('@/components/PatientInfo/tabs/WearableTab', () => ({ default: () => <div>wearable fields</div> }));
vi.mock('@/components/PatientInfo/tabs/ClinicalSummaryTab', () => ({ default: () => <div>history</div> }));
vi.mock('./PatientOmopTab', () => ({ default: () => <div>omop rows</div> }));
vi.mock('./PatientSourceCodesTab', () => ({ default: () => <div>source codes</div> }));

import api from '@/api/axios';

const STAFF = { id: 1, sub: 's', email: 's@example.org', name: 'Staff', is_staff: true } as User;
const ORG_ADMIN = { id: 2, sub: 'a', email: 'a@example.org', name: 'Admin', is_org_admin: true } as User;
const DOCTOR = {
  id: 3, sub: 'd', email: 'd@example.org', name: 'Doc',
  org_accesses: [{ role: 'doctor', org_name: 'Org', org_slug: 'org', expires_at: null }],
} as User;

const custom = (id: number, field_name: string, display_name: string, tab: string, diseases: string[] = []) => ({
  id, field_name, display_name, tab, field_type: 'text', mode: 'editable', unit: '',
  scope: diseases.length ? 'disease' : 'base', diseases,
});
const CUSTOM_FIELDS = [
  custom(1, 'm_protein_note', 'M protein note', 'disease', ['MM']),
  custom(2, 'her2_note', 'HER2 note', 'disease', ['BC']),
  custom(3, 'frailty_index', 'Frailty index', 'labs'),
];

// What the server lets this caller write on this patient.
const writable = { kind: 'direct', writable: true, target: 'patient_record', custom: true };
let descriptors: Record<string, unknown>;
let record: Record<string, unknown>;
// Disease Category management's mapping of disease names to categories.
let diseaseMappings: Array<{ disease_name: string; category: string }>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  __resetDiseaseCategoryCaches();
  diseaseMappings = [];
  descriptors = { m_protein_note: writable, her2_note: writable, frailty_index: writable };
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
  record = {
    person_id: 101, patient_name: 'Ada Lovelace', disease: 'Multiple Myeloma',
    custom_fields: { m_protein_note: 'IgG kappa', frailty_index: 'Fit' },
  };
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: descriptors });
    if (url.includes('custom-patient-fields')) return Promise.resolve({ data: CUSTOM_FIELDS });
    if (url.includes('disease-category-mappings')) return Promise.resolve({ data: diseaseMappings });
    if (url.includes('/patient-info/101/')) return Promise.resolve({ data: { patient_info: { ...record }, user: null } });
    return Promise.resolve({ data: [] });
  });
});

function MappingProbe() {
  const location = useLocation();
  return <div>field mappings {location.search} {JSON.stringify(location.state)}</div>;
}

async function renderPatient(user: User | null, state?: unknown) {
  render(
    <MemoryRouter initialEntries={[{ pathname: '/patient/101', state }]}>
      <Routes>
        <Route path="/patient/:personId" element={<PatientDetailRoute user={user} />} />
        <Route path="/field-mappings" element={<MappingProbe />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText('Ada Lovelace');
}

const openTab = (label: string) => fireEvent.click(
  within(screen.getByRole('navigation', { name: 'Patient info tabs' })).getByRole('button', { name: label }),
);
const addFieldLinks = () => screen.queryAllByRole('link', { name: 'Add Field' });
const hrefs = () => addFieldLinks().map((link) => link.getAttribute('href'));

describe('PatientDetail — Add Field', () => {
  it('offers Add Field at the top and bottom of each tab that holds fields', async () => {
    await renderPatient(STAFF);
    expect(hrefs()).toEqual(['/field-mappings?new=1&tab=general', '/field-mappings?new=1&tab=general']);
    // Top: beside the tab's heading. Bottom: after the tab's fields.
    const [top, bottom] = addFieldLinks();
    const fields = screen.getByText('general fields');
    expect(top.compareDocumentPosition(fields) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(fields.compareDocumentPosition(bottom) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    for (const [label, tab] of [
      ['Treatment', 'treatment'], ['Labs', 'labs'], ['Genomics', 'genomics'],
      ['Behavior', 'behavior'], ['Wearables', 'wearable'],
    ]) {
      openTab(label);
      expect(hrefs()).toEqual([`/field-mappings?new=1&tab=${tab}`, `/field-mappings?new=1&tab=${tab}`]);
    }
  });

  it("names the record's disease from the disease tab", async () => {
    await renderPatient(ORG_ADMIN);
    openTab('Multiple Myeloma');
    expect(hrefs()).toEqual([
      '/field-mappings?new=1&tab=disease&disease=MM',
      '/field-mappings?new=1&tab=disease&disease=MM',
    ]);
  });

  it("uses Disease Category management's category for the record's disease over the editor's name match", async () => {
    // The editor shows a diffuse large B-cell lymphoma on its lymphoma tab;
    // the record's category is DLBCL, not FL.
    record = { ...record, disease: 'Diffuse Large B-Cell Lymphoma' };
    diseaseMappings = [{ disease_name: 'Diffuse large B-cell lymphoma', category: 'DLBCL' }];
    await renderPatient(STAFF);
    openTab('Follicular Lymphoma');
    await waitFor(() => expect(hrefs()).toEqual([
      '/field-mappings?new=1&tab=disease&disease=DLBCL',
      '/field-mappings?new=1&tab=disease&disease=DLBCL',
    ]));
  });

  it("names no disease for a record Disease Category management maps to Other", async () => {
    record = { ...record, disease: 'Mantle Cell Lymphoma' };
    diseaseMappings = [{ disease_name: 'Mantle Cell Lymphoma', category: 'Other' }];
    await renderPatient(STAFF);
    openTab('Mantle Cell Lymphoma');
    await waitFor(() => expect(hrefs()).toEqual([
      '/field-mappings?new=1&tab=disease', '/field-mappings?new=1&tab=disease',
    ]));
  });

  it('has no Add Field on tabs that hold no PatientRecord fields', async () => {
    await renderPatient(STAFF);
    for (const label of ['History', 'Source Codes', 'OMOP']) {
      openTab(label);
      expect(addFieldLinks()).toHaveLength(0);
    }
  });

  it('is offered to staff and org admins only, and leaves nothing behind for anyone else', async () => {
    await renderPatient(DOCTOR);
    expect(addFieldLinks()).toHaveLength(0);
    // No empty row where the bottom link would be: the tab ends at its fields.
    expect(screen.getByText('general fields').nextElementSibling).toBeNull();
    openTab('Treatment');
    expect(addFieldLinks()).toHaveLength(0);
    expect(screen.getByText('treatment fields').nextElementSibling).toBeNull();
  });

  it('is not offered in patient mode, even to a staff account', async () => {
    render(
      <MemoryRouter>
        <PatientDetail personIdOverride="101" patientMode user={STAFF} />
      </MemoryRouter>,
    );
    await screen.findByText('general fields');
    expect(addFieldLinks()).toHaveLength(0);
    openTab('Multiple Myeloma');
    expect(addFieldLinks()).toHaveLength(0);
    // The added field itself is part of the record, so the patient sees it.
    expect(await screen.findByText('M protein note')).toBeInTheDocument();
  });

  it('goes to Field Concept Mapping with the tab to come back to, and nothing about the patient in the URL', async () => {
    await renderPatient(STAFF);
    openTab('Labs');
    fireEvent.click(addFieldLinks()[0]);
    const probe = await screen.findByText(/^field mappings/);
    expect(probe).toHaveTextContent('?new=1&tab=labs');
    expect(probe).toHaveTextContent('{"addFieldReturn":{"path":"/patient/101","tab":"labs"}}');
  });

  it('opens on the tab it is returned to', async () => {
    await renderPatient(STAFF, { tab: 'labs' });
    expect(screen.getByRole('heading', { level: 2, name: 'Labs' })).toBeInTheDocument();
    expect(screen.getByText('lab fields')).toBeInTheDocument();
    // Choosing another tab leaves the returned one.
    openTab('General');
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toBeInTheDocument();
  });
});

describe('PatientDetail — added fields', () => {
  it('shows a field added to the disease tab for that disease, last on the tab', async () => {
    await renderPatient(DOCTOR);
    expect(screen.queryByText('M protein note')).not.toBeInTheDocument();
    openTab('Multiple Myeloma');
    const added = await screen.findByTestId('custom-patient-fields');
    expect(within(added).getByText('M protein note')).toBeInTheDocument();
    expect(within(added).getByDisplayValue('IgG kappa')).toBeInTheDocument();
    // Another disease's field is not part of this record.
    expect(screen.queryByText('HER2 note')).not.toBeInTheDocument();
    const fields = screen.getByText('disease fields');
    expect(fields.compareDocumentPosition(added) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows the fields of the record's mapped category, not of the tab it is shown on", async () => {
    record = { ...record, disease: 'Plasma cell leukemia' };
    diseaseMappings = [{ disease_name: 'Plasma Cell Leukemia', category: 'MM' }];
    await renderPatient(DOCTOR);
    // No disease tab for a name the editor does not recognise, so the MM
    // field has no tab to show on; a base field still shows.
    openTab('Labs');
    expect(await screen.findByText('Frailty index')).toBeInTheDocument();
  });

  it('shows a base field on its tab whatever the disease', async () => {
    record = { ...record, disease: 'Breast Cancer' };
    await renderPatient(DOCTOR);
    openTab('Labs');
    expect(await screen.findByText('Frailty index')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Fit')).toBeInTheDocument();
    openTab('Breast Cancer');
    expect(await screen.findByText('HER2 note')).toBeInTheDocument();
    expect(screen.queryByText('M protein note')).not.toBeInTheDocument();
  });

  it('loads the definitions once, not on every tab change', async () => {
    await renderPatient(DOCTOR);
    openTab('Labs');
    await screen.findByText('Frailty index');
    openTab('Treatment');
    // A tab with no fields of its own, then back to one that has them.
    openTab('History');
    expect(screen.queryByTestId('custom-patient-fields')).not.toBeInTheDocument();
    openTab('Multiple Myeloma');
    await screen.findByText('M protein note');
    const reads = (api.get as ReturnType<typeof vi.fn>).mock.calls.filter(([url]) => String(url).includes('custom-patient-fields'));
    expect(reads).toHaveLength(1);
  });
});

/** Editing an added field (#2669): it saves with the record, under custom_fields. */
describe('PatientDetail — editing an added field', () => {
  afterEach(() => vi.useRealTimers());

  const patches = () => (api.patch as ReturnType<typeof vi.fn>).mock.calls;

  it("saves an edit through the record PATCH, keeping the other added fields' values", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderPatient(DOCTOR);
    openTab('Multiple Myeloma');
    const input = await screen.findByDisplayValue('IgG kappa');
    await waitFor(() => expect(input).toBeEnabled());

    fireEvent.change(input, { target: { value: 'IgA lambda' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0]).toEqual(['/patient-info/101/', {
      custom_fields: { m_protein_note: 'IgA lambda', frailty_index: 'Fit' },
    }]);
    expect(screen.getByDisplayValue('IgA lambda')).toBeInTheDocument();
  });

  it('sends null for a value that was cleared', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderPatient(DOCTOR);
    openTab('Multiple Myeloma');
    const input = await screen.findByDisplayValue('IgG kappa');
    await waitFor(() => expect(input).toBeEnabled());

    fireEvent.change(input, { target: { value: '' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0][1]).toEqual({ custom_fields: { m_protein_note: null, frailty_index: 'Fit' } });
  });

  it('offers no input to a caller the server does not let write', async () => {
    descriptors = { m_protein_note: {
      kind: 'direct', writable: false, read_only_for_caller: true,
      reason: 'You have read-only access to this patient record.',
    } };
    await renderPatient(DOCTOR);
    openTab('Multiple Myeloma');
    const input = await screen.findByDisplayValue('IgG kappa');
    expect(input).toBeDisabled();
    expect(await screen.findByTestId('reason-m_protein_note')).toHaveTextContent('read-only access');
  });

  it('lets the patient edit an added field on their own record', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(
      <MemoryRouter>
        <PatientDetail personIdOverride="101" patientMode user={DOCTOR} />
      </MemoryRouter>,
    );
    await screen.findByText('general fields');
    openTab('Multiple Myeloma');
    const input = await screen.findByDisplayValue('IgG kappa');
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: 'IgG kappa, stable' } });
    await act(async () => { vi.advanceTimersByTime(2100); });
    await waitFor(() => expect(patches()).toHaveLength(1));
    expect(patches()[0][1]).toEqual({
      custom_fields: { m_protein_note: 'IgG kappa, stable', frailty_index: 'Fit' },
    });
  });
});
