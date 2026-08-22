/**
 * A save must not echo the record back at the server.
 *
 * Editing a lab value returned:
 *
 *   OMOP-mapped PatientRecord fields are read-only. Write a complete clinical
 *   fact to the appropriate OMOP resource, then rederive the record.
 *   fields: [absolute_neutrophile_count, calcium_mg_dl, egfr, …]
 *
 * every one of them an alias the edit had just moved. The save wrote the OMOP
 * fact first, derivation updated the canonical column *and its aliases*, and the
 * PATCH then sent a payload captured before that write — so the server read stale
 * values as attempted writes to read-only fields and refused the whole request.
 *
 * The rule these pin: send the edit, not the record. Anything the descriptor
 * knows is OMOP-mapped, lifecycle columns go stale on any write, and an unchanged
 * value has nothing to say.
 */
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import PatientDetail from './PatientDetail';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

vi.mock('@/api/axios', () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ personId: '261' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

vi.mock('@/api/clinicalFacts', async () => {
  const actual = await vi.importActual<typeof import('@/api/clinicalFacts')>(
    '@/api/clinicalFacts',
  );
  return { ...actual, writeFieldValue: vi.fn().mockResolvedValue({}) };
});

import api from '@/api/axios';
import { writeFieldValue } from '@/api/clinicalFacts';

const DESCRIPTORS = {
  anc_thousand_per_ul: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 3013650, code: '751-8', value_kind: 'number',
    type_concept_id: 32856, source_value: '751-8',
  },
  // The alias the failure named: derivation moves it when the canonical is
  // written, so a payload captured beforehand carries a stale value.
  absolute_neutrophile_count: {
    kind: 'alias', writable: false, canonical: 'anc_thousand_per_ul',
    reason: 'Mirrors anc_thousand_per_ul; edit that field instead.',
  },
  hemoglobin_g_dl: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 3000963, code: '718-7', value_kind: 'number',
    type_concept_id: 32856, source_value: '718-7',
  },
};

const PATIENT = {
  person_id: 261,
  patient_name: 'Alishia Tawny Howell',
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
  it('routes a clinical edit to the OMOP write path', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(writeFieldValue).toHaveBeenCalled());
    const [, field] = (writeFieldValue as ReturnType<typeof vi.fn>).mock.calls.at(-1)!;
    expect(field).toBe('anc_thousand_per_ul');
  });

  it('never sends an OMOP-mapped field in the PATCH', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(writeFieldValue).toHaveBeenCalled());
    const body = patchBody();
    if (body) {
      for (const mapped of Object.keys(DESCRIPTORS)) {
        expect(body).not.toHaveProperty(mapped);
      }
    }
  });

  it('never sends the alias that derivation just moved', async () => {
    // The specific field the server named when refusing the save.
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(writeFieldValue).toHaveBeenCalled());
    const body = patchBody();
    if (body) expect(body).not.toHaveProperty('absolute_neutrophile_count');
  });

  it('never sends lifecycle columns', async () => {
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(writeFieldValue).toHaveBeenCalled());
    const body = patchBody();
    if (body) {
      for (const f of ['id', 'created_at', 'updated_at', 'derivation_version',
                       'user_edited_fields']) {
        expect(body).not.toHaveProperty(f);
      }
    }
  });

  it('skips the PATCH entirely when only clinical facts moved', async () => {
    // The OMOP writes have already done the work; an empty PATCH is a request
    // that can only fail.
    await renderAndLoad();
    await openBloodTab();
    await editAndSave('3.1', '5.5');

    await waitFor(() => expect(writeFieldValue).toHaveBeenCalled());
    expect(api.patch).not.toHaveBeenCalled();
  });

  it('does not PATCH at all when the descriptor cannot be fetched', async () => {
    // Failing closed has to cover the whole save. An empty descriptor stops the
    // OMOP writes correctly, but it also makes the projection filter match
    // everything -- there is no longer any way to tell an OMOP-mapped column
    // from one this record owns. PATCHing the lot is how the 405 comes back.
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
    expect(writeFieldValue).not.toHaveBeenCalled();
  });

  it('surfaces the rejected field names from a read-only refusal', async () => {
    // `fields` is the whole diagnosis: which column was rejected says whether the
    // edited value was refused or a derived one rode along. It was being dropped,
    // leaving an on-screen error nobody could act on.
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
