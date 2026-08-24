/**
 * The Behavior tab, rendered against the writable-field descriptor.
 *
 * Twenty-six of its twenty-seven fields have no write path yet. They are real
 * PatientRecord columns awaiting a concept assignment — not derived, not
 * missing — so they render read-only with that reason rather than as boxes that
 * return 405 on save, which is what they were.
 *
 * The one exception, `insurance_type`, shows the other half: a field that gains
 * an approved mapping becomes editable here with no change to this file.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import BehaviorTab from './BehaviorTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: { get: (...a: unknown[]) => mockGet(...a), post: vi.fn(), patch: vi.fn() },
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

const NEEDS_CONCEPT = {
  kind: 'unmapped', writable: false, group: 'needs-concept-set',
  reason: 'No concept set assigned yet.',
};

const DESCRIPTORS: Record<string, unknown> = {
  insurance_type: {
    kind: 'editable', writable: true, target: 'observation',
    concept_id: 4177416, code: '40766-3', value_kind: 'string',
    type_concept_id: 32817, source_value: '40766-3', curated: true,
  },
  smoking_status: NEEDS_CONCEPT,
  alcohol_use: NEEDS_CONCEPT,
  pack_years: NEEDS_CONCEPT,
  employment_status: NEEDS_CONCEPT,
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockResolvedValue({ data: DESCRIPTORS });
});

function renderTab(formData: Record<string, unknown> = {}) {
  return render(<BehaviorTab formData={formData} onChange={vi.fn()} />);
}

describe('BehaviorTab', () => {
  it('fetches the descriptor', async () => {
    renderTab();
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(
        '/v1/patient-records/writable-fields/',
        expect.anything(),
      ),
    );
  });

  it('leaves the one mapped field editable', async () => {
    renderTab({ insurance_type: 'Medicare' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryByTestId('reason-insurance_type')).not.toBeInTheDocument();
  });

  it('offers a result date only for a measurement, and this one is an observation', async () => {
    // insurance_type writes an Observation, which is dated by the write helper
    // rather than by a picker here — a coverage type is not a result.
    renderTab({ insurance_type: 'Medicare' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryAllByLabelText('Result date')).toHaveLength(0);
  });

  it('explains every field that is awaiting a concept', async () => {
    // These were boxes that looked editable and returned 405 on save.
    renderTab({ smoking_status: 'Former Smoker', pack_years: 20 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    for (const name of ['smoking_status', 'alcohol_use', 'pack_years',
                        'employment_status']) {
      expect(screen.getByTestId(`reason-${name}`)).toHaveTextContent(
        /no concept set/i,
      );
    }
  });

  it('still shows the stored values', async () => {
    // Read-only is not hidden: the tab is how these are read back.
    renderTab({ smoking_status: 'Former Smoker', pack_years: 20 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByDisplayValue('20')).toBeInTheDocument();
  });

  it('fails closed when the descriptor cannot be fetched', async () => {
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab({ insurance_type: 'Medicare' });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByTestId('reason-insurance_type')).toBeInTheDocument(),
    );
  });

  it('keeps every section it had before the conversion', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    for (const title of ['Lifestyle Factors', 'Socioeconomic Factors']) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
  });
});
