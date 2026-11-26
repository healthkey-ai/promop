import { MemoryRouter } from 'react-router-dom';
/**
 * "Confirm my record" while proposals are on the page (#2761).
 *
 * A patient who confirms says what the page shows is accurate, so a value still
 * marked Provisional is not offered for that: the banner says how many, and
 * the button returns once none that the patient can edit remain. Editing a marked
 * field confirms it as approved data, which removes its mark.
 */
import { render as baseRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
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

vi.mock('@/api/clinicalFacts', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/clinicalFacts')>(),
  confirmRecord: vi.fn(),
}));

import api from '@/api/axios';
import { confirmRecord } from '@/api/clinicalFacts';

const render = (ui: React.ReactElement) => baseRender(<MemoryRouter>{ui}</MemoryRouter>);

const lab = {
  kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number',
  projection: {
    omop_table: 'measurement', concept_id: 3000963, code: '718-7',
    type_concept_id: 32856, source_value: '718-7',
  },
};
const DESCRIPTORS = {
  date_of_birth: {
    kind: 'direct', writable: true, target: 'patient_record',
    projection_target: 'person', value_kind: 'date',
  },
  hemoglobin_g_dl: lab,
  // A mirror of another field: it has no write path of its own, so it never blocks.
  hemoglobin_level: { kind: 'alias', writable: false, canonical: 'hemoglobin_g_dl' },
  // Shown on the General tab but calculated from height and weight.
  bmi: { kind: 'computed', writable: false, target: 'patient_record', value_kind: 'number' },
};

const BASE = {
  person_id: 261,
  patient_name: 'Alishia Tawny Howell',
  date_of_birth: '1970-01-01',
  id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1, user_edited_fields: [],
};

/** The record the server returns: a fixed one, or a function so a later read can differ. */
function serve(
  record: Record<string, unknown> | (() => Record<string, unknown>),
  descriptors: Record<string, unknown> = DESCRIPTORS,
) {
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) return Promise.resolve({ data: descriptors });
    if (/\/v1\/patient-records\/\d+\//.test(url)) {
      const body = typeof record === 'function' ? record() : record;
      return Promise.resolve({ data: { patient_info: { ...body }, user: null, patient_name: BASE.patient_name } });
    }
    return Promise.resolve({ data: [] });
  });
}

