/**
 * Every field on the treatment tab is read-only, and the tab says why once.
 *
 * This tab is unlike Blood and Labs. Those hold analytes with a LOINC code each,
 * so an edit is one Measurement. Nothing here is a single fact: a line of therapy
 * is an Episode grouping the drug exposures given in it, and every field on the
 * tab is read back out of that grouping by regimen inference.
 *
 * So it offered twenty-six inputs that could not save — selects over regimen
 * lists, date pickers, an outcome dropdown — each returning a 405. These pin that
 * none of them are offered any more, and that the tab explains what to do instead.
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
  'first_line_therapy', 'first_line_start_date', 'first_line_end_date',
  'first_line_intent', 'first_line_discontinuation_reason', 'first_line_outcome',
  'second_line_therapy', 'second_line_start_date', 'second_line_end_date',
  'second_line_intent', 'second_line_discontinuation_reason', 'second_line_outcome',
  'later_therapy', 'later_start_date', 'later_end_date',
  'later_intent', 'later_discontinuation_reason', 'later_outcome',
  'supportive_therapy_start_date', 'supportive_therapy_end_date',
  'supportive_therapies', 'supportive_therapy_intent', 'planned_therapies',
];

const DESCRIPTORS: Record<string, unknown> = {
  ...Object.fromEntries(THERAPY_FIELDS.map((f) => [f, authored()])),
  first_line_therapy: authored({
    authored_via: {
      target: 'episode',
      endpoint: 'POST /api/v1/episodes/',
      steps: [
        'POST /api/v1/drug-exposures/ for each drug given in the line',
        'POST /api/v1/episodes/ with episode_concept=32531 (Treatment Regimen)',
        'POST /api/v1/episode-events/ linking each drug_exposure_id to the episode',
      ],
    },
  }),
  refractory_status: {
    kind: 'alias', writable: false, canonical: 'treatment_refractory_status',
    reason: 'Mirrors treatment_refractory_status; edit that field instead.',
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  mockGet.mockResolvedValue({ data: DESCRIPTORS });
});

function renderTab(formData: Record<string, unknown> = {}) {
  return render(
    <TreatmentTab formData={formData} onChange={vi.fn()} diseaseType="myeloma" />,
  );
}

const THREE_LINES = {
  therapy_lines_count: 3,
  first_line_therapy: 'VRd', second_line_therapy: 'DRd', later_therapy: 'KPd',
  supportive_therapies: 'Zoledronic acid', planned_therapies: 'CAR-T',
  refractory_status: 'Refractory to lenalidomide',
};

describe('TreatmentTab', () => {
  it('offers no editable input at all', async () => {
    // The whole point: not one field on this tab can be written directly.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    const inputs = screen.queryAllByRole('textbox');
    expect(inputs.length).toBeGreaterThan(0);
    for (const input of inputs) expect(input).toBeDisabled();
    // The regimen/intent/outcome selects are gone entirely, not merely disabled.
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  });

  it('shows the authoring steps the server supplies', async () => {
    // Taken from the descriptor, not written out locally: the steps name concept
    // ids and endpoints, and a local copy would go stale against the server.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/To record a line of therapy/i)).toBeInTheDocument();
    expect(
      screen.getByText(/POST \/api\/v1\/episode-events\/ linking each drug_exposure_id/),
    ).toBeInTheDocument();
  });

  it('states the shared reason once, not beside every field', async () => {
    // Twenty-five fields carry the identical reason. Printing it under each box
    // buries it instead of explaining anything.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getAllByText(AUTHORED_REASON)).toHaveLength(1);
  });

  it('still shows the derived values', async () => {
    // Read-only is not the same as hidden — the tab is how a clinician reads the
    // therapy history back.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByDisplayValue('VRd')).toBeInTheDocument();
    expect(screen.getByDisplayValue('DRd')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Refractory to lenalidomide')).toBeInTheDocument();
  });

  it('keeps the per-line sections gated on the line count', async () => {
    renderTab({ therapy_lines_count: 1, first_line_therapy: 'VRd' });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    // The section title and the field label share this text, so count instead of
    // asserting a single match.
    expect(screen.getAllByText('First Line Therapy').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('Second Line Therapy')).toHaveLength(0);
  });

  it('lists every later line, not just the most recent', async () => {
    renderTab({
      therapy_lines_count: 3,
      later_therapies: [
        { therapy: 'KPd', startDate: '2025-01-01', lineNumber: 3 },
        { therapy: 'Selinexor', startDate: '2025-09-01', lineNumber: 4 },
      ],
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText('KPd')).toBeInTheDocument();
    expect(screen.getByText('Selinexor')).toBeInTheDocument();
    expect(screen.getByText('Line 4:')).toBeInTheDocument();
  });

  it('shows the derived component and therapy-type concept ids', async () => {
    renderTab({
      therapy_lines_count: 1,
      first_line_component_ids: [111, 222],
      first_line_therapy_type_ids: [333],
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/Component concept IDs: 111, 222/)).toBeInTheDocument();
    expect(screen.getByText(/Therapy type concept IDs: 333/)).toBeInTheDocument();
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
 * Therapy component concept_ids display (#189/#231).
 *
 * Predates the descriptor conversion and is unaffected by it: these lines are
 * server-derived and were always read-only, so they render the same whether or
 * not the surrounding fields are editable. Kept as-is apart from awaiting the
 * descriptor fetch the tab now makes on mount.
 */
