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
import { __resetWritableFieldsCache, fetchWritableFields } from '@/hooks/useWritableFields';
import { useVocabulary } from '@/hooks/useVocabulary';
import { MYELOMA_TYPE_OPTIONS, STEM_CELL_TRANSPLANT_OPTIONS, SCT_ELIGIBILITY_OPTIONS } from '../patientConstants';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/hooks/useVocabulary', () => ({ useVocabulary: vi.fn() }));

/**
 * The tab now asks the server what may be written before rendering a box, so
 * these need a descriptor or every field comes up read-only.
 *
 * The SCT entries mirror what migration 0156 seeds: an approved concept mapping
 * pointing at a dated Observation keyed on `mm-sct-*`, which is how these fields
 * have always been derived. Before that mapping existed the descriptor had
 * nothing to act on, so the controls below could not save at all.
 */
const observation = (source_value: string, value_kind: string, multiple = false) => ({
  kind: 'editable', writable: true, target: 'observation',
  concept_id: 32817, type_concept_id: 32817, source_value, value_kind,
  curated: true, multiple,
});

const DESCRIPTORS: Record<string, unknown> = {
  sct_date: observation('mm-sct-date', 'date'),
  stem_cell_transplant_history: observation('mm-sct-history', 'string', true),
  sct_eligibility: observation('mm-sct-eligibility', 'string', true),
};

