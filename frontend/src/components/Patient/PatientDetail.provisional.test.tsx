import { MemoryRouter } from 'react-router-dom';
/**
 * Provisional values on the patient page (#2659).
 *
 * Where the site has provisional data on, the record read lays a proposal over a
 * field the approved record leaves empty and lists it in `provisional_fields`.
 * The page marks such a field and must never save a proposal as approved data:
 * an untouched form sends nothing, a touched proposal is sent as the user's
 * answer, and its indicator goes.
 */
import { render as baseRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import PatientDetail from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('react-router-dom', async importOriginal => ({
  ...await importOriginal<typeof import('react-router-dom')>(),
  useParams: () => ({ personId: '261' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ grouped: new Map(), isLoading: false })),
}));

import api from '@/api/axios';

const render = (ui: React.ReactElement) => baseRender(<MemoryRouter>{ui}</MemoryRouter>);

const DESCRIPTORS = {
  date_of_birth: {
    kind: 'direct', writable: true, target: 'patient_record',
    projection_target: 'person', value_kind: 'date',
  },
  hemoglobin_g_dl: {
    kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number',
    projection: {
      omop_table: 'measurement', concept_id: 3000963, code: '718-7',
      type_concept_id: 32856, source_value: '718-7',
    },
  },
};

const PATIENT = {
  person_id: 261,
  patient_name: 'Alishia Tawny Howell',
  date_of_birth: '1970-01-01',
  hemoglobin_g_dl: '11.5',                     // a proposal, not approved data (DRF sends Decimal as a string)
  provisional_fields: { hemoglobin_g_dl: 82, prior_procedures: 72 },
  // A list holding an approved and a proposed item (#2534): the merged list and
  // which of its items are proposals are both read-only.
  prior_procedures: [{ procedure: 'Chest X-ray' }, { procedure: 'PET scan' }],
  provisional_items: { prior_procedures: [{ procedure: 'PET scan' }] },
  id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1, user_edited_fields: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
    if (url.includes('/patient-info/')) {
      return Promise.resolve({
        data: { patient_info: { ...PATIENT }, user: null, patient_name: PATIENT.patient_name },
      });
    }
    return Promise.resolve({ data: [] });
  });
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
});

afterEach(() => vi.useRealTimers());

async function renderAndOpenLabs() {
  render(<PatientDetail />);
  await waitFor(() =>
    expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
  await waitFor(() => expect(screen.getByDisplayValue('11.5')).toBeInTheDocument());
}

function patchBodies() {
  return (api.patch as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1] as Record<string, unknown>);
}

describe('PatientDetail provisional values', () => {
  it('marks a proposed field with its confidence', async () => {
    await renderAndOpenLabs();

    expect(screen.getByTestId('provisional-hemoglobin_g_dl')).toHaveTextContent('Provisional 82%');
  });

  it('sends nothing for an untouched form: a proposal is never saved as approved', async () => {
    render(<PatientDetail />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument(),
    );
    // An edit elsewhere on the page triggers the autosave over the whole record.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('1970-01-01'), { target: { value: '1971-02-03' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBodies().at(-1)).toEqual({ date_of_birth: '1971-02-03' });
    expect(patchBodies().every((b) => !('hemoglobin_g_dl' in b) && !('provisional_fields' in b))).toBe(true);
    // A merged list is never written back, and neither is the record of which items are proposals.
    expect(patchBodies().every((b) => !('prior_procedures' in b) && !('provisional_items' in b))).toBe(true);
  });

  it('sends a touched proposal as the user\'s answer and drops its indicator', async () => {
    await renderAndOpenLabs();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('11.5'), { target: { value: '13' } });

    expect(screen.queryByTestId('provisional-hemoglobin_g_dl')).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBodies().at(-1)).toEqual({ hemoglobin_g_dl: 13 });
  });

  it('lets the user confirm a proposal by landing on the same value', async () => {
    await renderAndOpenLabs();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('11.5'), { target: { value: '12' } });
    fireEvent.change(screen.getByDisplayValue('12'), { target: { value: '11.5' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBodies().at(-1)).toEqual({ hemoglobin_g_dl: 11.5 });
  });

  it('shows no indicator when nothing is proposed', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/')) {
        return Promise.resolve({
          data: { patient_info: { ...PATIENT, provisional_fields: {} }, user: null, patient_name: PATIENT.patient_name },
        });
      }
      return Promise.resolve({ data: [] });
    });
    await renderAndOpenLabs();

    expect(screen.queryByTestId('provisional-hemoglobin_g_dl')).not.toBeInTheDocument();
  });
});
