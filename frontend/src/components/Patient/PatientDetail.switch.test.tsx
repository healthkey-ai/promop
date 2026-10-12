/**
 * The editor's state belongs to one record (#1668).
 *
 * Every edit is built on pendingDataRef, the synchronous copy of the editor
 * state, and each save sends what differs from the baseline. Two ways that copy
 * went stale:
 *
 * - Across patients: the route kept one PatientDetail mounted when :personId
 *   changed, so B's first edit carried A's values and was PATCHed into B.
 * - On the same patient: a server refresh replaced the editor state and the
 *   baseline but not pendingDataRef, so the next edit sent pre-refresh values
 *   back as edits.
 */
import { useEffect } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MemoryRouter, Routes, Route, useNavigate, type NavigateFunction } from 'react-router-dom';
import PatientDetail, { PatientDetailRoute } from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

// Stands in for the therapy-line dialog: the server re-derives the record and
// hands it back through onRecordRefreshed.
let refreshedRecord: Record<string, unknown> = {};
vi.mock('@/components/PatientInfo/tabs/TreatmentTab', () => ({
  default: ({ onRecordRefreshed }: { onRecordRefreshed?: (info: Record<string, unknown>) => void }) => (
    <button onClick={() => onRecordRefreshed?.(refreshedRecord)}>Author therapy line</button>
  ),
}));

vi.mock('@/components/PatientInfo/tabs/BehaviorTab', () => ({
  default: ({ formData, onChange, onRefresh }: {
    formData: Record<string, unknown>;
    onChange: (field: string, value: unknown) => void;
    onRefresh?: () => void;
  }) => (
    <>
      <span>TNBC: {String(formData.tnbc_status)}</span>
      <button onClick={() => onChange('estrogen_receptor_status', 'Negative')}>Set ER negative</button>
      <button onClick={onRefresh}>Refresh patient</button>
    </>
  ),
}));

vi.mock('@/api/clinicalFacts', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/clinicalFacts')>(),
  confirmRecord: vi.fn(),
}));

import api from '@/api/axios';
import { confirmRecord } from '@/api/clinicalFacts';

const text = { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'string' };
const DESCRIPTORS = {
  phone_number: text, city: text,
  estrogen_receptor_status: text,
  tnbc_status: { kind: 'derived', writable: false, target: 'patient_record', value_kind: 'boolean' },
};

const RECORDS: Record<string, Record<string, unknown>> = {
  '101': { person_id: 101, patient_name: 'Ada Lovelace', city: 'Rome', phone_number: '2025550101', current_line_of_therapy: 1 },
  '102': { person_id: 102, patient_name: 'Grace Hopper', city: 'Paris', phone_number: '2025550102', current_line_of_therapy: 1 },
};

// When set, the descriptor fetch waits on it: the window between a save starting
// and the save reading its baseline.
let descriptorGate: Promise<void> | null = null;

const router: { navigate?: NavigateFunction } = {};
function CaptureNavigate() {
  const navigate = useNavigate();
  useEffect(() => { router.navigate = navigate; }, [navigate]);
  return null;
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  descriptorGate = null;
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes('writable-fields')) {
      return descriptorGate
        ? descriptorGate.then(() => ({ data: DESCRIPTORS }))
        : Promise.resolve({ data: DESCRIPTORS });
    }
    const id = url.match(/\/patient-info\/(\d+)\//)?.[1];
    if (id && RECORDS[id]) {
      const record = { ...RECORDS[id] };
      return Promise.resolve({ data: { patient_info: record, user: null } });
    }
    return Promise.resolve({ data: [] });
  });
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
  (confirmRecord as ReturnType<typeof vi.fn>).mockResolvedValue({
    validated: true, validated_by: 'patient', validation_date: '2026-10-12',
  });
});

afterEach(() => vi.useRealTimers());

async function renderAt(personId: string) {
  const view = render(
    <MemoryRouter initialEntries={[`/patient/${personId}`]}>
      <CaptureNavigate />
      <Routes>
        <Route path="/patient/:personId" element={<PatientDetailRoute />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() =>
    expect(screen.getByDisplayValue(RECORDS[personId].patient_name as string)).toBeInTheDocument(),
  );
  return view;
}

function edit(displayValue: string, next: string) {
  fireEvent.change(screen.getByDisplayValue(displayValue), { target: { value: next } });
}

async function flushAutosave() {
  await act(async () => { vi.advanceTimersByTime(2100); });
}

function lastPatch() {
  const calls = (api.patch as ReturnType<typeof vi.fn>).mock.calls;
  return calls.length ? { url: calls.at(-1)![0], body: calls.at(-1)![1] } : null;
}

describe('PatientDetail — switching patients', () => {
  it("sends only B's edit to B after editing A in the same mounted route", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAt('101');

    edit('2025550101', '2025550199');
    await flushAutosave();
    await waitFor(() => expect(lastPatch()).toEqual({
      url: '/patient-info/101/', body: { phone_number: '2025550199' },
    }));

    // Straight from A to B, as the browser history menu does -- no list in between.
    act(() => { router.navigate!('/patient/102'); });
    await waitFor(() => expect(screen.getByDisplayValue('Grace Hopper')).toBeInTheDocument());
    expect(screen.getByDisplayValue('Paris')).toBeInTheDocument();

    edit('2025550102', '2025550188');
    await flushAutosave();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(lastPatch()).toEqual({
      url: '/patient-info/102/', body: { phone_number: '2025550188' },
    });
  });
});

