/**
 * The Labs tab renders against the server's writable-field descriptor.
 *
 * PatientRecord owns no writable clinical column, so a box is typeable only when
 * the server can name the OMOP fact behind it. Everything else must render
 * read-only *with a reason* — the difference between a UI that looks unfinished
 * and one that explains itself.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import LabsTab from './LabsTab';
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

const DESCRIPTORS = {
  hemoglobin_g_dl: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 1, code: '718-7', value_kind: 'number', unit: 'g/dL',
    type_concept_id: 32856, source_value: '718-7',
  },
  ast_u_l: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 2, code: '1742-6', value_kind: 'number',
    type_concept_id: 32856, source_value: '1742-6',
  },
  egfr_ml_min_173m2: {
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 3, code: '62238-1', value_kind: 'number',
    type_concept_id: 32856, source_value: '62238-1',
  },
  bone_imaging_result: {
    kind: null, writable: false,
    reason: 'No reviewed concept set for this field yet — it cannot be written.',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockResolvedValue({ data: DESCRIPTORS });
});

function renderTab(formData: Record<string, unknown> = {}) {
  return render(<LabsTab formData={formData} onChange={vi.fn()} />);
}

describe('LabsTab', () => {
  it('fetches the descriptor once', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet).toHaveBeenCalledWith(
        '/v1/patient-records/writable-fields/',
        expect.anything(),
      );
  });

  it('shows canonical field names, not the legacy aliases', async () => {
    renderTab({ egfr_ml_min_173m2: 90 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    // The canonical column owns the LOINC code; the alias could never be written.
    expect(screen.getByDisplayValue('90')).toBeInTheDocument();
    expect(screen.queryByLabelText(/Serum Sodium \(mEq\/L\)/)).not.toBeInTheDocument();
  });

  it('offers a result date beside an editable value', async () => {
    renderTab({ ast_u_l: 30 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const dates = screen.getAllByLabelText('Result date');
    expect(dates.length).toBeGreaterThan(0);
    expect((dates[0] as HTMLInputElement).value).toBe(
      new Date().toISOString().slice(0, 10),
    );
  });

  it('renders a field with no concept set read-only, with the reason', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const reason = screen.getByTestId('reason-bone_imaging_result');
    expect(reason).toHaveTextContent('No reviewed concept set');
  });

  it('gives no result-date input to a non-writable field', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(
      document.querySelector('#bone_imaging_result-date'),
    ).not.toBeInTheDocument();
  });

  it('fails closed when the descriptor cannot be fetched', async () => {
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab({ ast_u_l: 30 });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    // No descriptor means nothing is advertised as editable, so no date inputs.
    await waitFor(() =>
      expect(screen.queryAllByLabelText('Result date')).toHaveLength(0),
    );
  });

  it('explains a field it does not have a descriptor for at all', async () => {
    mockGet.mockResolvedValue({ data: {} });
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByTestId('reason-ast_u_l')).toHaveTextContent(
      /not editable here/i,
    );
  });

  it('shows the canonical calcium column, not its alias', async () => {
    // Moved here with the Electrolytes section: the alias regression is
    // still real, it just lives on the tab that renders calcium now.
    // calcium_mg_dl is populated during derivation and owns no LOINC code, so it
    // can never be edited. Showing it rendered a read-only box pointing at a
    // field the user could not reach.
    renderTab({ serum_calcium_mg_dl: 9.1 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByDisplayValue('9.1')).toBeInTheDocument();
    expect(screen.queryByTestId('reason-calcium_mg_dl')).not.toBeInTheDocument();
  });
});
