/**
 * The Source Codes tab under the patient page (#2822): its Generate OMOP run
 * is watched inside the tab, so the tab stays mounted once opened and can
 * still have the page re-read the record after the curator has moved on.
 */
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PatientDetailRoute } from './PatientDetail';
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
vi.mock('@/components/PatientInfo/tabs/LabsTab', () => ({ default: () => <div>lab fields</div> }));
vi.mock('./PatientOmopTab', () => ({ default: () => <div>omop rows</div> }));

const tab = vi.hoisted(() => ({ mounts: 0, refreshed: null as null | (() => void | Promise<void>) }));
vi.mock('./PatientSourceCodesTab', async () => {
  const { useEffect } = await import('react');
  function SourceCodesStub({ onRecordRefreshed }: { onRecordRefreshed?: () => void | Promise<void> }) {
    useEffect(() => { tab.mounts += 1; }, []);
    useEffect(() => { tab.refreshed = onRecordRefreshed ?? null; }, [onRecordRefreshed]);
    return <div>source codes</div>;
  }
  return { default: SourceCodesStub };
});

import api from '@/api/axios';

const STAFF = { id: 1, sub: 's', email: 's@example.org', name: 'Staff', is_staff: true } as User;
const recordReads = () => vi.mocked(api.get).mock.calls
  .filter(([url]) => String(url).includes('/v1/patient-records/101/')).length;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  __resetDiseaseCategoryCaches();
  tab.mounts = 0;
  tab.refreshed = null;
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: {} });
    if (url.includes('/v1/patient-records/101/')) {
      return Promise.resolve({ data: { patient_info: { person_id: 101, patient_name: 'Ada Lovelace' }, user: null } });
    }
    return Promise.resolve({ data: [] });
  });
});

async function renderPatient() {
  render(
    <MemoryRouter initialEntries={['/patient/101']}>
      <Routes>
        <Route path="/patient/:personId" element={<PatientDetailRoute user={STAFF} />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText('Ada Lovelace');
}

const openTab = (label: string) => fireEvent.click(
  within(screen.getByRole('navigation', { name: 'Patient info tabs' })).getByRole('button', { name: label }),
);

describe('PatientDetail and the Source Codes tab', () => {
  it('does not load the tab until it is opened', async () => {
    await renderPatient();
    openTab('Labs');

    expect(screen.queryByText('source codes')).not.toBeInTheDocument();
    expect(tab.mounts).toBe(0);
  });

  it('keeps the tab mounted, hidden, once the curator moves to another tab', async () => {
    await renderPatient();
    openTab('Source Codes');
    expect(screen.getByText('source codes')).toBeVisible();

    openTab('Labs');
    expect(screen.getByText('lab fields')).toBeVisible();
    expect(screen.getByText('source codes')).not.toBeVisible();

    openTab('Source Codes');
    expect(screen.getByText('source codes')).toBeVisible();
    expect(tab.mounts).toBe(1);
  });

  it('re-reads the record when a run started on the tab ends while another tab is showing', async () => {
    await renderPatient();
    openTab('Source Codes');
    openTab('Labs');
    const before = recordReads();

    await act(async () => { await tab.refreshed?.(); });

    await waitFor(() => expect(recordReads()).toBe(before + 1));
  });
});
