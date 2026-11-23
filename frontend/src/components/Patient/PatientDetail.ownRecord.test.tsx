/**
 * What a person does only for themselves on the Wearables tab (the Garmin
 * sync and connection, #2794, #2802, and file uploads with their history,
 * #2819) is offered on their own record, in patient mode or in the provider
 * console, and on nobody else's (#2816).
 */
import { render, screen, fireEvent, within } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
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

vi.mock('@/components/PatientInfo/tabs/GeneralTab', () => ({ default: () => <div>general fields</div> }));
vi.mock('@/components/PatientInfo/tabs/DiseaseTab', () => ({ default: () => <div>disease fields</div> }));
vi.mock('@/components/PatientInfo/tabs/TreatmentTab', () => ({ default: () => <div>treatment fields</div> }));
vi.mock('@/components/PatientInfo/tabs/LabsTab', () => ({ default: () => <div>lab fields</div> }));
vi.mock('@/components/PatientInfo/tabs/GenomicsTab', () => ({ default: () => <div>genomics fields</div> }));
vi.mock('@/components/PatientInfo/tabs/BehaviorTab', () => ({ default: () => <div>behavior fields</div> }));
// The tab says what it was told; its own panels are tested in WearableTab.test.tsx.
vi.mock('@/components/PatientInfo/tabs/WearableTab', () => ({
  default: ({ canSyncOwnDevice, ownRecord }: { canSyncOwnDevice?: boolean; ownRecord?: boolean }) => (
    <div>
      <span>{canSyncOwnDevice ? 'own device sync offered' : 'own device sync withheld'}</span>
      <span>{ownRecord ? 'uploads offered' : 'uploads withheld'}</span>
    </div>
  ),
}));
vi.mock('@/components/PatientInfo/tabs/ClinicalSummaryTab', () => ({ default: () => <div>history</div> }));
vi.mock('./PatientOmopTab', () => ({ default: () => <div>omop rows</div> }));
vi.mock('./PatientSourceCodesTab', () => ({ default: () => <div>source codes</div> }));
vi.mock('./AllergyList', () => ({ default: () => <div>allergies</div> }));
vi.mock('./ImmunizationList', () => ({ default: () => <div>immunizations</div> }));
vi.mock('./PatientSurveys', () => ({ default: () => <div>surveys</div> }));

import api from '@/api/axios';

const patientLink = (person_id: number) => ({
  role: 'patient', scope: 'patient', source: 'patient_link', person_id, expires_at: null,
});

// Staff and org admin who added themselves as a patient (#2773): the console,
// not patient mode, and no `person_id` of their own on the account.
const ADMIN_AND_PATIENT = {
  id: 1, sub: 'a', email: 'a@example.org', name: 'Admin', is_staff: true, is_org_admin: true,
  is_patient: false, person_id: null,
  effective_roles: [
    { role: 'staff', scope: 'platform', source: 'staff_flag', expires_at: null },
    patientLink(101),
  ],
} as User;
const ADMIN_ONLY = {
  id: 2, sub: 'b', email: 'b@example.org', name: 'Admin', is_staff: true, is_org_admin: true,
  effective_roles: [{ role: 'staff', scope: 'platform', source: 'staff_flag', expires_at: null }],
} as User;
const PATIENT = {
  id: 3, sub: 'p', email: 'p@example.org', name: 'Pat', is_patient: true, person_id: 101,
  effective_roles: [patientLink(101)],
} as User;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  __resetDiseaseCategoryCaches();
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: {} });
    const match = url.match(/\/patient-info\/(\d+)\//);
    if (match) {
      return Promise.resolve({
        data: { patient_info: { person_id: Number(match[1]), patient_name: 'Ada Lovelace' }, user: null },
      });
    }
    return Promise.resolve({ data: [] });
  });
});

async function openWearables(element: React.ReactNode, path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={element} />
        <Route path="/patient/:personId" element={element} />
      </Routes>
    </MemoryRouter>,
  );
  const tabs = await screen.findByRole('navigation', { name: 'Patient info tabs' });
  fireEvent.click(within(tabs).getByRole('button', { name: 'Wearables' }));
}

describe('PatientDetail — own-device sync on the Wearables tab (#2816)', () => {
  it('is offered to an admin on their own record in the provider console', async () => {
    await openWearables(<PatientDetailRoute user={ADMIN_AND_PATIENT} />, '/patient/101');
    expect(screen.getByText('own device sync offered')).toBeInTheDocument();
    expect(screen.getByText('uploads offered')).toBeInTheDocument();
  });

  it("is withheld from that admin on somebody else's record", async () => {
    await openWearables(<PatientDetailRoute user={ADMIN_AND_PATIENT} />, '/patient/202');
    expect(screen.getByText('own device sync withheld')).toBeInTheDocument();
    expect(screen.getByText('uploads withheld')).toBeInTheDocument();
  });

  it('is withheld from an admin who is no patient', async () => {
    await openWearables(<PatientDetailRoute user={ADMIN_ONLY} />, '/patient/101');
    expect(screen.getByText('own device sync withheld')).toBeInTheDocument();
    expect(screen.getByText('uploads withheld')).toBeInTheDocument();
  });

  it('is offered in patient mode, as before', async () => {
    await openWearables(<PatientDetail personIdOverride="101" patientMode user={PATIENT} />, '/');
    expect(screen.getByText('own device sync offered')).toBeInTheDocument();
    expect(screen.getByText('uploads offered')).toBeInTheDocument();
  });
});
