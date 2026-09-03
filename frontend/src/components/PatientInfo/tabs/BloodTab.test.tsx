/**
 * The Blood tab renders against the writable-field descriptor.
 *
 * Every field on it was already mapped server-side; what it could not do was
 * save, because it PATCHed `PatientRecord` — which owns no writable clinical
 * column. A mapping and a write path are independent halves, and these cover the
 * second: a box is typeable only when the server names the fact behind it, and
 * anything else renders read-only with its reason.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import BloodTab from './BloodTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: {
    get: (...a: unknown[]) => mockGet(...a),
    post: vi.fn(),
    patch: vi.fn(),
  },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

const editable = (code: string) => ({
  kind: 'editable', writable: true, target: 'measurement',
  concept_id: 1, code, value_kind: 'number',
  type_concept_id: 32856, source_value: code,
});

const DESCRIPTORS = {
  hemoglobin_g_dl: editable('718-7'),
  anc_thousand_per_ul: editable('751-8'),
  platelet_count_thousand_per_ul: editable('777-3'),
  psa_ng_ml: editable('2857-1'),
  serum_calcium_mg_dl: editable('17861-6'),
  // Not on this tab any more, but the descriptor still classifies it; kept here
  // so the fixture mirrors what the server actually returns.
  calcium_mg_dl: {
    kind: 'alias', writable: false, canonical: 'serum_calcium_mg_dl',
    reason: 'Mirrors serum_calcium_mg_dl; edit that field instead.',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockResolvedValue({ data: DESCRIPTORS });
});

function renderTab(formData: Record<string, unknown> = {}) {
  return render(<BloodTab formData={formData} onChange={vi.fn()} />);
}

describe('BloodTab', () => {
  it('fetches the descriptor', async () => {
    renderTab();
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(
        '/v1/patient-records/writable-fields/',
        expect.anything(),
      ),
    );
  });

  it('offers a result date beside an editable value', async () => {
    renderTab({ hemoglobin_g_dl: 12.5 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const dates = screen.getAllByLabelText('Result date');
    expect(dates.length).toBeGreaterThan(0);
    expect((dates[0] as HTMLInputElement).value).toBe(
      new Date().toISOString().slice(0, 10),
    );
  });

  it('explains a field the descriptor does not cover', async () => {
    mockGet.mockResolvedValue({ data: {} });
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-hemoglobin_g_dl')).toHaveTextContent(
      /not editable here/i,
    );
  });

  it('fails closed when the descriptor cannot be fetched', async () => {
    // Offering an edit the server will refuse is worse than showing a value that
    // cannot yet change.
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab({ hemoglobin_g_dl: 12.5 });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.queryAllByLabelText('Result date')).toHaveLength(0),
    );
  });

  it('holds the blood counts and nothing the Labs tab already has', async () => {
    // Electrolytes, Cardiac & Other, Coagulation and Tumor Markers rendered the
    // same field keys as sections already on Labs, so a value had two editable
    // boxes on two tabs. The counts are what this tab is for.
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('Blood Counts')).toBeInTheDocument();
    for (const gone of ['Electrolytes', 'Cardiac & Other', 'Coagulation',
                        'Tumor Markers']) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument();
    }
  });
});
