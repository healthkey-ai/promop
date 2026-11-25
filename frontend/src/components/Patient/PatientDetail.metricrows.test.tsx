/**
 * A 30-day wearable value's link to its OMOP rows (#2887): on the patient page
 * it switches to the OMOP tab and hands it the group to open. Only a reader who
 * has that tab is offered the link.
 */
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PatientDetailRoute } from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { __resetDiseaseCategoryCaches } from '@/hooks/useDiseaseCategories';
import type { User } from '@/hooks/useAuth';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));
vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));
vi.mock('@/components/PatientInfo/tabs/GeneralTab', () => ({ default: () => <div>general fields</div> }));
vi.mock('./PatientOmopTab', () => ({
  default: ({ focus }: { focus?: unknown }) => <div data-testid="omop-tab">{JSON.stringify(focus ?? null)}</div>,
}));

import api from '@/api/axios';

const STAFF = { id: 1, sub: 's', email: 's@example.org', name: 'Staff', is_staff: true } as User;
const DOCTOR = { id: 2, sub: 'd', email: 'd@example.org', name: 'Doctor' } as User;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  __resetDiseaseCategoryCaches();
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('wearable-metric')) {
      return Promise.resolve({ data: {
        groups: [{ table: 'observations', concept_id: 3, source_value: null, wearable: true, rows: 4 }],
        days: [], window: null, truncated: false,
      } });
    }
    if (url.includes('writable-fields')) return Promise.resolve({ data: {} });
    if (url.includes('/v1/patient-records/101/')) {
      return Promise.resolve({ data: {
        patient_info: { person_id: 101, patient_name: 'Ada Lovelace', median_daily_steps_30d: 5500 },
        user: null,
      } });
    }
    return Promise.resolve({ data: [] });
  });
});

async function renderPatient(user: User) {
  render(
    <MemoryRouter initialEntries={['/patient/101']}>
      <Routes>
        <Route path="/patient/:personId" element={<PatientDetailRoute user={user} />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText('Ada Lovelace');
  fireEvent.click(within(screen.getByRole('navigation', { name: 'Patient info tabs' }))
    .getByRole('button', { name: /wearable/i }));
}

describe('PatientDetail and a 30-day wearable value', () => {
  it('opens the OMOP tab at the group holding the rows', async () => {
    await renderPatient(STAFF);
    fireEvent.click(await screen.findByRole('button', { name: /show the omop rows behind median daily steps/i }));

    const omop = await screen.findByTestId('omop-tab');
    expect(JSON.parse(omop.textContent ?? 'null')).toMatchObject({
      table: 'observations', concept_id: 3, wearable: true,
    });
  });

  it('opens the OMOP tab from the tab bar as it always does, not at the last link\'s group', async () => {
    await renderPatient(STAFF);
    fireEvent.click(await screen.findByRole('button', { name: /show the omop rows behind median daily steps/i }));
    expect(JSON.parse((await screen.findByTestId('omop-tab')).textContent ?? 'null')).not.toBeNull();

    const tabs = within(screen.getByRole('navigation', { name: 'Patient info tabs' }));
    fireEvent.click(tabs.getByRole('button', { name: /wearable/i }));
    fireEvent.click(tabs.getByRole('button', { name: 'OMOP' }));

    expect((await screen.findByTestId('omop-tab')).textContent).toBe('null');
  });

  it('offers the chart and no link to rows to a reader without the OMOP tab', async () => {
    await renderPatient(DOCTOR);
    expect(await screen.findByRole('button', { name: /chart median daily steps/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: /omop rows behind/i })).toBeNull());
  });
});