describe('TreatmentTab - component concept ids', () => {
  const baseFormData: Record<string, unknown> = {
    therapy_lines_count: 2,
    first_line_therapy: 'RVD',
    second_line_therapy: 'Kd',
  };

  const renderWith = async (formData: Record<string, unknown>) => {
    renderTab(formData);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
  };

  it('renders component ids for lines that have them', async () => {
    await renderWith({
      ...baseFormData,
      first_line_component_ids: [35900001, 35900002, 1900001],
      second_line_component_ids: [35900003],
    });
    expect(screen.getByText('Component concept IDs: 35900001, 35900002, 1900001')).toBeInTheDocument();
    expect(screen.getByText('Component concept IDs: 35900003')).toBeInTheDocument();
  });

  it('renders nothing for lines without component ids', async () => {
    await renderWith(baseFormData);
    expect(screen.queryByText(/Component concept IDs:/)).not.toBeInTheDocument();
  });

  it('ignores empty component id lists', async () => {
    await renderWith({ ...baseFormData, first_line_component_ids: [] });
    expect(screen.queryByText(/Component concept IDs:/)).not.toBeInTheDocument();
  });

  it('renders therapy-type class ids for lines that have them', async () => {
    await renderWith({
      ...baseFormData,
      first_line_therapy_type_ids: [35807295, 35807403],
      second_line_therapy_type_ids: [35807295],
    });
    expect(screen.getByText('Therapy type concept IDs: 35807295, 35807403')).toBeInTheDocument();
    expect(screen.getByText('Therapy type concept IDs: 35807295')).toBeInTheDocument();
  });

  it('renders nothing for lines without type class ids', async () => {
    await renderWith(baseFormData);
    expect(screen.queryByText(/Therapy type concept IDs:/)).not.toBeInTheDocument();
  });

  it('labels each later line with its own line number (not always 3)', async () => {
    await renderWith({
      ...baseFormData,
      therapy_lines_count: 5,
      later_therapy: 'RegA',
      later_therapies: [
        { lineNumber: 3, therapy: 'RegA', startDate: '2024-01-01', endDate: null },
        { lineNumber: 5, therapy: 'RegC', startDate: '2024-06-01', endDate: null },
      ],
    });
    expect(screen.getByText('Line 3:')).toBeInTheDocument();
    expect(screen.getByText('Line 5:')).toBeInTheDocument();
  });
});

/**
 * The tab is read-only, but it is not a dead end — it offers the one write that
 * moves the fields it displays.
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

  it('does not offer authoring without a person to write against', async () => {
    // Rendered outside a patient context the write has no subject, and a button
    // that cannot work is worse than no button.
    renderTab(THREE_LINES);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.queryByRole('button', { name: /add therapy line/i })).not.toBeInTheDocument();
  });
});