describe('PatientDetail — a server refresh on the same patient', () => {
  async function openTreatmentAndRefresh() {
    fireEvent.click(screen.getByRole('button', { name: 'Treatment' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Author therapy line' }));
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
  }

  beforeEach(() => {
    // The server moved a derived column the form does not own.
    refreshedRecord = { ...RECORDS['101'], phone_number: '2025550199', current_line_of_therapy: 2 };
  });

  it('does not send pre-refresh values back with the next edit', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAt('101');

    edit('2025550101', '2025550199');
    await flushAutosave();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));

    await openTreatmentAndRefresh();

    edit('Rome', 'Milan');
    await flushAutosave();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(lastPatch()!.body).toEqual({ city: 'Milan' });
  });

  it('keeps an edit still waiting for autosave when a refresh lands', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    refreshedRecord = { ...RECORDS['101'], current_line_of_therapy: 2 };
    await renderAt('101');

    edit('Rome', 'Milan');
    await openTreatmentAndRefresh();

    // Still on screen, not reverted by the refresh.
    expect(screen.getByDisplayValue('Milan')).toBeInTheDocument();

    await flushAutosave();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(lastPatch()!.body).toEqual({ city: 'Milan' });
  });

  it('does not send the pre-refresh snapshot when a refresh lands mid-save', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // The server moved a writable column too.
    refreshedRecord = { ...RECORDS['101'], city: 'Paris', current_line_of_therapy: 2 };
    let release!: () => void;
    await renderAt('101');
    descriptorGate = new Promise((r) => { release = r; });

    edit('2025550101', '2025550199');
    await flushAutosave();  // the save is now waiting on the descriptor
    await openTreatmentAndRefresh();
    await act(async () => { release(); });

    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(lastPatch()!.body).toEqual({ phone_number: '2025550199' });
  });

  it('does not revert a confirmed save when an older refresh arrives later', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    refreshedRecord = { ...RECORDS['101'], current_line_of_therapy: 2 };
    await renderAt('101');

    edit('Rome', 'Milan');
    await flushAutosave();
    await waitFor(() => expect(lastPatch()?.body).toEqual({ city: 'Milan' }));
    await openTreatmentAndRefresh();

    expect(screen.getByDisplayValue('Milan')).toBeInTheDocument();
    edit('2025550101', '2025550199');
    await flushAutosave();
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
    expect(lastPatch()?.body).toEqual({ phone_number: '2025550199' });
  });

  it('uses the server TNBC value after an unsendable derived change', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const original = RECORDS['101'];
    RECORDS['101'] = {
      ...original, estrogen_receptor_status: 'Positive', tnbc_status: true,
    };
    try {
      refreshedRecord = { ...RECORDS['101'], current_line_of_therapy: 2 };
      await renderAt('101');
      fireEvent.click(screen.getByRole('button', { name: 'Behavior' }));
      fireEvent.click(screen.getByRole('button', { name: 'Set ER negative' }));
      expect(screen.getByText('TNBC: false')).toBeInTheDocument();
      await flushAutosave();
      await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
      expect(lastPatch()?.body).toEqual({ estrogen_receptor_status: 'Negative' });
      fireEvent.click(screen.getByRole('button', { name: 'Refresh patient' }));
      await waitFor(() => expect(screen.getByText('TNBC: true')).toBeInTheDocument());
    } finally {
      RECORDS['101'] = original;
    }
  });

  it('rebases an unsaved edit through reloadPatientInfo', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAt('101');
    edit('Rome', 'Milan');
    fireEvent.click(screen.getByRole('button', { name: 'Behavior' }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh patient' }));
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    expect(screen.getByDisplayValue('Milan')).toBeInTheDocument();
    await flushAutosave();
    await waitFor(() => expect(lastPatch()?.body).toEqual({ city: 'Milan' }));
  });

  it('ignores a reload started before a saved edit', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAt('101');
    let finishReload!: (response: unknown) => void;
    const originalGet = (api.get as ReturnType<typeof vi.fn>).getMockImplementation()!;
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) =>
      url === '/patient-info/101/'
        ? new Promise((resolve) => { finishReload = resolve; })
        : originalGet(url),
    );

    edit('Rome', 'Milan');
    fireEvent.click(screen.getByRole('button', { name: 'Behavior' }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh patient' }));
    await flushAutosave();
    await waitFor(() => expect(lastPatch()?.body).toEqual({ city: 'Milan' }));
    await act(async () => {
      finishReload({ data: { patient_info: { ...RECORDS['101'] } } });
    });
    fireEvent.click(screen.getByRole('button', { name: 'General' }));
    expect(screen.getByDisplayValue('Milan')).toBeInTheDocument();
  });
});

describe('PatientDetail — leaving during autosave', () => {
  it('flushes the pending edit for the old patient on unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const view = await renderAt('101');
    edit('Rome', 'Milan');
    view.unmount();
    await waitFor(() => expect(lastPatch()).toEqual({
      url: '/patient-info/101/', body: { city: 'Milan' },
    }));
  });

  it('keeps a pending edit when record confirmation finishes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<MemoryRouter><PatientDetail personIdOverride="101" patientMode /></MemoryRouter>);
    await waitFor(() => expect(screen.getByDisplayValue('Rome')).toBeInTheDocument());
    edit('Rome', 'Milan');
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Record' }));
    await waitFor(() => expect(confirmRecord).toHaveBeenCalled());
    expect(screen.getByDisplayValue('Milan')).toBeInTheDocument();
    await flushAutosave();
    await waitFor(() => expect(lastPatch()?.body).toEqual({ city: 'Milan' }));
  });
});
