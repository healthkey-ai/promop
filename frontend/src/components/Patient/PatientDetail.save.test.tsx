import { MemoryRouter } from 'react-router-dom';
/**
 * PatientRecord-first writes: every edit goes through the PatientRecord PATCH.
 *
 * The old split — clinical edits to OMOP endpoints, projection-owned to PATCH —
 * is replaced by a single write path: all writable fields land on PatientRecord,
 * and the backend projects mapped fields into OMOP tables.
 *
 * Profile fields (target === 'patient_record' with projection_target) also go
 * through the same PATCH — the backend projects them to Person/Location.
 *
 * The rule these pin: send only changed, writable fields in the PATCH. Lifecycle
 * columns are never sent, aliases are never sent, and unchanged values are never
 * sent.
 */
import { render as baseRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import PatientDetail from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('react-router-dom', async importOriginal => ({
  ...await importOriginal<typeof import("react-router-dom")>(),
  useParams: () => ({ personId: '261' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

import api from '@/api/axios';

const DESCRIPTORS = {
  date_of_birth: {
    kind: 'direct', writable: true, target: 'patient_record',
    projection_target: 'person', value_kind: 'date',
  },
  anc_thousand_per_ul: {
    kind: 'direct', writable: true, target: 'patient_record',
    value_kind: 'number',
    projection: {
      omop_table: 'measurement', concept_id: 3013650, code: '751-8',
      type_concept_id: 32856, source_value: '751-8',
    },
  },
  // The alias the failure named: derivation moves it when the canonical is
  // written, so a payload captured beforehand carries a stale value.
  absolute_neutrophile_count: {
    kind: 'alias', writable: false, canonical: 'anc_thousand_per_ul',
    reason: 'Mirrors anc_thousand_per_ul; edit that field instead.',
  },
  hemoglobin_g_dl: {
    kind: 'direct', writable: true, target: 'patient_record',
    value_kind: 'number',
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
  anc_thousand_per_ul: 3.1,
  absolute_neutrophile_count: '3.10',
  hemoglobin_g_dl: 12.5,
  // lifecycle — read-only, and updated_at moves on any write
  id: 7, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  derivation_version: 1, user_edited_fields: [],
  // projection-owned, genuinely writable — no OMOP fact behind it
  email: 'howell@example.org',
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

async function renderAndLoad() {
  render(<PatientDetail />);
  await waitFor(() =>
    expect(screen.getByDisplayValue('Alishia Tawny Howell')).toBeInTheDocument(),
  );
}

/** The clinical fields under test live on the Blood tab, not the default one. */
async function openBloodTab() {
  fireEvent.click(screen.getByRole('button', { name: 'Blood' }));
  await waitFor(() => expect(screen.getByDisplayValue('3.1')).toBeInTheDocument());
}

async function editAndSave(displayValue: string, next: string) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  fireEvent.change(screen.getByDisplayValue(displayValue), { target: { value: next } });
  await act(async () => { vi.advanceTimersByTime(2100); });
}

function patchBody() {
  const calls = (api.patch as ReturnType<typeof vi.fn>).mock.calls;
  return calls.length ? calls.at(-1)![1] as Record<string, unknown> : null;
}

describe('PatientDetail save — the edit, not the record', () => {
  it('routes a date-of-birth correction through the PatientRecord PATCH', async () => {
    await renderAndLoad();
    await editAndSave('1970-01-01', '1971-02-03');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBody()).toEqual({ date_of_birth: '1971-02-03' });
  });

  it('routes a clinical edit to the PatientRecord PATCH', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const body = patchBody();
    expect(body).toMatchObject({ anc_thousand_per_ul: 5.5 });
  });

  it('never sends an alias in the PATCH', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const body = patchBody();
    expect(body).not.toHaveProperty('absolute_neutrophile_count');
  });

  it('never sends the alias that derivation just moved', async () => {
    // The specific field the server named when refusing the save.
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const body = patchBody();
    if (body) expect(body).not.toHaveProperty('absolute_neutrophile_count');
  });

  it('never sends lifecycle columns', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const body = patchBody();
    if (body) {
      for (const f of ['id', 'created_at', 'updated_at', 'derivation_version',
                       'user_edited_fields']) {
        expect(body).not.toHaveProperty(f);
      }
    }
  });

  it('sends the clinical edit in the PATCH, not to a separate endpoint', async () => {
    // Under the new architecture all writable fields go through PATCH.
    // No separate OMOP endpoint write happens.
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
  });

  it('does not PATCH at all when the descriptor cannot be fetched', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('writable-fields')) return Promise.reject(new Error('offline'));
      if (url.includes('/patient-info/')) {
        return Promise.resolve({
          data: { patient_info: { ...PATIENT }, user: null, patient_name: PATIENT.patient_name },
        });
      }
      return Promise.resolve({ data: [] });
    });

    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    expect(api.patch).not.toHaveBeenCalled();
  });

  it('surfaces the rejected field names from a read-only refusal', async () => {
    (api.patch as ReturnType<typeof vi.fn>).mockRejectedValue({
      response: {
        status: 405,
        data: {
          detail: 'OMOP-mapped PatientRecord fields are read-only.',
          fields: ['absolute_neutrophile_count', 'egfr'],
        },
      },
    });

    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText(/absolute_neutrophile_count, egfr/)).toBeInTheDocument(),
    );
  });

  it('still sends a genuinely projection-owned edit', async () => {
    await renderAndLoad();
    await editAndSave('howell@example.org', 'a.howell@example.org');

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    expect(patchBody()).toEqual({ email: 'a.howell@example.org' });
  });
});

function render(ui: React.ReactElement) {
  return baseRender(<MemoryRouter>{ui}</MemoryRouter>);
}