const recordReads = () =>
  (api.get as ReturnType<typeof vi.fn>).mock.calls
    .filter((c) => /\/v1\/patient-records\/\d+\//.test(String(c[0]))).length;

async function renderAsPatient() {
  render(<PatientDetail patientMode />);
  await waitFor(() =>
    expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument(),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
  (confirmRecord as ReturnType<typeof vi.fn>).mockResolvedValue({
    validated: true, validated_by: 'patient', validation_date: '2026-10-12',
  });
});

describe('Confirm my record with proposals on the page', () => {
  it('offers the button when nothing on the page is marked', async () => {
    serve({ ...BASE, provisional_fields: {} });
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
  });

  it('says how many values are marked and offers no button while one the patient can edit is', async () => {
    serve({ ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } });
    await renderAsPatient();

    const banner = await screen.findByTestId('record-confirmation-blocked');
    expect(banner).toHaveTextContent('1 value is marked Provisional');
    expect(banner).toHaveTextContent('enter your own answer in its field');
    expect(banner).toHaveTextContent('pick another and then it again');
    expect(banner).toHaveTextContent('clear it to reject it');
    expect(screen.queryByRole('button', { name: 'Confirm Record' })).not.toBeInTheDocument();
  });

  it('does not count a list, or a field with no write path of its own', async () => {
    serve({
      ...BASE, hemoglobin_g_dl: '11.5', hemoglobin_level: '11.5', bmi: 24,
      prior_procedures: [{ procedure: 'PET scan' }],
      provisional_fields: { hemoglobin_g_dl: 82, hemoglobin_level: 82, bmi: 80, prior_procedures: 72 },
      provisional_items: { prior_procedures: [{ procedure: 'PET scan' }] },
    });
    await renderAsPatient();

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked');
  });

  it('offers the button when only a list or a mirrored field is marked', async () => {
    serve({
      ...BASE, hemoglobin_level: '11.5', bmi: 24, prior_procedures: [{ procedure: 'PET scan' }],
      provisional_fields: { hemoglobin_level: 82, bmi: 80, prior_procedures: 72 },
      provisional_items: { prior_procedures: [{ procedure: 'PET scan' }] },
    });
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
  });

  it('brings the button back once the patient edits the marked value', async () => {
    serve({ ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } });
    await renderAsPatient();
    expect(await screen.findByTestId('record-confirmation-blocked')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
    fireEvent.change(await screen.findByDisplayValue('11.5'), { target: { value: '12' } });
    fireEvent.click(screen.getByRole('button', { name: 'General' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Confirm Record' })).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
  });

  it('shows a confirmed record with newer proposals as not covered, with no Re-confirm', async () => {
    serve({
      ...BASE, validated: true, validation_date: '2026-10-01',
      hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 },
    });
    await renderAsPatient();

    expect(await screen.findByText(/Record confirmed on 2026-10-01/)).toBeInTheDocument();
    expect(screen.getByTestId('record-confirmation-pending'))
      .toHaveTextContent('1 value is marked Provisional and not covered by that confirmation');
    expect(screen.queryByRole('button', { name: 'Re-confirm' })).not.toBeInTheDocument();
  });

  it('keeps Re-confirm for a confirmed record with nothing marked', async () => {
    serve({ ...BASE, validated: true, validation_date: '2026-10-01', provisional_fields: {} });
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Re-confirm' })).toBeInTheDocument();
    expect(screen.queryByTestId('record-confirmation-pending')).not.toBeInTheDocument();
  });

  it("shows the server's reason when it refuses a confirmation the page allowed", async () => {
    serve({ ...BASE, provisional_fields: {} });
    (confirmRecord as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 409, data: { detail: '2 values on your record are still marked Provisional.' } },
    });
    await renderAsPatient();

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Record' }));

    expect(await screen.findByText(/2 values on your record are still marked Provisional/)).toBeInTheDocument();
  });

  it('does not count a marked field that no patient tab shows', async () => {
    // concomitant_medications and the diagnosis date are proposed by the server, but
    // no tab renders them, so the patient could never clear the mark.
    serve({
      ...BASE, concomitant_medications: 'Warfarin', diagnosis_date: '2024-03-01',
      provisional_fields: { concomitant_medications: 85, diagnosis_date: 88, diagnosis_date_precision: 88 },
    });
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
  });

  it('applies the same rule before the write descriptor has loaded', async () => {
    serve({
      ...BASE, hemoglobin_g_dl: '11.5', concomitant_medications: 'Warfarin',
      provisional_fields: { hemoglobin_g_dl: 82, concomitant_medications: 85 },
    }, {});
    await renderAsPatient();

    const banner = await screen.findByTestId('record-confirmation-blocked');
    expect(banner).toHaveTextContent('1 value is marked Provisional');
  });

  it('saves the patient\'s edit and reads the record again, so marks the server cleared go too', async () => {
    let reads = 0;
    serve(() => (reads++ < 1
      ? { ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } }
      : { ...BASE, hemoglobin_g_dl: '12', provisional_fields: {} }));
    await renderAsPatient();
    expect(await screen.findByTestId('record-confirmation-blocked')).toBeInTheDocument();
    const before = recordReads();

    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
    fireEvent.change(await screen.findByDisplayValue('11.5'), { target: { value: '12' } });
    await act(async () => { vi.advanceTimersByTime(2100); });

    await waitFor(() => expect(api.patch).toHaveBeenCalledWith(
      expect.stringContaining('/patient-info/'), { hemoglobin_g_dl: 12 }));
    await waitFor(() => expect(recordReads()).toBeGreaterThan(before));
    vi.useRealTimers();
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
  });

  it('sends an edit that is still waiting for the autosave before it confirms', async () => {
    serve({ ...BASE, provisional_fields: {} });
    await renderAsPatient();

    fireEvent.change(screen.getByDisplayValue('1970-01-01'), { target: { value: '1971-02-03' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Record' }));

    await waitFor(() => expect(confirmRecord).toHaveBeenCalled());
    expect(api.patch).toHaveBeenCalledWith(expect.stringContaining('/patient-info/'), { date_of_birth: '1971-02-03' });
    const patchOrder = (api.patch as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    const confirmOrder = (confirmRecord as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0];
    expect(patchOrder).toBeLessThan(confirmOrder);
  });

  it('reads the record again when the server refuses, so the banner shows the new count', async () => {
    let reads = 0;
    serve(() => (reads++ < 1
      ? { ...BASE, provisional_fields: {} }
      : { ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } }));
    (confirmRecord as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 409, data: { detail: '1 value on your record is still marked Provisional.' } },
    });
    await renderAsPatient();

    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Record' }));

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked');
  });

  it('waits for the read that follows a save before confirming, so a late read cannot undo the confirmation', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let reads = 0;
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (!/\/v1\/patient-records\/\d+\//.test(url)) return Promise.resolve({ data: [] });
      const first = reads++ === 0;
      const body = first
        ? { ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } }
        : { ...BASE, hemoglobin_g_dl: '12', validated: false, provisional_fields: {} };
      const reply = { data: { patient_info: body, user: null, patient_name: BASE.patient_name } };
      return first ? Promise.resolve(reply) : gate.then(() => reply);   // the read after the save is slow
    });
    await renderAsPatient();

    vi.useFakeTimers({ shouldAdvanceTime: true });
    fireEvent.click(screen.getByRole('button', { name: 'Labs' }));
    fireEvent.change(await screen.findByDisplayValue('11.5'), { target: { value: '12' } });
    await act(async () => { vi.advanceTimersByTime(2100); });
    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    vi.useRealTimers();
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Record' }));

    // The slow read has not landed, so the confirmation has not been sent.
    await act(async () => { await Promise.resolve(); });
    expect(confirmRecord).not.toHaveBeenCalled();
    release();

    await waitFor(() => expect(confirmRecord).toHaveBeenCalled());
    expect(await screen.findByText(/Record confirmed on 2026-10-12/)).toBeInTheDocument();
  });

  it('does not confirm over a save that failed, and shows the save error', async () => {
    serve({ ...BASE, provisional_fields: {} });
    (api.patch as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: { status: 400, data: { detail: 'That date is not valid.' } },
    });
    await renderAsPatient();

    fireEvent.change(screen.getByDisplayValue('1970-01-01'), { target: { value: '1971-02-03' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Record' }));

    expect(await screen.findByText(/That date is not valid\./)).toBeInTheDocument();
    expect(confirmRecord).not.toHaveBeenCalled();
  });

  it('still offers the button when the write descriptor cannot be fetched, and the server decides', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) return Promise.reject(new Error('network'));
      if (/\/v1\/patient-records\/\d+\//.test(url)) {
        return Promise.resolve({ data: {
          patient_info: { ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } },
          user: null, patient_name: BASE.patient_name,
        } });
      }
      return Promise.resolve({ data: [] });
    });
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
  });

  it('shows no attestation banner outside patient mode', async () => {
    serve({ ...BASE, hemoglobin_g_dl: '11.5', provisional_fields: { hemoglobin_g_dl: 82 } });
    render(<PatientDetail />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument(),
    );

    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm Record' })).not.toBeInTheDocument();
  });
});

