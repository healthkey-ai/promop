/**
 * Treatment tab tests.
 *
 * The tab renders therapy lines as a list with intent/outcome/discontinuation,
 * supports authoring via the TherapyLineDialog, and shows treatment history,
 * supportive therapy, and planned therapy sections.
 */
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import TreatmentTab from './TreatmentTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: { get: (...a: unknown[]) => mockGet(...a), post: vi.fn(), patch: vi.fn() },
}));

const AUTHORED_REASON =
  'Derived from the therapy episodes, not from one fact. Author a line as an '
  + 'Episode grouping its drug exposures and this field follows.';

const authored = (extra: Record<string, unknown> = {}) => ({
  kind: 'authored', writable: false, group: 'therapy-inference',
  reason: AUTHORED_REASON, ...extra,
});

const THERAPY_FIELDS = [
  'therapy_lines_count', 'relapse_count',
  'supportive_therapy_start_date', 'supportive_therapy_end_date',
  'supportive_therapies', 'supportive_therapy_intent', 'planned_therapies',
];

const DESCRIPTORS: Record<string, unknown> = {
  ...Object.fromEntries(THERAPY_FIELDS.map((f) => [f, authored()])),
  refractory_status: {
    kind: 'alias', writable: false, canonical: 'treatment_refractory_status',
    reason: 'Mirrors treatment_refractory_status; edit that field instead.',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes('writable-fields') ? DESCRIPTORS : [] }));
});

function renderTab(formData: Record<string, unknown> = {}) {
  return render(
    <TreatmentTab formData={formData} onChange={vi.fn()} diseaseType="myeloma" />,
  );
}

const THREE_LINES = {
  therapy_lines_count: 3,
  supportive_therapies: 'Zoledronic acid', planned_therapies: 'CAR-T',
  refractory_status: 'Refractory to lenalidomide',
};

const STRUCTURED_LINES = [
  {
    line: 1,
    episode_id: 901,
    regimen: 'VRd',
    start_date: '2025-01-01',
    end_date: '2025-03-01',
    outcome: 'Partial Response',
    intent: 'Curative',
    discontinuation_reason: null,
    drugs: [
      {
        concept_id: 19026972,
        concept_name: 'lenalidomide',
        concept_code: '337535',
        vocabulary_id: 'RxNorm',
      },
    ],
  },
  {
    line: 2,
    episode_id: 902,
    regimen: 'DRd',
    start_date: '2025-04-01',
    end_date: null,
    outcome: null,
    intent: 'Palliative',
    discontinuation_reason: 'Progressive Disease',
    drugs: [],
  },
];

describe('TreatmentTab', () => {
  it('renders treatment history fields', async () => {
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('Number of Prior Lines')).toBeInTheDocument();
    expect(screen.getByText('Relapse Count')).toBeInTheDocument();
    expect(screen.getByText('Refractory Status')).toBeInTheDocument();
  });

  it('renders supportive therapy section', async () => {
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('Supportive Therapy')).toBeInTheDocument();
    expect(screen.getByText('Zoledronic acid')).toBeInTheDocument();
  });

  it('renders planned therapies section', async () => {
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getAllByText('Planned Therapies').length).toBeGreaterThan(0);
  });

  it('renders therapy lines with intent and outcome', async () => {
    renderTab({
      ...THREE_LINES,
      person_id: 262,
      lines_of_therapy: STRUCTURED_LINES,
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/Line 1: VRd/)).toBeInTheDocument();
    expect(screen.getByText('Curative')).toBeInTheDocument();
    expect(screen.getByText('Partial Response')).toBeInTheDocument();
    expect(screen.getByText(/Line 2: DRd/)).toBeInTheDocument();
    expect(screen.getByText('Palliative')).toBeInTheDocument();
    expect(screen.getByText(/Reason: Progressive Disease/)).toBeInTheDocument();
  });

  it('renders read-only when the descriptor cannot be fetched', async () => {
    // Failing closed, same as every other converted tab.
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    await waitFor(() => {
      const inputs = screen.queryAllByRole('textbox');
      expect(inputs.length).toBeGreaterThan(0);
      for (const input of inputs) expect(input).toBeDisabled();
    });
  });
});

/**
 * The tab offers the one write that moves the fields it displays.
 */
describe('TreatmentTab - authoring a line', () => {
  it('offers the add-line action', async () => {
    renderTab({ ...THREE_LINES, person_id: 262 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByRole('button', { name: /add therapy line/i })).toBeInTheDocument();
  });

  it('opens the dialog prefilled with the next line number', async () => {
    // Three lines on record means the next one is the fourth.
    renderTab({ ...THREE_LINES, person_id: 262 });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /add therapy line/i }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
    expect(screen.getByLabelText(/line number/i)).toHaveValue(4);
  });

  it('opens an existing line for editing', async () => {
    renderTab({ ...THREE_LINES, person_id: 262, lines_of_therapy: STRUCTURED_LINES });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/Line 1: VRd/i)).toBeInTheDocument();
    // Click the first Edit button
    const editButtons = screen.getAllByRole('button', { name: /edit/i });
    fireEvent.click(editButtons[0]);

    await waitFor(() => expect(screen.getByRole('dialog', { name: /edit a line/i })).toBeInTheDocument());
    expect(screen.getByLabelText(/line number/i)).toHaveValue(1);
    expect(screen.getByText('lenalidomide')).toBeInTheDocument();
  });

  it('does not offer authoring without a person to write against', async () => {
    // Rendered outside a patient context the write has no subject, and a button
    // that cannot work is worse than no button.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryByRole('button', { name: /add therapy line/i })).not.toBeInTheDocument();
  });
});


it('selects planned regimens for the next actual line and preserves the current value', async () => {
  const onChange = vi.fn();
  mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes('writable-fields')
    ? { planned_therapies: { kind: 'direct', target: 'patient_record', writable: true } }
    : [{ code: 'new', title: 'Next regimen' }] }));
  render(<TreatmentTab formData={{ person_id: 262, disease: 'C3242', therapy_lines_count: 1,
    lines_of_therapy: [{ line: 2 }], planned_therapies: 'Previous plan' }} onChange={onChange} diseaseType="myeloma" />);
  const picker = await screen.findByLabelText('Planned Therapies');
  await waitFor(() => expect(picker).toBeEnabled());
  expect(mockGet).toHaveBeenCalledWith('/v1/therapy-regimens/', { params: { disease: 'C3242', round: 'later_line_therapy' } });
  expect(picker).toHaveValue('Previous plan');
  fireEvent.change(picker, { target: { value: 'Next regimen' } });
  expect(onChange).toHaveBeenCalledWith('planned_therapies', 'Next regimen');
});

it('offers editable relapse and refractory fields from the descriptor', async () => {
  mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes('writable-fields') ? {
    relapse_count: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'number' },
    refractory_status: { kind: 'direct', writable: true, target: 'patient_record', value_kind: 'string', options: [{ value: 'Primary Refractory' }] },
  } : [] }));
  renderTab({ relapse_count: 2, refractory_status: 'Primary Refractory' });
  await waitFor(() => expect(screen.getByText('Relapse Count').parentElement?.parentElement?.querySelector('input')).toBeEnabled());
  expect(screen.getByText('Refractory Status').parentElement?.parentElement?.querySelector('[role="combobox"]')).toBeEnabled();
});
