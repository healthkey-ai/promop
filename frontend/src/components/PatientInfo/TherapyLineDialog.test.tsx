/**
 * Recording a line of therapy — the one write that moves the treatment tab.
 *
 * Every therapy field on the tab is inferred from an Episode grouping drug
 * exposures, so none can be written directly. These pin what the dialog sends
 * (the clinician's vocabulary, never CDM ids), and that the server's specific
 * refusals survive to the screen rather than becoming "save failed".
 */
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import TherapyLineDialog from './TherapyLineDialog';

const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock('@/api/axios', () => ({
  default: {
    get: (...a: unknown[]) => mockGet(...a),
    post: (...a: unknown[]) => mockPost(...a),
    patch: vi.fn(),
  },
}));

const LENALIDOMIDE = {
  concept_id: 19026972, concept_name: 'lenalidomide', concept_code: '337535',
  vocabulary_id: 'RxNorm', concept_class_id: 'Ingredient', standard_concept: 'S',
};
const DEXAMETHASONE = {
  concept_id: 1518254, concept_name: 'dexamethasone', concept_code: '3264',
  vocabulary_id: 'RxNorm', concept_class_id: 'Ingredient', standard_concept: 'S',
};

let onClose: ReturnType<typeof vi.fn>;
let onAuthored: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  onClose = vi.fn();
  onAuthored = vi.fn();
  mockGet.mockResolvedValue({ data: { results: [LENALIDOMIDE, DEXAMETHASONE] } });
  mockPost.mockResolvedValue({
    data: {
      episode_id: 98, line_number: 1, created: true,
      drug_exposure_ids: [168, 169], drugs_created: 2,
      patient_info: { therapy_lines_count: 1, first_line_therapy: 'Rd' },
    },
  });
});

afterEach(() => vi.useRealTimers());

function open(defaultLineNumber = 1) {
  render(
    <TherapyLineDialog
      personId={262}
      defaultLineNumber={defaultLineNumber}
      onClose={onClose}
      onAuthored={onAuthored}
    />,
  );
}

async function search(text: string) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  fireEvent.change(screen.getByLabelText('Search drugs'), { target: { value: text } });
  await act(async () => { vi.advanceTimersByTime(400); });
  vi.useRealTimers();
}

async function addDrug(name: string, text = 'lena') {
  await search(text);
  // Anchored: once the drug is in the list its "Remove <name>" button matches an
  // unanchored pattern too.
  const result = new RegExp(`^${name}`);
  await waitFor(() => expect(screen.getByRole('button', { name: result })).toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: result }));
}

describe('TherapyLineDialog', () => {
  it('searches RxNorm ingredients only', async () => {
    // A line is the drugs given, not their branded pack sizes.
    open();
    await search('lena');

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    const [url, config] = mockGet.mock.calls.at(-1)!;
    expect(url).toBe('/v1/concepts/search/');
    expect((config as { params: Record<string, unknown> }).params).toMatchObject({
      q: 'lena', vocabulary_id: 'RxNorm', concept_class_id: 'Ingredient',
    });
  });

  it('does not search below the server minimum', async () => {
    // Three characters is a trigram; shorter cannot use the index and the server
    // rejects it outright.
    open();
    await search('le');
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('sends the clinician vocabulary, never CDM ids', async () => {
    open(2);
    await addDrug('lenalidomide');
    fireEvent.change(screen.getByLabelText(/start date/i), { target: { value: '2025-03-01' } });
    fireEvent.click(screen.getByRole('button', { name: /record line/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    const [url, body] = mockPost.mock.calls.at(-1)!;
    expect(url).toBe('/v1/therapy-lines/');
    expect(body).toEqual({
      person: 262,
      line_number: 2,
      start_date: '2025-03-01',
      end_date: null,
      outcome: null,
      drugs: [{ concept_id: 19026972, source_value: 'lenalidomide' }],
    });
    // Nothing about episodes, type concepts or primary keys.
    const keys = Object.keys(body as Record<string, unknown>);
    expect(keys).not.toContain('episode_id');
    expect(keys).not.toContain('episode_concept');
  });

  it('sends the outcome value the server can code, not the display label', async () => {
    // OUTCOME_SNOMED_CODES keys on the bare phrase; the tab's display constant
    // carries the abbreviation. Sending the label stores text but no code.
    open();
    await addDrug('lenalidomide');
    fireEvent.change(screen.getByLabelText(/outcome/i), { target: { value: 'Partial Response' } });
    fireEvent.click(screen.getByRole('button', { name: /record line/i }));

    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    const [, body] = mockPost.mock.calls.at(-1)!;
    expect((body as { outcome: string }).outcome).toBe('Partial Response');
  });

  it('hands the re-derived record back and closes', async () => {
    open();
    await addDrug('lenalidomide');
    fireEvent.click(screen.getByRole('button', { name: /record line/i }));

    await waitFor(() => expect(onAuthored).toHaveBeenCalled());
    expect(onAuthored).toHaveBeenCalledWith({
      therapy_lines_count: 1, first_line_therapy: 'Rd',
    });
    expect(onClose).toHaveBeenCalled();
  });

  it('refuses to send a line with no drugs', async () => {
    // The server refuses it too, but failing here says why without a round trip.
    open();
    fireEvent.click(screen.getByRole('button', { name: /record line/i }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('shows the server refusal rather than a generic failure', async () => {
    // The refusals are specific -- an unloaded concept, an unknown drug -- and
    // each says what to do about it.
    mockPost.mockRejectedValue({
      response: { status: 400, data: { detail: 'Unknown drug concept_id 99999999' } },
    });
    open();
    await addDrug('lenalidomide');
    fireEvent.click(screen.getByRole('button', { name: /record line/i }));

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Unknown drug concept_id 99999999'),
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not list the same ingredient twice', async () => {
    open();
    await addDrug('lenalidomide');
    await addDrug('lenalidomide');

    fireEvent.click(screen.getByRole('button', { name: /record line/i }));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    const [, body] = mockPost.mock.calls.at(-1)!;
    expect((body as { drugs: unknown[] }).drugs).toHaveLength(1);
  });

  it('drops a drug that was added by mistake', async () => {
    open();
    await addDrug('lenalidomide');
    fireEvent.click(screen.getByRole('button', { name: /remove lenalidomide/i }));

    fireEvent.click(screen.getByRole('button', { name: /record line/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(mockPost).not.toHaveBeenCalled();
  });
});
