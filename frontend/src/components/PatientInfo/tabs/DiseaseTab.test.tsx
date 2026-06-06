/**
 * Tests for DiseaseTab — MyelomaSection SCT fields (PR #115)
 *
 * Three new fields added to MyelomaSection:
 *   - stem_cell_transplant_history  (multiselect, label "Prior SCT Type")
 *   - sct_date                      (date input, label "SCT Date")
 *   - sct_eligibility               (multiselect, label "SCT Eligibility")
 *
 * MultiSelectControl is replaced with a simple stub (checkboxes) to avoid
 * Radix UI Popover limitations in JSDOM.  useVocabulary is mocked to control
 * returned vocabulary options.
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import DiseaseTab from './DiseaseTab';
import { useVocabulary } from '@/hooks/useVocabulary';
import { STEM_CELL_TRANSPLANT_OPTIONS } from '../patientConstants';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/hooks/useVocabulary', () => ({ useVocabulary: vi.fn() }));

vi.mock('@/components/UI/VocabularyTooltip', () => ({
  VocabularyTooltip: () => null,
}));

/**
 * Stub out MultiSelectControl with plain checkboxes so we can interact with
 * options directly without opening a Radix UI Popover.
 *
 * Renders:
 *   - data-testid="ms-display"        the current display string
 *   - data-testid="ms-opt-{value}"    one checkbox per option
 */