vi.mock('@/api/axios', () => ({
  default: {
    get: vi.fn(() => Promise.resolve({ data: (globalThis as Record<string, unknown>).__DESCRIPTORS__ })),
    post: vi.fn(), patch: vi.fn(),
  },
}));

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
  beforeEach(async () => {
    __resetWritableFieldsCache();
    (globalThis as Record<string, unknown>).__DESCRIPTORS__ = DESCRIPTORS;
    // Fill the module cache so useWritableFields has it on first render:
    // these assertions do not await.
    await fetchWritableFields();
    vi.clearAllMocks();
    setupVocabMock();
  });

  // --- Labels ---------------------------------------------------------------

  it('labels the myeloma subtype field as M-Protein Type', () => {
    renderMyeloma();
    expect(screen.getByText('M-Protein Type')).toBeInTheDocument();
    expect(screen.queryByText('Myeloma Type')).not.toBeInTheDocument();
  });

  it('renders all three SCT field labels', () => {
    renderMyeloma();
    expect(screen.getByText('Prior SCT Type')).toBeInTheDocument();
    expect(screen.getByText('SCT Date')).toBeInTheDocument();
    expect(screen.getByText('SCT Eligibility')).toBeInTheDocument();
  });

  it('keeps the myeloma type fallback options aligned with the requested value set', () => {
    expect(MYELOMA_TYPE_OPTIONS).toEqual([
      'IgG kappa',
      'IgG lambda',
      'IgA kappa',
      'IgA lambda',
      'IgD kappa',
      'IgD lambda',
      'IgE kappa',
      'IgE lambda',
      'IgM kappa',
      'IgM lambda',
      'Light-chain kappa',
      'Light-chain lambda',
    ]);
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

  it('renders SCT_ELIGIBILITY_OPTIONS as fallback when vocab is empty', () => {
    setupVocabMock({ sctEligibilityEmpty: true });
    renderMyeloma();
    SCT_ELIGIBILITY_OPTIONS.forEach((opt) => {
      expect(screen.getByTestId(`ms-opt-${opt}`)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// LymphomaSection — FL → DLBCL transformation fields
// ---------------------------------------------------------------------------

vi.mock('../controls/SelectControl', () => ({
  // Stub the Radix-based SelectControl with a native <select> so tests can
  // interact with it in JSDOM (same rationale as the MultiSelectControl stub).
  default: ({
    value,
    options,
    onChange,
  }: {
    value: unknown;
    options: { value: unknown; label: string }[];
    onChange: (v: unknown) => void;
  }) => (
    <select
      data-testid="select-control"
      value={value == null ? '' : String(value)}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">Select…</option>
      {options.map((o) => (
        <option key={String(o.value)} value={String(o.value)}>
          {o.label}
        </option>
      ))}
    </select>
  ),
}));

const TX_OUTCOME_VOCAB = [
  { value: 'Complete Response',   label: 'Complete Response' },
  { value: 'Partial Response',    label: 'Partial Response' },
  { value: 'Progressive Disease', label: 'Progressive Disease' },
  { value: 'Deceased',            label: 'Deceased' },
];

function renderLymphoma(
  formData: Record<string, unknown> = {},
  onChange = vi.fn(),
) {
  (useVocabulary as Mock).mockImplementation((modelName: string) => {
    if (modelName === 'post-transformation-outcome') {
      return { options: TX_OUTCOME_VOCAB, loading: false, source: null };
    }
    return { options: [], loading: false, source: null };
  });
  return render(
    <DiseaseTab {...BASE_PROPS} diseaseType="lymphoma" formData={formData} onChange={onChange} />,
  );
}

describe('LymphomaSection — transformation to DLBCL fields', () => {
  beforeEach(async () => {
    __resetWritableFieldsCache();
    (globalThis as Record<string, unknown>).__DESCRIPTORS__ = DESCRIPTORS;
    // Fill the module cache so useWritableFields has it on first render:
    // these assertions do not await.
    await fetchWritableFields();
    vi.clearAllMocks();
  });

  it('renders all three transformation field labels', () => {
    renderLymphoma();
    expect(screen.getByText('Transformed to DLBCL')).toBeInTheDocument();
    expect(screen.getByText('Transformation Date')).toBeInTheDocument();
    expect(screen.getByText('Post-Transformation Outcome')).toBeInTheDocument();
  });

  it('renders dlbcl_transformation_date value in the date input', () => {
    renderLymphoma({ dlbcl_transformation_date: '2023-04-15' });
    expect(screen.getByDisplayValue('2023-04-15')).toBeInTheDocument();
  });

  it('calls onChange("dlbcl_transformation_date", value) when date changes', () => {
    const onChange = vi.fn();
    const { container } = renderLymphoma({}, onChange);
    const dateInput = container.querySelector('input[type="date"]')!;
    fireEvent.change(dateInput, { target: { value: '2023-04-15' } });
    expect(onChange).toHaveBeenCalledWith('dlbcl_transformation_date', '2023-04-15');
  });

  it('shows Yes when transformed_to_dlbcl is true and calls onChange on change', () => {
    const onChange = vi.fn();
    renderLymphoma({ transformed_to_dlbcl: true }, onChange);
    const booleanSelect = screen
      .getAllByTestId('select-control')
      .find((el) => (el as HTMLSelectElement).value === 'true')!;
    expect(booleanSelect).toBeInTheDocument();
    fireEvent.change(booleanSelect, { target: { value: 'false' } });
    expect(onChange).toHaveBeenCalledWith('transformed_to_dlbcl', false);
  });

  it('renders vocabulary-backed outcome options and calls onChange', () => {
    const onChange = vi.fn();
    renderLymphoma({ post_transformation_outcome: 'Complete Response' }, onChange);
    const outcomeSelect = screen
      .getAllByTestId('select-control')
      .find((el) => (el as HTMLSelectElement).value === 'Complete Response')!;
    expect(outcomeSelect).toBeInTheDocument();
    fireEvent.change(outcomeSelect, { target: { value: 'Progressive Disease' } });
    expect(onChange).toHaveBeenCalledWith('post_transformation_outcome', 'Progressive Disease');
  });
});

/**
 * The tab renders against the writable-field descriptor (plan step 2).
 *
 * Eighty-two fields, of which fifteen are writable biomarkers and staging
 * values. The rest divide into three refusals, and the third is the one worth
 * being careful about: eleven controls here are for fields the API has no column
 * for at all (#646). Calling those "derived" would be wrong in the other
 * direction — they are absent, not computed.
 */
const baseProps = {
  onChange: vi.fn(),
  onMutationAdd: vi.fn(),
  onMutationRemove: vi.fn(),
  onMutationChange: vi.fn(),
};

describe('DiseaseTab — descriptor-driven', () => {
  const measurement = (source_value: string) => ({
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 1, code: source_value, value_kind: 'string',
    type_concept_id: 32856, source_value,
  });

  const CONVERTED: Record<string, unknown> = {
    estrogen_receptor_status: measurement('16112-5'),
    her2_status: measurement('48676-1'),
    ki67_proliferation_index: { ...measurement('85337-4'), value_kind: 'number' },
    tumor_stage: measurement('21905-5'),
    // Real column, no concept assigned yet.
    staging_modalities: {
      kind: 'unmapped', writable: false, group: 'needs-concept-set',
      reason: 'No concept set assigned yet.',
    },
    // Derived from the three receptor statuses.
    tnbc_status: {
      kind: 'computed', writable: false,
      inputs: ['estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status'],
      reason: 'Computed from estrogen_receptor_status, progesterone_receptor_status, her2_status.',
    },
  };

  beforeEach(async () => {
    __resetWritableFieldsCache();
    (globalThis as Record<string, unknown>).__DESCRIPTORS__ = CONVERTED;
    (useVocabulary as Mock).mockReturnValue({ options: [], source: null, loading: false });
    await fetchWritableFields();
  });

  it('leaves a mapped biomarker editable', () => {
    render(<DiseaseTab {...baseProps} diseaseType="breast"
      formData={{ estrogen_receptor_status: 'Positive' }} />);

    expect(screen.queryByTestId('reason-estrogen_receptor_status')).not.toBeInTheDocument();
  });

  it('offers a result date beside a biomarker, which is an event', () => {
    render(<DiseaseTab {...baseProps} diseaseType="breast"
      formData={{ ki67_proliferation_index: 22 }} />);

    expect(screen.getAllByLabelText('Result date').length).toBeGreaterThan(0);
  });

  it('explains a field whose concept is not assigned yet', () => {
    render(<DiseaseTab {...baseProps} diseaseType="breast" formData={{}} />);

    expect(screen.getByTestId('reason-staging_modalities')).toHaveTextContent(
      /no concept set/i,
    );
  });

  it('shows a computed status as text, never as a control', () => {
    // tnbc_status follows from the three receptor statuses, and the tab has
    // always rendered it as a plain Yes/No rather than a field. Converting must
    // not turn it into a box: it has no descriptor entry to write against.
    render(<DiseaseTab {...baseProps} diseaseType="breast"
      formData={{ tnbc_status: true }} />);

    // "Yes" appears in several read-only displays, so anchor on the label.
    expect(screen.getByText(/Triple Negative Status \(Computed\)/)).toBeInTheDocument();
    expect(screen.getByText(/Automatically computed from ER, PR, and HER2/)).toBeInTheDocument();
    expect(screen.queryByTestId('reason-tnbc_status')).not.toBeInTheDocument();
  });

  it('says a field the API has no column for is not stored, not that it is derived', () => {
    // #646. "Derived from OMOP data" would send a reader looking for a value
    // that was never recorded anywhere.
    render(<DiseaseTab {...baseProps} diseaseType="lymphoma" formData={{}} />);

    expect(screen.getByTestId('reason-b_symptoms')).toHaveTextContent(
      /not stored on the patient record yet/i,
    );
  });

  it('offers no editable control for an absent field', () => {
    render(<DiseaseTab {...baseProps} diseaseType="myeloma" formData={{}} />);

    for (const name of ['r_iss_stage', 'hypercalcemia', 'cytogenetic_risk']) {
      expect(screen.getByTestId(`reason-${name}`)).toBeInTheDocument();
    }
  });
});

/**
 * Staging and biomarker fields that no tab showed (plan step 5).
 *
 * All four were mapped and writable, so the write path existed and nothing could
 * reach it. They sit beside whichever disease section is on screen because they
 * are not specific to one: nodal and metastasis status apply to any solid
 * tumour, and PD-L1 drives checkpoint-inhibitor eligibility across several.
 */
describe('DiseaseTab — shared staging and biomarkers', () => {
  const measurement = (source_value: string, value_kind = 'string') => ({
    kind: 'editable', writable: true, target: 'measurement',
    concept_id: 1, code: source_value, value_kind,
    type_concept_id: 32856, source_value,
  });

  const SHARED: Record<string, unknown> = {
    lymph_node_status: measurement('92837-4'),
    metastasis_status: measurement('21907-1'),
    pd_l1_combined_positive_score: measurement('83054-7', 'number'),
    pd_l1_ic_percentage: measurement('83055-4', 'number'),
  };

  beforeEach(async () => {
    __resetWritableFieldsCache();
    (globalThis as Record<string, unknown>).__DESCRIPTORS__ = SHARED;
    (useVocabulary as Mock).mockReturnValue({ options: [], source: null, loading: false });
    await fetchWritableFields();
  });

  it.each(['breast', 'lymphoma', 'myeloma', 'cll', 'other'] as const)(
    'shows them for %s, not only one disease',
    (diseaseType) => {
      render(<DiseaseTab {...baseProps} diseaseType={diseaseType} formData={{}} />);
      expect(screen.getByText('Staging & Biomarkers')).toBeInTheDocument();
    },
  );

  it('leaves all four editable, since all four are mapped', () => {
    render(<DiseaseTab {...baseProps} diseaseType="breast" formData={{}} />);

    for (const name of Object.keys(SHARED)) {
      expect(screen.queryByTestId(`reason-${name}`)).not.toBeInTheDocument();
    }
  });

  it('shows the stored values', () => {
    render(<DiseaseTab {...baseProps} diseaseType="breast"
      formData={{ lymph_node_status: 'N1', pd_l1_combined_positive_score: 12 }} />);

    expect(screen.getByDisplayValue('N1')).toBeInTheDocument();
    expect(screen.getByDisplayValue('12')).toBeInTheDocument();
  });
});

describe('DiseaseTab — field ownership (#960)', () => {
  it('owns Stage and Histologic Type, and leaves Disease to the General tab', () => {
    // This tab knows which staging vocabulary applies — Ann Arbor for lymphoma,
    // ISS for myeloma, generic here — so the editable Stage box belongs to it.
    // `disease` is the discriminator that selects which section renders, so the
    // General tab owns that one and this tab no longer draws a second box.
    render(
      <DiseaseTab
        {...BASE_PROPS}
        diseaseType="other"
        formData={{ disease: 'Other', stage: 'II', histologic_type: 'Ductal' }}
      />,
    );

    expect(screen.getByText('Stage')).toBeInTheDocument();
    expect(screen.getByText('Histologic Type')).toBeInTheDocument();
    expect(screen.queryByText('Disease')).toBeNull();
  });
});
