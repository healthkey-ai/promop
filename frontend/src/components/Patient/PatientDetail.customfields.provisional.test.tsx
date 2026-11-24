import { MemoryRouter } from 'react-router-dom';
/**
 * Provisional values of fields added through Field Concept Mapping (#2857).
 *
 * An added field has no column: its value is a key of `custom_fields`, and the
 * server marks a proposal for it as `provisional_fields["custom_fields.<name>"]`.
 * The page marks it beside its label, never saves a proposal as approved data (the
 * save sends only the keys the user changed), drops the mark when the user enters
 * a value, and counts a marked field the patient sees against "Confirm my record".
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

const writable = { kind: 'direct', writable: true, target: 'patient_record', custom: true };
const DESCRIPTORS: Record<string, unknown> = {
  date_of_birth: {
    kind: 'direct', writable: true, target: 'patient_record',
    projection_target: 'person', value_kind: 'date',
  },
  frailty_note: writable,
  unplaced_note: writable,
  other_note: writable,
};

const custom = (id: number, field_name: string, tab: string) => ({
  id, field_name, display_name: field_name.replace('_', ' '), tab, field_type: 'text',
  mode: 'editable', unit: '', scope: 'base', diseases: [],
});
const DEFINITIONS = [
  custom(1, 'frailty_note', 'general'),
  custom(2, 'unplaced_note', 'omop'),
  custom(3, 'other_note', 'general'),
];

let record: Record<string, unknown>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  record = {
    person_id: 261,
    patient_name: 'Alishia Tawny Howell',
    date_of_birth: '1970-01-01',
    custom_fields: { frailty_note: 'Stable', other_note: 'Approved text' },
    provisional_fields: { 'custom_fields.frailty_note': 82 },
    id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
    derivation_version: 1, user_edited_fields: [],
  };
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
    if (url.includes('custom-patient-fields')) return Promise.resolve({ data: DEFINITIONS });
    if (/\/v1\/patient-records\/\d+\//.test(url)) {
      return Promise.resolve({ data: { patient_info: { ...record }, user: null, patient_name: 'Alishia Tawny Howell' } });
    }
    return Promise.resolve({ data: [] });
  });
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
});

afterEach(() => vi.useRealTimers());

async function renderPage(patientMode = false) {
  render(<PatientDetail patientMode={patientMode} />);
  await waitFor(() => expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument());
  await screen.findByTestId('custom-patient-fields');
}

const patchBodies = () => (api.patch as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1] as Record<string, unknown>);

describe('PatientDetail with a proposed added field', () => {
  it('marks the added field with its confidence and no other', async () => {
    await renderPage();

    expect(screen.getByTestId('provisional-custom_fields.frailty_note')).toHaveTextContent('Provisional 82%');
    expect(screen.queryByTestId('provisional-custom_fields.other_note')).not.toBeInTheDocument();
  });

  it('sends nothing for an untouched proposal, then only the key the user changed', async () => {
    await renderPage();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('1970-01-01'), { target: { value: '1971-02-03' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    // The proposal shown in custom_fields is not echoed back as an answer.
    expect(patchBodies().at(-1)).toEqual({ date_of_birth: '1971-02-03' });
  });

  it('drops the mark when the user enters a value and sends that key alone', async () => {
    await renderPage();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.change(screen.getByDisplayValue('Stable'), { target: { value: 'Frail' } });

    expect(screen.queryByTestId('provisional-custom_fields.frailty_note')).not.toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBodies().at(-1)).toEqual({ custom_fields: { frailty_note: 'Frail' } });
  });

  it('confirms a proposal by landing on the same value: the key is still sent', async () => {
    await renderPage();
    // The server approves the value, so its next read carries no mark.
    (api.patch as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      record = { ...record, provisional_fields: {} };
      return { data: {} };
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const box = screen.getByDisplayValue('Stable');
    fireEvent.change(box, { target: { value: 'Stable2' } });
    fireEvent.change(box, { target: { value: 'Stable' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBodies().at(-1)).toEqual({ custom_fields: { frailty_note: 'Stable' } });
    expect(screen.queryByTestId('provisional-custom_fields.frailty_note')).not.toBeInTheDocument();
  });
});

describe('Confirm my record with a proposed added field', () => {
  it('counts a marked added field the patient sees', async () => {
    await renderPage(true);

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked Provisional');
  });

  it('counts nothing for a field placed on no tab', async () => {
    record.custom_fields = { unplaced_note: 'Guess' };
    record.provisional_fields = { 'custom_fields.unplaced_note': 82 };
    await renderPage(true);

    await waitFor(() => expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument());
    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
  });
});