vi.mock('../controls/MultiSelectControl', () => ({
  default: ({
    options,
    selectedValues,
    display,
    onChange,
  }: {
    options: { value: string; label: string }[];
    selectedValues: string[];
    display: string;
    onChange: (v: unknown[]) => void;
  }) => (
    <div>
      <span data-testid="ms-display">{display}</span>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          data-testid={`ms-opt-${o.value}`}
          aria-pressed={selectedValues.includes(o.value)}
          onClick={() => {
            const next = selectedValues.includes(o.value)
              ? selectedValues.filter((v) => v !== o.value)
              : [...selectedValues, o.value];
            onChange(next);
          }}
        >
          {o.label}
        </button>
      ))}
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const SCT_TYPE_VOCAB = STEM_CELL_TRANSPLANT_OPTIONS.map((v) => ({ value: v, label: v }));
const SCT_ELIGIBILITY_VOCAB = [
  { value: 'eligible for autologous SCT',   label: 'eligible for autologous SCT' },
  { value: 'eligible for allogeneic SCT',   label: 'eligible for allogeneic SCT' },
  { value: 'ineligible for autologous SCT', label: 'ineligible for autologous SCT' },
  { value: 'ineligible for allogeneic SCT', label: 'ineligible for allogeneic SCT' },
];

function setupVocabMock({
  sctTypeEmpty = false,
  sctEligibilityEmpty = false,
} = {}) {
  (useVocabulary as Mock).mockImplementation((modelName: string) => {
    if (modelName === 'stem-cell-transplant') {
      return { options: sctTypeEmpty ? [] : SCT_TYPE_VOCAB, loading: false, source: null };
    }
    if (modelName === 'sct-eligibility') {
      return { options: sctEligibilityEmpty ? [] : SCT_ELIGIBILITY_VOCAB, loading: false, source: null };
    }
    return { options: [], loading: false, source: null };
  });
}

const BASE_PROPS = {
  formData: {} as Record<string, unknown>,
  onChange: vi.fn(),
  onMutationAdd: () => {},
  onMutationRemove: () => {},
  onMutationChange: () => {},
  diseaseType: 'myeloma' as const,
};

function renderMyeloma(
  formData: Record<string, unknown> = {},
  onChange = vi.fn(),
) {
  return render(
    <DiseaseTab {...BASE_PROPS} formData={formData} onChange={onChange} />,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('MyelomaSection — SCT fields', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupVocabMock();
  });

  // --- Labels ---------------------------------------------------------------

  it('renders all three SCT field labels', () => {
    renderMyeloma();
    expect(screen.getByText('Prior SCT Type')).toBeInTheDocument();
    expect(screen.getByText('SCT Date')).toBeInTheDocument();
    expect(screen.getByText('SCT Eligibility')).toBeInTheDocument();
  });

  // --- sct_date (date input) ------------------------------------------------

  it('renders sct_date value in the date input', () => {
    renderMyeloma({ sct_date: '2022-05-10' });
    expect(screen.getByDisplayValue('2022-05-10')).toBeInTheDocument();
  });

  it('renders empty date input when sct_date is not set', () => {
    const { container } = renderMyeloma();
    const dateInput = container.querySelector('input[type="date"]')!;
    expect(dateInput).toBeInTheDocument();
    expect(dateInput).toHaveValue('');
  });

  it('calls onChange("sct_date", value) when date changes', () => {
    const onChange = vi.fn();
    const { container } = renderMyeloma({}, onChange);
    const dateInput = container.querySelector('input[type="date"]')!;
    fireEvent.change(dateInput, { target: { value: '2023-06-01' } });
    expect(onChange).toHaveBeenCalledWith('sct_date', '2023-06-01');
  });

  it('calls onChange("sct_date", null) when date is cleared', () => {
    const onChange = vi.fn();
    const { container } = renderMyeloma({ sct_date: '2022-05-10' }, onChange);
    const dateInput = container.querySelector('input[type="date"]')!;
    fireEvent.change(dateInput, { target: { value: '' } });
    expect(onChange).toHaveBeenCalledWith('sct_date', null);
  });

  // --- stem_cell_transplant_history (Prior SCT Type multiselect) -----------

  it('shows "Select..." when no SCT type is selected', () => {
    renderMyeloma({ stem_cell_transplant_history: [] });
    const [sctTypeDisplay] = screen.getAllByTestId('ms-display');
    expect(sctTypeDisplay).toHaveTextContent('Select...');
  });

  it('shows selected SCT type in the multiselect display', () => {
    renderMyeloma({ stem_cell_transplant_history: ['autologous SCT'] });
    const [sctTypeDisplay] = screen.getAllByTestId('ms-display');
    expect(sctTypeDisplay).toHaveTextContent('autologous SCT');
  });

  it('calls onChange with array when value is array-backed and an option is toggled', () => {
    const onChange = vi.fn();
    renderMyeloma({ stem_cell_transplant_history: [] }, onChange);
    fireEvent.click(screen.getByTestId('ms-opt-autologous SCT'));
    expect(onChange).toHaveBeenCalledWith('stem_cell_transplant_history', ['autologous SCT']);
  });

  it('calls onChange with comma string when value is null (isStringBacked path)', () => {
    const onChange = vi.fn();
    renderMyeloma({ stem_cell_transplant_history: null }, onChange);
    fireEvent.click(screen.getByTestId('ms-opt-autologous SCT'));
    expect(onChange).toHaveBeenCalledWith('stem_cell_transplant_history', 'autologous SCT');
  });

  it('emits array after value transitions from null → array (stale-ref regression)', () => {
    // Before fix: useRef captured null at mount → always emitted string even after API loaded.
    // After fix: isStringBacked is derived per-render, so it updates when value becomes array.
    const onChange = vi.fn();
    const { rerender } = render(
      <DiseaseTab
        {...BASE_PROPS}
        formData={{ stem_cell_transplant_history: null }}
        onChange={onChange}
      />,
    );
    // Simulate API response arriving — value changes from null to array
    rerender(
      <DiseaseTab
        {...BASE_PROPS}
        formData={{ stem_cell_transplant_history: ['autologous SCT'] }}
        onChange={onChange}
      />,
    );
    // Selecting another option should now emit array, not a comma-joined string
    fireEvent.click(screen.getByTestId('ms-opt-allogeneic SCT'));
    expect(onChange).toHaveBeenCalledWith(
      'stem_cell_transplant_history',
      ['autologous SCT', 'allogeneic SCT'],
    );
  });

  it('renders STEM_CELL_TRANSPLANT_OPTIONS as fallback when vocab is empty', () => {
    setupVocabMock({ sctTypeEmpty: true });
    renderMyeloma();
    STEM_CELL_TRANSPLANT_OPTIONS.forEach((opt) => {
      expect(screen.getByTestId(`ms-opt-${opt}`)).toBeInTheDocument();
    });
  });

  // --- sct_eligibility (SCT Eligibility multiselect) -----------------------

  it('shows "Select..." when no SCT eligibility is selected', () => {
    renderMyeloma({ sct_eligibility: [] });
    const [, sctEligibilityDisplay] = screen.getAllByTestId('ms-display');
    expect(sctEligibilityDisplay).toHaveTextContent('Select...');
  });

  it('shows selected SCT eligibility in the multiselect display', () => {
    renderMyeloma({ sct_eligibility: ['eligible for autologous SCT'] });
    const [, sctEligibilityDisplay] = screen.getAllByTestId('ms-display');
    expect(sctEligibilityDisplay).toHaveTextContent('eligible for autologous SCT');
  });

  it('calls onChange with array when an SCT eligibility option is toggled', () => {
    const onChange = vi.fn();
    renderMyeloma({ sct_eligibility: [] }, onChange);
    fireEvent.click(screen.getByTestId('ms-opt-eligible for autologous SCT'));
    expect(onChange).toHaveBeenCalledWith('sct_eligibility', ['eligible for autologous SCT']);
  });

  it('deselects an SCT eligibility option when it is already selected', () => {
    const onChange = vi.fn();
    renderMyeloma(
      { sct_eligibility: ['eligible for autologous SCT', 'eligible for allogeneic SCT'] },
      onChange,
    );
    fireEvent.click(screen.getByTestId('ms-opt-eligible for autologous SCT'));
    expect(onChange).toHaveBeenCalledWith('sct_eligibility', ['eligible for allogeneic SCT']);
  });

  it('renders no options for SCT eligibility when vocab is empty', () => {
    setupVocabMock({ sctEligibilityEmpty: true });
    renderMyeloma();
    expect(
      screen.queryByTestId('ms-opt-eligible for autologous SCT'),
    ).not.toBeInTheDocument();
  });
});
