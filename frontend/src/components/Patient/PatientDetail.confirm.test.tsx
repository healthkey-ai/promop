import { MemoryRouter } from 'react-router-dom';
/**
 * A professional confirms a patient's provisional values (#2870).
 *
 * The v1 record read carries `provisional_confirm` only for staff, an org admin or a doctor
 * who may confirm. The page offers a "Confirm <field>" button beside a Provisional badge where
 * the server listed the field, and "Confirm all N provisional values" for the record. It never
 * decides confirmability itself, never sends a value (the server uses its own proposal), and
 * reads the record again after a confirmation.
 */
import { render as baseRender, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
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
  date_of_birth: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'date' },
  hemoglobin_g_dl: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number' },
  sodium_meq_l: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number' },
};

const PROPOSED = {
  person_id: 261,
  patient_name: 'Generated Test Patient',
  date_of_birth: '1970-01-01',
  hemoglobin_g_dl: '11.5',
  sodium_meq_l: '140',
  bmi: 24.2,
  provisional_fields: { hemoglobin_g_dl: 82, sodium_meq_l: 71, bmi: 71 },
  // What the server will confirm; BMI is calculated and clears with its inputs.
  provisional_confirm: { confirmable: ['hemoglobin_g_dl', 'sodium_meq_l'], skipped: { bmi: 'calculated' } },
  id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1, user_edited_fields: [],
};

const CONFIRMED = {
  ...PROPOSED,
  provisional_fields: { sodium_meq_l: 71, bmi: 71 },
  provisional_confirm: { confirmable: ['sodium_meq_l'], skipped: { bmi: 'calculated' } },
};

let recordReads: Record<string, unknown>[];

function serve(...reads: Record<string, unknown>[]) {
  recordReads = reads;
  let served = 0;
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
    if (/\/v1\/patient-records\/\d+\//.test(url)) {
      const info = recordReads[Math.min(served++, recordReads.length - 1)];
      return Promise.resolve({ data: { patient_info: { ...info }, user: null, patient_name: info.patient_name } });
    }
    return Promise.resolve({ data: [] });
  });
}

const recordReadCount = () =>
  (api.get as ReturnType<typeof vi.fn>).mock.calls.filter((c) => /\/v1\/patient-records\/\d+\/$/.test(c[0])).length;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
  (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
    data: { person_id: 261, confirmed: ['hemoglobin_g_dl'], skipped: [], remaining: 2 },
  });
  serve(PROPOSED, CONFIRMED);
});

async function openLabs() {
  render(<PatientDetail />);
  await waitFor(() => expect(screen.getByDisplayValue('Generated Test Patient')).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
  await waitFor(() => expect(screen.getByDisplayValue('11.5')).toBeInTheDocument());
}

describe('a professional confirming one proposal', () => {
  it('offers an explicit Confirm button, named for the field, beside the badge', async () => {
    await openLabs();

    expect(screen.getByTestId('provisional-hemoglobin_g_dl')).toHaveTextContent('Provisional 82%');
    expect(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm Sodium (mEq/L)' })).toBeInTheDocument();
  });

  it('asks the server to confirm that field, sends no value of its own, and reads the record again', async () => {
    await openLabs();
    const reads = recordReadCount();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith(
      '/v1/patient-records/261/confirm-provisional/',
      { fields: ['hemoglobin_g_dl'], expected: { hemoglobin_g_dl: '11.5' } },
    );
    // The page names what it showed; it never sends a value to write.
    expect(Object.keys((api.post as ReturnType<typeof vi.fn>).mock.calls[0][1])).toEqual(['fields', 'expected']);
    await waitFor(() => expect(recordReadCount()).toBeGreaterThan(reads));
    // The record read says the field is approved now: no badge, no button; the other stays.
    await waitFor(() => expect(screen.queryByTestId('provisional-hemoglobin_g_dl')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Confirm Hemoglobin (g/dL)' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm Sodium (mEq/L)' })).toBeInTheDocument();
    expect(api.patch).not.toHaveBeenCalled();                    // a confirmation is not an autosave
  });

  it('offers no button for a field the server did not list: a calculated value', async () => {
    await openLabs();

    expect(screen.getByTestId('provisional-sodium_meq_l')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Confirm BMI/i })).not.toBeInTheDocument();
  });

  it('shows a refusal inline, beside the field, when the proposal changed (409)', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: {
        status: 409,
        data: { confirmed: [], skipped: [{ field: 'hemoglobin_g_dl', reason: 'changed' }], remaining: 3 },
      },
    });
    serve(PROPOSED);                       // the read after the refusal still shows a proposal
    await openLabs();
    const reads = recordReadCount();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    expect(await screen.findByTestId('confirm-error-hemoglobin_g_dl')).toHaveTextContent(/changed since this page was loaded/i);
    await waitFor(() => expect(recordReadCount()).toBeGreaterThan(reads));   // the new proposal is shown
  });

  it('shows a permission refusal inline (403) and does not read the record again', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 403, data: { detail: 'Only staff, an organization admin or a doctor can confirm.' } },
    });
    await openLabs();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    expect(await screen.findByTestId('confirm-error-hemoglobin_g_dl')).toHaveTextContent(/Only staff, an organization admin or a doctor/);
    expect(screen.getByTestId('provisional-hemoglobin_g_dl')).toBeInTheDocument();    // still a proposal
  });

  it('says why a field the server skipped was not confirmed', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { confirmed: [], skipped: [{ field: 'hemoglobin_g_dl', reason: 'invalid', detail: 'Hemoglobin is out of range.' }], remaining: 3 },
    });
    serve(PROPOSED);
    await openLabs();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    expect(await screen.findByTestId('confirm-error-hemoglobin_g_dl')).toHaveTextContent('Hemoglobin is out of range.');
  });
});