describe('Confirm my record with proposals on the disease tab and the checklists (#2761)', () => {
  const descriptors = {
    ...DESCRIPTORS,
    b_symptom_fever: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'boolean' },
    gelf_criteria_options: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'string' },
  };

  it('counts a proposal in the disease section the record shows', async () => {
    serve({ ...BASE, disease: 'Follicular Lymphoma', b_symptom_fever: true, provisional_fields: { b_symptom_fever: 75 } }, descriptors);
    await renderAsPatient();

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked');
    expect(screen.queryByRole('button', { name: 'Confirm Record' })).not.toBeInTheDocument();
  });

  it('counts the GELF checklist on a lymphoma record', async () => {
    serve({
      ...BASE, disease: 'Follicular Lymphoma', gelf_criteria_options: 'large_mass',
      provisional_fields: { gelf_criteria_options: 80 },
    }, descriptors);
    await renderAsPatient();

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked');
  });

  it('does not count a lymphoma field on a record whose page shows another disease', async () => {
    serve({ ...BASE, disease: 'Multiple Myeloma', b_symptom_fever: true, provisional_fields: { b_symptom_fever: 75 } }, descriptors);
    await renderAsPatient();

    expect(await screen.findByRole('button', { name: 'Confirm Record' })).toBeInTheDocument();
    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();
  });
});

describe('Confirm my record while the write descriptor loads', () => {
  it('counts nothing until the descriptor is known, then only what the server would count', async () => {
    // A calculated field (bmi) is counted by the page only once the descriptor says it has
    // no write path, so a count shown earlier would flash a number that then falls.
    let release: () => void = () => {};
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) {
        return new Promise((resolve) => { release = () => resolve({ data: DESCRIPTORS }); });
      }
      if (/\/v1\/patient-records\/\d+\//.test(url)) {
        return Promise.resolve({ data: {
          patient_info: { ...BASE, bmi: 24, hemoglobin_g_dl: '11.5', provisional_fields: { bmi: 80, hemoglobin_g_dl: 82 } },
          user: null, patient_name: BASE.patient_name,
        } });
      }
      return Promise.resolve({ data: [] });
    });
    await renderAsPatient();

    expect(screen.queryByTestId('record-confirmation-blocked')).not.toBeInTheDocument();

    await act(async () => { release(); });

    expect(await screen.findByTestId('record-confirmation-blocked')).toHaveTextContent('1 value is marked');
  });
});