describe('a group confirmed together', () => {
  const GROUPED = {
    ...PROPOSED,
    provisional_confirm: {
      confirmable: ['hemoglobin_g_dl', 'sodium_meq_l'], skipped: { bmi: 'calculated' },
      groups: [['hemoglobin_g_dl', 'sodium_meq_l']],
    },
  };

  it('sends what the page shows for every member of the field\'s group, even on a single-field click', async () => {
    serve(GROUPED, CONFIRMED);
    await openLabs();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith(
      '/v1/patient-records/261/confirm-provisional/',
      { fields: ['hemoglobin_g_dl'], expected: { hemoglobin_g_dl: '11.5', sodium_meq_l: '140' } },
    );
  });

  it('shows a skipped group on the field the person pressed (the server skipped the whole group)', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 409, data: { confirmed: [], skipped: [
        { field: 'hemoglobin_g_dl', reason: 'changed' }, { field: 'sodium_meq_l', reason: 'changed' }], remaining: 3 } },
    });
    serve(GROUPED);
    await openLabs();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Hemoglobin (g/dL)' }));

    expect(await screen.findByTestId('confirm-error-hemoglobin_g_dl')).toHaveTextContent(/changed since this page was loaded/i);
  });
});

describe('who is offered it', () => {
  it('offers nothing to a patient on their own page, even if the record read carried the list', async () => {
    render(<PatientDetail patientMode />);
    await waitFor(() => expect(screen.getByDisplayValue('Generated Test Patient')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
    await waitFor(() => expect(screen.getByDisplayValue('11.5')).toBeInTheDocument());

    expect(screen.getByTestId('provisional-hemoglobin_g_dl')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Confirm (Hemoglobin|Sodium)/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Provisional values' })).not.toBeInTheDocument();
  });

  it('offers nothing when the server sent no list: an analyst, a credential, a reader without the right', async () => {
    const { provisional_confirm: _omitted, ...withoutList } = PROPOSED;
    serve(withoutList);
    await openLabs();

    expect(screen.getByTestId('provisional-hemoglobin_g_dl')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Confirm /i })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Provisional values' })).not.toBeInTheDocument();
  });

  it('offers no button where the value is not provisional', async () => {
    serve({ ...PROPOSED, provisional_fields: {}, provisional_confirm: undefined });
    await openLabs();

    expect(screen.queryByTestId('provisional-hemoglobin_g_dl')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Confirm /i })).not.toBeInTheDocument();
  });

  it('offers no button for a field that is listed but no longer marked (an edit made it the user\'s)', async () => {
    serve({ ...PROPOSED, provisional_fields: { sodium_meq_l: 71 } });
    await openLabs();

    expect(screen.queryByRole('button', { name: 'Confirm Hemoglobin (g/dL)' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm Sodium (mEq/L)' })).toBeInTheDocument();
  });
});

describe('Confirm all', () => {
  async function openRecord() {
    render(<PatientDetail />);
    await waitFor(() => expect(screen.getByDisplayValue('Generated Test Patient')).toBeInTheDocument());
  }

  it('states the count and asks once, inline, before doing anything', async () => {
    await openRecord();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm all 2 provisional values' }));

    // The question names what will be confirmed: the labels of the fields, not only a number.
    expect(screen.getByText(/Save these 2 provisional values as approved data for this patient:/)).toBeInTheDocument();
    expect(screen.getByText('Hemoglobin g dl, Sodium meq l')).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Confirm all 2 provisional values' })).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('confirms every listed field in one request, names what it showed, and reports the result', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { person_id: 261, confirmed: ['hemoglobin_g_dl', 'sodium_meq_l'], skipped: [{ field: 'bmi', reason: 'calculated' }], remaining: 0 },
    });
    serve(PROPOSED, { ...PROPOSED, provisional_fields: {}, provisional_confirm: undefined });
    await openRecord();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm all 2 provisional values' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, confirm all 2' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(api.post).toHaveBeenCalledWith(
      '/v1/patient-records/261/confirm-provisional/',
      { all: true, expected: { hemoglobin_g_dl: '11.5', sodium_meq_l: '140' } },
    );
    expect(await screen.findByText(/2 provisional values confirmed as approved data\./)).toBeInTheDocument();
    // The record was read again: nothing is left to confirm.
    await waitFor(() => expect(screen.queryByRole('button', { name: /Confirm all/ })).not.toBeInTheDocument());
  });

  it('reports the ones the server could not confirm and marks them', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        confirmed: ['sodium_meq_l'],
        skipped: [{ field: 'hemoglobin_g_dl', reason: 'changed' }, { field: 'bmi', reason: 'calculated' }],
        remaining: 2,
      },
    });
    await openRecord();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm all 2 provisional values' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, confirm all 2' }));

    expect(await screen.findByText(/1 provisional value confirmed as approved data; 1 could not be confirmed/)).toBeInTheDocument();
  });

  it('shows a 409 as nothing confirmed and a 403 as a refusal', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
      response: { status: 409, data: { confirmed: [], skipped: [{ field: 'hemoglobin_g_dl', reason: 'changed' }], remaining: 3 } },
    });
    serve(PROPOSED);
    await openRecord();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm all 2 provisional values' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, confirm all 2' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/nothing was confirmed/i);

    (api.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce({
      response: { status: 403, data: { detail: 'Access denied.' } },
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm all 2 provisional values' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, confirm all 2' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Access denied.'));
  });
});
