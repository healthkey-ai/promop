import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ConceptToCodeTab from './ConceptToCodeTab';

const get = vi.fn();
const post = vi.fn();
const remove = vi.fn(() => Promise.resolve({ data: {} }));
const patch = vi.fn(() => Promise.resolve({ data: {} }));
const put = vi.fn();
vi.mock('@/api/axios', () => ({ default: {
  get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args),
  delete: (...args: unknown[]) => remove(...args), patch: (...args: unknown[]) => patch(...args),
  put: (...args: unknown[]) => put(...args),
} }));
// The source code systems the Wearables row offers; a test sets its own.
let wearableSystems: { id: string; label: string }[] = [];
vi.mock('./wearableSourceSystems', () => ({ useWearableSourceSystems: () => wearableSystems }));
const concept = {
  concept_id: 123, concept_name: 'Serum albumin', concept_code: 'A1', vocabulary_id: 'LOINC', domain_id: 'Measurement',
  fields: [{ field_name: 'albumin', status: 'approved', omop_table: 'measurement' }],
  sccm_counts: { approved: 10, proposed: 2, rejected: 1 },
};
const source = {
  mapping_id: 7, source_code: 'S1', source_code_description: 'Albumin in blood', source_vocabulary_id: 'SNOMED',
  occurrence_count: 8, status: 'proposed', origin_system: 'HT-One', updated_at: '2026-09-25T12:00:00+00:00',
  destination_concept_id: 456, destination_concept_name: 'Old nonstandard target', destination_standard_concept: null,
};
const page = <T,>(results: T[]) => ({ results, total: results.length, page: 1, page_size: 50, zero_seen: 3 });
const run = (state = 'success') => ({
  run_id: 'run-1', state, error: '', total: 1, done: state === 'success' ? 1 : 0, destinations: 0,
  selection: { limit: 25, include_zero_seen: false },
  activity: [{ concept: { concept_id: 123 }, candidates: [{ ...source, evidence: ['umls'], verdict: 'review' }] }],
});

beforeEach(() => {
  // The tab remembers the chosen domain (#2656): one test's choice is not the next one's.
  localStorage.clear();
  wearableSystems = [];
  get.mockReset(); post.mockReset();
  get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept])
    : url === '/v1/concept-to-code/axis-variant-walk/' ? { active: null, last: null, completed: null, patients_since: 0, walked_at: null } : page([source]) }));
  post.mockResolvedValue({ data: { ...source, destination_concept_id: 123, status: 'approved' } });
});

/** The dialog opens on Suggested Codes (#2010); stored rows are under Mapped Codes. */
async function showExisting(dialog: HTMLElement = screen.getByRole('dialog')) {
  fireEvent.click(within(dialog).getByRole('button', { name: 'Mapped Codes' }));
}

async function selectConcept(canApprove = true) {
  render(<ConceptToCodeTab canWrite canApprove={canApprove} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
  await showExisting();
  await screen.findByText('SNOMED:S1');
}

/** The "> 0 only" checkbox. It narrows what could be mapped, so only Suggested Codes has it (#2686). */
const SEEN_FILTER = 'Only source codes with Seen greater than zero';

/** Opens the concept and stays on Suggested Codes, the view the Seen filter belongs to. */
async function openSuggested(canApprove = true) {
  render(<ConceptToCodeTab canWrite canApprove={canApprove} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
  return screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
}

/** The params of every request for a concept's stored mappings (the Mapped Codes groups). */
const linkedParams = (conceptId = 123) => get.mock.calls
  .filter(([url, config]) => url === `/v1/concept-to-code/${conceptId}/` && config?.params?.mode === 'linked')
  .map(([, config]) => config.params as Record<string, unknown>);

/** Mapped Codes was read, and never narrowed by Seen: the request carries no such parameter. */
function expectNoSeenFilterOnMappedCodes(conceptId = 123) {
  const requests = linkedParams(conceptId);
  expect(requests.length).toBeGreaterThan(0);
  requests.forEach(params => expect(params).not.toHaveProperty('seen_only'));
}

describe('concept-first curation', () => {
  it('links to the mapping accuracy analytics page from the top of the tab (#2383)', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const link = await screen.findByRole('link', { name: 'Mapping accuracy analytics' });
    expect(link).toHaveAttribute('href', '/code-mappings/accuracy/analytics');
  });

  it('opens a dialog immediately from a long concept list and shows source loading errors inside it', async () => {
    const concepts = Array.from({ length: 50 }, (_, index) => ({
      ...concept, concept_id: 123 + index, concept_name: index ? `Concept ${index}` : concept.concept_name,
    }));
    get.mockImplementation((url: string) => url === '/v1/concept-to-code/'
      ? Promise.resolve({ data: page(concepts) }) : Promise.reject(new Error('Unavailable')));
    render(<ConceptToCodeTab canWrite canApprove />);
    const trigger = await screen.findByRole('button', { name: 'Serum albumin' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText('Standard LOINC:A1 · OMOP 123 · Measurement')).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Standard concept coverage' })).not.toBeInTheDocument();
    await showExisting(dialog);
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Could not load source codes.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(screen.getByRole('table', { name: 'Standard concept coverage' })).toBeInTheDocument();
  });

  it('supports keyboard opening, traps focus, and returns focus on Escape', async () => {
    const user = userEvent.setup();
    render(<ConceptToCodeTab canWrite canApprove />);
    const trigger = await screen.findByRole('button', { name: 'Serum albumin' });
    trigger.focus();
    await user.keyboard('{Enter}');
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
    const close = within(dialog).getByRole('button', { name: 'Close' });
    expect(close).toHaveFocus();
    await showExisting(dialog);
    await within(dialog).findByText('SNOMED:S1');
    close.focus();
    await user.tab({ shift: true });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it('opens a different concept with fresh source selection and filters after closing', async () => {
    const second = { ...concept, concept_id: 456, concept_name: 'Total protein' };
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept, second]) : page([{ ...source, source_code: url.includes('/456/') ? 'S2' : 'S1' }]) }));
    // Change a filter in each view, and select a row: the Seen filter is
    // Suggested Codes' own (#2686), the note filter and the selection are Mapped Codes'.
    const first = await openSuggested();
    fireEvent.click(within(first).getByRole('checkbox', { name: SEEN_FILTER }));
    expect(within(first).getByRole('checkbox', { name: SEEN_FILTER })).not.toBeChecked();
    await showExisting(first);
    await within(first).findByText('SNOMED:S1');
    fireEvent.click(within(first).getByRole('checkbox', { name: 'Only source codes with a curator note' }));
    await waitFor(() => expect(linkedParams(123).at(-1)).toMatchObject({ with_notes: '1' }));
    fireEvent.click(await within(first).findByRole('checkbox', { name: 'Select SNOMED S1' }));
    expect(within(first).getByRole('button', { name: 'Approve selected (1)' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Total protein' }));
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Total protein' });
    expect(within(dialog).getByRole('checkbox', { name: SEEN_FILTER })).toBeChecked();
    await showExisting(dialog);
    await within(dialog).findByText('SNOMED:S2');
    expect(within(dialog).queryByText('SNOMED:S1')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: 'Only source codes with a curator note' })).not.toBeChecked();
    expect(within(dialog).getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    expect(linkedParams(456)).toEqual([{ mode: 'linked', status: 'proposed', search: '', page: 1 }]);
  });

  it('prevents dismissal during a batch save and allows closing when the whole batch completes', async () => {
    const onWritingChange = vi.fn();
    let finishFirst!: (value: unknown) => void;
    post.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }));
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept]) : page([source, { ...source, mapping_id: 8, source_code: 'S2' }]) }));
    render(<ConceptToCodeTab canWrite canApprove onWritingChange={onWritingChange} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    await screen.findByText('SNOMED:S1');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select displayed proposed sources' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (2)' }));
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close).toBeDisabled();
    expect(onWritingChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(close);
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.pointerDown(document.body, { button: 0, pointerType: 'mouse' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => { finishFirst({ data: { ...source, status: 'approved' } }); });
    await screen.findByText('Approved 2 of 2 selected mappings.');
    expect(post).toHaveBeenCalledTimes(2);
    expect(onWritingChange).toHaveBeenLastCalledWith(false);
    expect(close).toBeEnabled();
    fireEvent.click(close);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('shows distinct concept coverage and searches by scope and domain', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const table = await screen.findByRole('table', { name: 'Standard concept coverage' });
    await screen.findByRole('button', { name: 'Serum albumin' });
    expect(within(table).getByText('10')).toBeInTheDocument();
    expect(within(table).getByText('albumin')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Concept scope'), { target: { value: 'all' } });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'drug_exposure' } });
    fireEvent.change(screen.getByLabelText('Search standard concepts'), { target: { value: 'codeine' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({ params: {
      domain: 'drug_exposure', scope: 'all', search: 'codeine', page: 1, page_size: 50, view: 'categories',
    } })));
  });

  it('defaults Suggested Codes to Seen > 0 and allows zero-Seen codes; Mapped Codes is never narrowed by Seen (#2686)', async () => {
    const unseen = { ...source, mapping_id: null, status: 'unmapped', source_code: 'Z0', occurrence_count: 0 };
    post.mockResolvedValueOnce({ data: { ...run(), activity: [{ concept: { concept_id: 123 },
      candidates: [{ ...source, evidence: ['umls'], verdict: 'review' }, unseen] }] } });
    const estimated = () => get.mock.calls.filter(([url]) => String(url).endsWith('/estimate/'))
      .map(([, config]) => config.params.include_zero_seen);
    await openSuggested();
    const checkbox = () => screen.getByRole('checkbox', { name: SEEN_FILTER });
    expect(checkbox()).toBeChecked();
    expect(checkbox().closest('th')).toHaveAttribute('aria-sort', 'descending');
    await waitFor(() => expect(estimated()).toEqual(['0']));
    // The search asks for seen codes only, and the list shows only those.
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('SNOMED:S1');
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', expect.objectContaining({ include_zero_seen: false }));
    expect(screen.queryByText('SNOMED:Z0')).not.toBeInTheDocument();
    // Unchecked, the zero-Seen candidate shows, the estimate counts such codes,
    // and the run that was made without them is flagged.
    fireEvent.click(checkbox());
    expect(checkbox()).not.toBeChecked();
    expect(screen.getByText('SNOMED:Z0')).toBeInTheDocument();
    await waitFor(() => expect(estimated().at(-1)).toBe('1'));
    expect(screen.getByText('Run Suggest source codes again to search with the changed Seen filter.')).toBeInTheDocument();
    // None of it reaches the stored mappings.
    expect(linkedParams()).toEqual([]);
    await showExisting();
    await screen.findByText('SNOMED:S1');
    expectNoSeenFilterOnMappedCodes();
  });

  it('filters source codes to those with a curator note, off by default, and marks them (#2423)', async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => Promise.resolve({ data:
      url === '/v1/concept-to-code/' ? page([concept])
        : config?.params?.with_notes === '1' ? page([{ ...source, curator_note: 'Ask the lab about the specimen.' }])
          : page([{ ...source, curator_note: 'Ask the lab about the specimen.' }, { ...source, mapping_id: 78, source_code: 'S2', curator_note: '' }]) }));
    await selectConcept();
    const checkbox = screen.getByRole('checkbox', { name: 'Only source codes with a curator note' });
    expect(checkbox).not.toBeChecked();
    expect(checkbox.closest('th')).toHaveTextContent('Description');
    expect(get).not.toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({ params: expect.objectContaining({ with_notes: '1' }) }));
    // Only the row whose note a curator wrote carries the sticky note.
    const marks = screen.getAllByTestId('curator-note');
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveAttribute('title', 'Ask the lab about the specimen.');
    expect(marks[0].closest('tr')).toHaveTextContent('SNOMED:S1');
    fireEvent.click(checkbox);
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({ params: expect.objectContaining({ with_notes: '1', page: 1 }) })));
    await waitFor(() => expect(screen.queryByText('SNOMED:S2')).not.toBeInTheDocument());
  });

  it('approves only reviewed selections, with revision guards, and has no Find view (#2010)', async () => {
    await selectConcept();
    expect(screen.queryByRole('button', { name: 'Find source codes' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Propose selected/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/123/mappings/7/', {
      status: 'approved', expected_updated_at: source.updated_at,
    });
  });

  it('hides batch approval for non-admin curators, who edit one code at a time', async () => {
    await selectConcept(false);
    expect(screen.queryByRole('button', { name: /Approve selected/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Propose selected/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'SNOMED:S1' })).toBeEnabled();
  });

  it('groups existing mappings by status, approved first and folded (#2010)', async () => {
    const rows = [
      { ...source, mapping_id: 1, source_code: 'OK1', status: 'approved' }, { ...source, mapping_id: 2, source_code: 'OK2', status: 'approved' },
      { ...source, mapping_id: 3, source_code: 'MAYBE', status: 'proposed' }, { ...source, mapping_id: 4, source_code: 'NO', status: 'rejected' },
    ];
    get.mockImplementation((url: string, config?: { params?: { status?: string } }) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept]) : { ...page(rows.filter(row => row.status === config?.params?.status)), status_counts: { approved: 2, proposed: 1, rejected: 1 } } }));
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    await screen.findByText('SNOMED:MAYBE');
    // Only the proposed group was fetched; the others announce their counts.
    const sourceGets = () => get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/123/').map(([, config]) => config.params.status);
    expect(sourceGets()).toEqual(['proposed']);
    const approved = screen.getByRole('button', { name: 'Approved mappings (2)' });
    const proposed = screen.getByRole('button', { name: 'Proposed mappings (1)' });
    const rejected = screen.getByRole('button', { name: 'Rejected mappings (1)' });
    expect(approved).toHaveAttribute('aria-expanded', 'false');
    expect(proposed).toHaveAttribute('aria-expanded', 'true');
    expect(rejected).toHaveAttribute('aria-expanded', 'false');
    expect(approved.compareDocumentPosition(proposed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(proposed.compareDocumentPosition(rejected) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('SNOMED:OK1')).not.toBeInTheDocument();
    // Zero-Seen codes are counted and listed, not excluded (#2686).
    expect(screen.getByText('4 matching sources · 3 with Seen 0')).toBeInTheDocument();
    expectNoSeenFilterOnMappedCodes();

    fireEvent.click(approved);
    await screen.findByText('SNOMED:OK1');
    expect(screen.getByText('SNOMED:OK2')).toBeInTheDocument();
    expect(approved).toHaveAttribute('aria-expanded', 'true');
    expect(sourceGets()).toEqual(['proposed', 'approved']);
    // Approved rows are done: they cannot join a batch.
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED OK1' })).toBeDisabled();
    fireEvent.click(approved);
    expect(screen.queryByText('SNOMED:OK1')).not.toBeInTheDocument();
  });

  it('forgets a folded group\'s failure and stale counts; a write refreshes the open groups', async () => {
    let approvedFails = true;
    let counts = { approved: 2, proposed: 1, rejected: 0 };
    get.mockImplementation((url: string, config?: { params?: { status?: string } }) => {
      if (url === '/v1/concept-to-code/') return Promise.resolve({ data: page([concept]) });
      if (config?.params?.status === 'approved' && approvedFails) return Promise.reject(new Error('Unavailable'));
      return Promise.resolve({ data: { ...page(config?.params?.status === 'proposed' ? [source] : []), status_counts: counts } });
    });
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    await screen.findByText('SNOMED:S1');
    fireEvent.click(screen.getByRole('button', { name: 'Approved mappings (2)' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load source codes.');
    // Folding the group that failed clears the failure; the proposed group loaded fine.
    fireEvent.click(screen.getByRole('button', { name: 'Approved mappings (2)' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(/Loading source codes/)).not.toBeInTheDocument();

    // Approving the proposed row reloads the open group, whose counts then win.
    approvedFails = false;
    counts = { approved: 3, proposed: 0, rejected: 0 };
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(await screen.findByRole('button', { name: 'Approved mappings (3)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Proposed mappings (0)' })).toBeInTheDocument();
  });

  it('refreshes the concept when a candidate was proposed but its approval failed', async () => {
    const fresh = { ...source, mapping_id: null, status: 'unmapped', occurrence_count: 0, source_code: 'NEW1' };
    post.mockResolvedValueOnce({ data: { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [fresh] }] } });
    await openSuggested();
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('Not yet mapped');
    const listLoads = () => get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/').length;
    const before = listLoads();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED NEW1' }));
    post.mockResolvedValueOnce({ data: { ...fresh, mapping_id: 21, status: 'proposed', updated_at: 'r2' } })
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: 'Locked by another curator.' } } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 0 of 1 selected mappings.');
    expect(screen.getByRole('alert')).toHaveTextContent('Locked by another curator.');
    // The row is now a stored proposal, so the coverage counts reload.
    expect(screen.getByText('proposed')).toBeInTheDocument();
    await waitFor(() => expect(listLoads()).toBeGreaterThan(before));
  });

  it('reports partial batch failures without claiming every row was approved', async () => {
    const second = { ...source, mapping_id: 8, source_code: 'S2' };
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept]) : page([source, second]) }));
    post.mockResolvedValueOnce({ data: { ...source, status: 'approved' } }).mockRejectedValueOnce({ response: { status: 409, data: { detail: 'Source changed after preview.' } } });
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select displayed proposed sources' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (2)' }));
    await screen.findByText('Approved 1 of 2 selected mappings.');
    expect(screen.getByRole('alert')).toHaveTextContent('S2: Source changed after preview.');
  });

  it('does not select approved or reference mappings in a batch', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept]) : page([
      source, { ...source, mapping_id: 8, source_code: 'APPROVED', status: 'approved' },
      { ...source, mapping_id: 9, source_code: 'REFERENCE', origin_system: 'athena' },
    ]) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select displayed proposed sources' }));
    expect(screen.getByRole('button', { name: 'Approve selected (1)' })).toBeEnabled();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED APPROVED' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED REFERENCE' })).toBeDisabled();
  });

  it('retrieves a preview and requires explicit selection before writing mappings', async () => {
    post.mockResolvedValue({ data: run() });
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('1 candidates retrieved (limit 25).');
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', {
      concept_ids: [123], strategies: ['umls', 'lexical'], limit: 25, include_zero_seen: false, ranking_model: 'jev', include_declined: false, exclude_keys: [], auto_propose_threshold: 0.8,
    });
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
  });

  it('keeps the Suggest button on the same line as the ranker and the limit, and sends Retry declined (#2034)', async () => {
    post.mockResolvedValue({ data: run() });
    await selectConcept();
    const settings = screen.getByRole('group', { name: 'Suggest settings' });
    const ranker = within(settings).getByLabelText('Source suggestion ranker');
    const limit = within(settings).getByLabelText('Maximum source candidates');
    const suggest = within(settings).getByRole('button', { name: 'Suggest source codes' });
    // One non-wrapping row holds all three.
    expect(ranker.parentElement).toBe(limit.parentElement);
    expect(suggest.parentElement).toBe(ranker.parentElement);
    expect(ranker.parentElement).toHaveClass('flex-nowrap');
    fireEvent.click(within(settings).getByRole('checkbox', { name: 'Retry declined' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/estimate/', expect.objectContaining({
      params: expect.objectContaining({ include_declined: '1' }) })));
    fireEvent.click(suggest);
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', expect.objectContaining({ include_declined: true })));
  });

  it('says hidden candidates were declined and that the next run moves on, and how many the estimate skips (#2034)', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('/estimate/')
      ? { total: 29, lexical: 29, declined_skipped: 25, timed_out: false, approximate: null }
      : url === '/v1/concept-to-code/' ? page([concept]) : page([source]) }));
    post.mockResolvedValue({ data: { ...run(), selection: { limit: 25, include_zero_seen: false, ranking_model: 'jev' },
      activity: [{ concept: { concept_id: 123 }, candidates: [{ ...source, evidence: ['lexical'], verdict: 'review', confidence: null, declined: true }] }] } });
    await selectConcept();
    expect(await screen.findByTestId('source-estimate')).toHaveTextContent(
      'About 29 candidate source codes (29 by name) · about 2 runs of 25. 25 a ranker already declined are skipped.');
    const estimates = () => get.mock.calls.filter(([url]) => String(url).includes('/estimate/')).length;
    const before = estimates();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    expect(await screen.findByText(/1 candidates retrieved \(limit 25\)\. 1 declined by the ranker and hidden; the next run moves on to other codes\./)).toBeInTheDocument();
    expect(screen.queryByText('SNOMED:S1')).not.toBeInTheDocument();
    // The finished run recorded declines, so the estimate is read again.
    await waitFor(() => expect(estimates()).toBeGreaterThan(before));
  });

  it('tells an unavailable ranker apart from a decline, and does not promise to move on past retried codes (#2034)', async () => {
    post.mockResolvedValue({ data: { ...run(), selection: { limit: 25, include_zero_seen: false, ranking_model: 'jev', include_declined: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [
        { ...source, evidence: ['lexical'], verdict: 'review', confidence: null, declined: true },
        { ...source, source_code: 'S2', mapping_id: 8, evidence: ['lexical'], verdict: 'review', confidence: null, declined: false, note: 'Ranking model unavailable' },
      ] }] } });
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    const status = await screen.findByText(/2 candidates retrieved/);
    expect(status).toHaveTextContent('1 declined by the ranker and hidden.');
    expect(status).not.toHaveTextContent('moves on');
    expect(status).toHaveTextContent('1 could not be ranked (the ranker was unavailable) and is hidden.');
  });

  it('polls queued jobs and retains failure information', async () => {
    post.mockResolvedValue({ data: run('queued') });
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('suggest-runs')
      ? { ...run('failure'), error: 'Search failed; retry.' }
      : url === '/v1/concept-to-code/' ? page([concept]) : page([source]) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText(/Search failed; retry./, {}, { timeout: 3000 });
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/suggest-runs/run-1/');
    expect(screen.getByRole('button', { name: 'Suggest source codes' })).toBeEnabled();
  });

  it('approves a new vocabulary candidate in one click: proposed from its preview, then approved (#2010)', async () => {
    const fresh = { ...source, mapping_id: null, status: 'unmapped', occurrence_count: 0,
      source_code: 'NEW1', origin_system: 'vocabulary', destination_concept_id: null, destination_concept_name: '' };
    const second = { ...fresh, source_code: 'NEW2' };
    const preview = { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [fresh, second] }] };
    post.mockResolvedValueOnce({ data: preview });
    await openSuggested();
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('2 candidates retrieved (limit 25).');
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', expect.objectContaining({ include_zero_seen: true }));
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED NEW1' }));
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED NEW2' })).not.toBeChecked();
    const proposed = { ...fresh, mapping_id: 17, status: 'proposed', updated_at: 'new-revision', destination_concept_id: 123 };
    post.mockResolvedValueOnce({ data: proposed }).mockResolvedValueOnce({ data: { ...proposed, status: 'approved' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(post).toHaveBeenNthCalledWith(2, '/v1/concept-to-code/123/mappings/', {
      run_id: 'run-1', source_vocabulary_id: 'SNOMED', source_code: 'NEW1', status: 'proposed',
    });
    // The approval carries the revision the proposal returned, not the preview's, and
    // names the preview so the server can store the score that preview gave (#2668).
    expect(post).toHaveBeenNthCalledWith(3, '/v1/concept-to-code/123/mappings/17/', {
      status: 'approved', expected_updated_at: 'new-revision', run_id: 'run-1',
    });
    expect(post).toHaveBeenCalledTimes(3);
    expect(screen.getByText('approved')).toBeInTheDocument();
    expect(screen.getAllByText('Not yet mapped')).toHaveLength(1);
  });

  it('retains a new vocabulary candidate when its preview is stale', async () => {
    post.mockResolvedValueOnce({ data: { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [{ ...source, mapping_id: null, status: 'unmapped', occurrence_count: 0 }] }] } });
    await openSuggested();
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('Not yet mapped');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    post.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'The source vocabulary changed after the preview.' } } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 0 of 1 selected mappings.');
    expect(screen.getByRole('alert')).toHaveTextContent('The source vocabulary changed after the preview.');
    expect(screen.getByText('Not yet mapped')).toBeInTheDocument();
    // The proposal failed, so no approval was attempted.
    expect(post.mock.calls.filter(([url]) => String(url).includes('/mappings/'))).toHaveLength(1);
  });
});

describe('the Seen filter belongs to Suggested Codes; Mapped Codes lists every mapped code (#2686)', () => {
  // One metric, mapped from five platforms' own codes. None has been sighted
  // on an instance that holds no wearable data, so every Seen count is 0.
  const steps = { ...concept, concept_id: 900, concept_name: 'Steps', concept_code: '41950-7', fields: [],
    sccm_counts: { approved: 2, proposed: 3, rejected: 0 } };
  const seeded = ([
    ['Apple', 'HKQuantityTypeIdentifierStepCount', 'approved'], ['Garmin', 'steps', 'approved'],
    ['HealthConnect', 'StepsRecord', 'proposed'], ['OpenWearables', 'steps', 'proposed'], ['HL7-PHR', 'stepCount', 'proposed'],
  ] as const).map(([source_vocabulary_id, source_code, status], index) => ({
    ...source, mapping_id: 100 + index, source_vocabulary_id, source_code, status, occurrence_count: 0,
    source_code_description: 'Step count', origin_system: 'wearable-seed',
    destination_concept_id: 900, destination_concept_name: 'Steps', destination_standard_concept: 'S',
  }));
  const estimates: Record<string, unknown>[] = [];
  beforeEach(() => {
    estimates.length = 0;
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === '/v1/concept-to-code/') return Promise.resolve({ data: page([steps]) });
      if (url === '/v1/concept-to-code/axis-variant-walk/') {
        return Promise.resolve({ data: { active: null, last: null, completed: null, patients_since: 0, walked_at: null } });
      }
      if (url.endsWith('/estimate/')) {
        estimates.push(config?.params ?? {});
        return Promise.resolve({ data: { total: 5, seen: 0, umls: 0, lexical: 5, capped: false, timed_out: false } });
      }
      // The stored mappings of the concept: every one, as the server lists them.
      const rows = seeded.filter(row => row.status === config?.params?.status);
      return Promise.resolve({ data: { ...page(rows), zero_seen: seeded.length, status_counts: steps.sccm_counts } });
    });
  });
  const openSteps = async (domain?: string) => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByRole('button', { name: 'Steps' });
    if (domain !== undefined) {
      fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: domain } });
      await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/',
        expect.objectContaining({ params: expect.objectContaining({ domain }) })));
    }
    fireEvent.click(await screen.findByRole('button', { name: 'Steps' }));
    return screen.getByRole('dialog', { name: 'Source codes for Steps' });
  };
  const seenFilter = (dialog: HTMLElement) => within(dialog).queryByRole('checkbox', { name: SEEN_FILTER });
  const codes = (group: HTMLElement) => within(group).getAllByRole('button', { name: /:/ }).map(item => item.textContent);

  it('lists the zero-Seen codes of every wearable vocabulary under Mapped Codes, by status, as the concept list counts them', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByRole('button', { name: 'Steps' });
    const listed = Array.from(screen.getByTestId('concept-row-900').querySelectorAll('td')).slice(-3).map(cell => cell.textContent);
    expect(listed).toEqual(['2', '3', '0']);
    fireEvent.click(screen.getByRole('button', { name: 'Steps' }));
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Steps' });
    // The work queue is on Seen > 0, its default outside the Wearables domain; that is not Mapped Codes' filter.
    expect(seenFilter(dialog)).toBeChecked();
    await showExisting(dialog);
    const proposed = within(dialog).getByTestId('mapping-group-proposed');
    await within(proposed).findByText('HealthConnect:StepsRecord');
    expect(codes(proposed)).toEqual(['HealthConnect:StepsRecord', 'OpenWearables:steps', 'HL7-PHR:stepCount']);
    // Each is listed with the Seen count it has, and its status.
    within(proposed).getAllByRole('row').slice(1, 4).forEach(row => {
      expect(within(row).getByText('0')).toBeInTheDocument();
      expect(within(row).getByText('proposed')).toBeInTheDocument();
    });
    // The group headers are the concept list's counts, and the summary says how many have no sightings.
    expect(within(dialog).getByRole('button', { name: 'Approved mappings (2)' })).toHaveAttribute('aria-expanded', 'false');
    expect(within(dialog).getByRole('button', { name: 'Proposed mappings (3)' })).toHaveAttribute('aria-expanded', 'true');
    expect(within(dialog).getByRole('button', { name: 'Rejected mappings (0)' })).toBeInTheDocument();
    expect(within(dialog).getByText('5 matching sources · 5 with Seen 0')).toBeInTheDocument();
    expect(within(dialog).queryByText(/excluded/)).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Approved mappings (2)' }));
    const approved = within(dialog).getByTestId('mapping-group-approved');
    await within(approved).findByText('Apple:HKQuantityTypeIdentifierStepCount');
    expect(codes(approved)).toEqual(['Apple:HKQuantityTypeIdentifierStepCount', 'Garmin:steps']);
    within(approved).getAllByRole('row').slice(1, 3).forEach(row => expect(within(row).getByText('approved')).toBeInTheDocument());

    // A group with nothing in it says so without sending the curator to a Seen filter.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rejected mappings (0)' }));
    expect(await within(dialog).findByText('No rejected mappings.')).toBeInTheDocument();
    expect(linkedParams(900).map(params => params.status)).toEqual(['proposed', 'approved', 'rejected']);
    expectNoSeenFilterOnMappedCodes(900);
  });

  it('leaves the zero-Seen count out of the Mapped Codes summary when every mapped code has been seen', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept])
      : { ...page([source]), zero_seen: 0, status_counts: { approved: 0, proposed: 1, rejected: 0 } } }));
    await selectConcept();
    expect(screen.getByText('1 matching sources')).toBeInTheDocument();
    expect(screen.queryByText(/with Seen 0/)).not.toBeInTheDocument();
  });

  it('shows the "> 0 only" checkbox on Suggested Codes and not on Mapped Codes', async () => {
    const dialog = await openSteps();
    expect(within(dialog).getByRole('button', { name: 'Suggested Codes' })).toHaveAttribute('aria-pressed', 'true');
    expect(seenFilter(dialog)).toBeInTheDocument();
    expect(within(dialog).getByText('> 0 only')).toBeInTheDocument();
    await showExisting(dialog);
    await within(dialog).findByText('HealthConnect:StepsRecord');
    expect(seenFilter(dialog)).not.toBeInTheDocument();
    expect(within(dialog).queryByText('> 0 only')).not.toBeInTheDocument();
    // The column and its ordering stay; the note filter is both views'.
    expect(within(dialog).getByRole('columnheader', { name: /Seen/ })).toHaveAttribute('aria-sort', 'descending');
    expect(within(dialog).getByRole('checkbox', { name: 'Only source codes with a curator note' })).toBeInTheDocument();
    // Back on Suggested Codes it is there again, as it was left.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Suggested Codes' }));
    expect(seenFilter(dialog)).toBeChecked();
  });

  it('does not refetch or change the Mapped Codes query when "> 0 only" is switched on Suggested Codes', async () => {
    const dialog = await openSteps();
    await showExisting(dialog);
    await within(dialog).findByText('HealthConnect:StepsRecord');
    const query = { mode: 'linked', status: 'proposed', search: '', page: 1 };
    expect(linkedParams(900)).toEqual([query]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Suggested Codes' }));
    await waitFor(() => expect(estimates.at(-1)).toMatchObject({ include_zero_seen: '0' }));
    fireEvent.click(seenFilter(dialog)!);
    expect(seenFilter(dialog)).not.toBeChecked();
    // The work queue's estimate follows the filter; that request is the only one it causes.
    await waitFor(() => expect(estimates.at(-1)).toMatchObject({ include_zero_seen: '1' }));
    // Longer than the groups' 250 ms debounce: a refetch would have gone out by now.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 400)); });
    expect(linkedParams(900)).toEqual([query]);

    // Returning reads the groups again, as every return does, with the same query and the same rows.
    await showExisting(dialog);
    await within(dialog).findByText('HealthConnect:StepsRecord');
    expect(linkedParams(900)).toEqual([query, query]);
    expect(codes(within(dialog).getByTestId('mapping-group-proposed')))
      .toEqual(['HealthConnect:StepsRecord', 'OpenWearables:steps', 'HL7-PHR:stepCount']);
    expect(within(dialog).getByRole('button', { name: 'Proposed mappings (3)' })).toBeInTheDocument();
  });

  it('starts Suggested Codes with "> 0 only" unchecked under the Wearables domain, and searches zero-Seen codes', async () => {
    post.mockResolvedValue({ data: { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 900 }, candidates: [
        { ...source, mapping_id: null, status: 'unmapped', source_vocabulary_id: 'HealthConnect', source_code: 'StepsCadenceRecord', occurrence_count: 0 },
      ] }] } });
    const dialog = await openSteps('wearables');
    expect(seenFilter(dialog)).not.toBeChecked();
    await waitFor(() => expect(estimates.at(-1)).toEqual({ strategies: 'umls,lexical', include_zero_seen: '1', include_declined: '0' }));
    expect(estimates.every(params => params.include_zero_seen === '1')).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Suggest source codes' }));
    // A platform's own code has no sightings and is shown, not filtered out of the preview.
    expect(await within(dialog).findByText('HealthConnect:StepsCadenceRecord')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', expect.objectContaining({ concept_ids: [900], include_zero_seen: true }));
    expect(within(dialog).queryByText(/changed Seen filter/)).not.toBeInTheDocument();
    // The curator can still narrow to seen codes.
    fireEvent.click(seenFilter(dialog)!);
    await waitFor(() => expect(estimates.at(-1)).toMatchObject({ include_zero_seen: '0' }));
    expect(within(dialog).queryByText('HealthConnect:StepsCadenceRecord')).not.toBeInTheDocument();
    // Mapped Codes is the same under this domain: no Seen filter, sent or shown.
    await showExisting(dialog);
    await within(dialog).findByText('HealthConnect:StepsRecord');
    expect(seenFilter(dialog)).not.toBeInTheDocument();
    expectNoSeenFilterOnMappedCodes(900);
  });

  it.each([['All domains', undefined], ['Measurement', 'measurement'], ['Observation', 'observation']] as const)(
    'starts Suggested Codes with "> 0 only" checked under %s, and searches seen codes only', async (_label, domain) => {
      post.mockResolvedValue({ data: { ...run(), activity: [{ concept: { concept_id: 900 }, candidates: [] }] } });
      const dialog = await openSteps(domain);
      expect(seenFilter(dialog)).toBeChecked();
      await waitFor(() => expect(estimates.at(-1)).toEqual({ strategies: 'umls,lexical', include_zero_seen: '0', include_declined: '0' }));
      fireEvent.click(within(dialog).getByRole('button', { name: 'Suggest source codes' }));
      await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/',
        expect.objectContaining({ concept_ids: [900], include_zero_seen: false })));
    });

  it('takes the default from the domain in force when the concept is opened, not from the one before', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByRole('button', { name: 'Steps' });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Steps' }));
    expect(seenFilter(screen.getByRole('dialog'))).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'measurement' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Steps' }));
    expect(seenFilter(screen.getByRole('dialog'))).toBeChecked();
  });
});

describe('the Wearables domain and the remembered domain (#2656)', () => {
  const listParams = () => get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/').map(([, config]) => config.params);
  const lastListParams = () => listParams().at(-1);

  it('offers Wearables after All domains and asks the server for it', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByRole('button', { name: 'Serum albumin' });
    const domain = screen.getByLabelText('Concept domain') as HTMLSelectElement;
    expect(Array.from(domain.options).map(option => option.textContent).slice(0, 2)).toEqual(['All domains', 'Wearables']);
    expect(screen.getByLabelText('Concept scope')).toBeEnabled();
    fireEvent.change(domain, { target: { value: 'wearables' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables', page: 1 }));
    // Wearables is its own scope, so the scope choice is set aside while it is chosen.
    expect(screen.getByLabelText('Concept scope')).toBeDisabled();
    fireEvent.change(domain, { target: { value: 'measurement' } });
    expect(screen.getByLabelText('Concept scope')).toBeEnabled();
  });

  it('restores the domain an account chose, and only for that account', async () => {
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByRole('button', { name: 'Serum albumin' });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables' }));
    expect(JSON.parse(localStorage.getItem('promop-concept-to-code:7') || '{}')).toEqual({ domain: 'wearables' });
    first.unmount();

    get.mockClear();
    const again = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByRole('button', { name: 'Serum albumin' });
    expect(screen.getByLabelText('Concept domain')).toHaveValue('wearables');
    // The first request already carries it: no flash of the unfiltered list.
    expect(listParams()[0]).toMatchObject({ domain: 'wearables' });
    again.unmount();

    get.mockClear();
    render(<ConceptToCodeTab canWrite canApprove userId={8} />);
    await screen.findByRole('button', { name: 'Serum albumin' });
    expect(screen.getByLabelText('Concept domain')).toHaveValue('');
    expect(listParams()[0]).toMatchObject({ domain: '' });
  });

  it('remembers any domain, and a return to All domains', async () => {
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByRole('button', { name: 'Serum albumin' });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'condition' } });
    first.unmount();
    const second = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('condition');
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: '' } });
    second.unmount();
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('');
  });

  it('reads the account\'s own choice when the account arrives after the first render', async () => {
    localStorage.setItem('promop-concept-to-code:7', JSON.stringify({ domain: 'wearables' }));
    const view = render(<ConceptToCodeTab canWrite canApprove />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('');
    view.rerender(<ConceptToCodeTab canWrite canApprove userId={7} />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('wearables');
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables' }));
    // Nothing was saved over it on the way.
    expect(JSON.parse(localStorage.getItem('promop-concept-to-code:7') || '{}')).toEqual({ domain: 'wearables' });
  });

  it('ignores a stored value that is not a domain, or not JSON', async () => {
    localStorage.setItem('promop-concept-to-code:7', JSON.stringify({ domain: 'not-a-domain' }));
    const view = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('');
    view.unmount();
    localStorage.setItem('promop-concept-to-code:7', '{broken');
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    expect(screen.getByLabelText('Concept domain')).toHaveValue('');
    await screen.findByRole('button', { name: 'Serum albumin' });
  });
});

describe('categories on the concept list (#2690, #2699)', () => {
  type Row = typeof concept & { category?: string; priority?: number | null };
  const listCalls = () => get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/').map(([, config]) => config.params as Record<string, unknown>);
  /** The requests for the headers (and for a plain list). */
  const listParams = () => listCalls().filter(params => !('category' in params));
  /** The requests for the rows of one category. */
  const categoryParams = (name?: string) => listCalls().filter(params => 'category' in params && (name === undefined || params.category === name));
  const lastListParams = () => listParams().at(-1);
  const filed = (id: number, name: string, category: string, priority: number | null = null): Row =>
    ({ ...concept, concept_id: id, concept_name: name, category, priority });
  const rows = [filed(1, 'Steps', 'Activity', 1), filed(2, 'Distance', 'Activity'), filed(3, 'Sleep duration', 'Sleep'), filed(4, 'Mystery metric', '')];
  let dataset: Row[];
  const categoriesOf = (list: Row[]) => {
    const counts = new Map<string, number>();
    for (const row of list) counts.set(row.category ?? '', (counts.get(row.category ?? '') ?? 0) + 1);
    return [...counts].map(([category, count]) => ({ category, count }));
  };
  /** What the server answers: the headers for view=categories, one category's page for category=, else the plain list. */
  const serve = (params: Record<string, unknown>) => {
    const base = { prioritized: 1, group_limit: 5000 };
    if (params.category !== undefined) {
      const wanted = params.category === '__none__' ? '' : params.category;
      const own = dataset.filter(row => (row.category ?? '') === wanted);
      const size = Number(params.page_size);
      const number = Number(params.page);
      return { ...page(own.slice((number - 1) * size, number * size)), ...base, total: own.length, page: number, page_size: size,
        order: 'category', grouped: true, categories: [{ category: wanted, count: own.length }] };
    }
    if (params.view === 'categories') {
      return { ...page([]), ...base, total: dataset.length, order: 'category', grouped: true, categories: categoriesOf(dataset) };
    }
    return { ...page(dataset), ...base, order: 'priority', grouped: false, categories: null };
  };
  let respond: (params: Record<string, unknown>) => unknown;
  beforeEach(() => {
    dataset = rows;
    respond = serve;
    get.mockImplementation((url: string, config?: { params: Record<string, unknown> }) => Promise.resolve({
      data: url === '/v1/concept-to-code/' ? respond(config?.params ?? {}) : page([source]) }));
    post.mockImplementation((_url: string, body: { position: number | null }) => Promise.resolve({ data: { position: body.position } }));
  });
  /** A category's header button; a concept's own button can begin with the same word. */
  const header = (name: RegExp) => {
    const found = screen.getAllByRole('button', { name }).filter(element => element.closest('th[scope="rowgroup"]'));
    expect(found).toHaveLength(1);
    return found[0];
  };
  /** The grouped list has loaded: every header is there, and every category starts folded. */
  const loaded = () => screen.findByTestId('concept-group-Activity');
  const shownRows = () => screen.queryAllByTestId(/^concept-row-/).map(row => row.getAttribute('data-testid'));
  /** Opens every category (the list is short) and waits for their rows. */
  const expandAll = async (count = rows.length) => {
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    await waitFor(() => expect(shownRows()).toHaveLength(count));
  };
  const stored = (userId: number) => JSON.parse(localStorage.getItem(`promop-concept-to-code:${userId}`) || '{}');

  it('shows Category as the first column and the first cell of every row', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const table = await screen.findByRole('table', { name: 'Standard concept coverage' });
    await expandAll();
    const head = table.querySelector('thead') as HTMLElement;
    expect(within(head).getAllByRole('columnheader').map(cell => cell.textContent)).toEqual([
      'Category', 'Priority', 'Standard concept', 'Patient fields', 'Approved', 'Proposed', 'Rejected']);
    expect(within(screen.getByTestId('concept-row-1')).getAllByRole('cell')[0]).toHaveTextContent('Activity');
    expect(within(screen.getByTestId('concept-row-3')).getAllByRole('cell')[0]).toHaveTextContent('Sleep');
    // No category is a muted dash, not an empty cell.
    expect(within(screen.getByTestId('concept-row-4')).getAllByRole('cell')[0]).toHaveTextContent('—');
    // Every row has as many cells as the header has columns, and a header row spans them all.
    expect(within(screen.getByTestId('concept-row-1')).getAllByRole('cell')).toHaveLength(7);
    expect(header(/^Activity/).closest('th')).toHaveAttribute('colspan', '7');
  });

  it('tolerates a response without categories: a plain list with a blank Category', async () => {
    respond = () => page([concept]);
    render(<ConceptToCodeTab canWrite canApprove />);
    const row = await screen.findByTestId('concept-row-123');
    expect(within(row).getAllByRole('cell')[0]).toHaveTextContent('—');
    expect(screen.getByRole('columnheader', { name: 'Category' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Collapse all' })).not.toBeInTheDocument();
    expect(screen.queryByText(/too many to group/)).not.toBeInTheDocument();
    expect(screen.getAllByRole('rowgroup')).toHaveLength(2);
  });

  it('first loads the headers alone, in one request that fetches no concept rows', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    expect(header(/^Activity/)).toHaveTextContent('Activity 2 concepts');
    expect(header(/^Sleep/)).toHaveTextContent('Sleep 1 concept');
    expect(header(/^Uncategorized/)).toHaveTextContent('Uncategorized 1 concept');
    expect(header(/^Sleep/)).toHaveAccessibleName('Sleep 1 concept');
    for (const name of [/^Activity/, /^Sleep/, /^Uncategorized/]) expect(header(name)).toHaveAttribute('aria-expanded', 'false');
    // One request, for the headers; no category was asked for and no page of rows.
    await new Promise(resolve => setTimeout(resolve, 400));
    expect(listCalls()).toHaveLength(1);
    expect(listCalls()[0]).toMatchObject({ view: 'categories', page: 1, page_size: 50 });
    expect(listCalls()[0]).not.toHaveProperty('category');
    expect(shownRows()).toEqual([]);
    // All the headers are on the one list: no pagination, however many concepts they hold.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.getByText('Categories are folded: open one to see its concepts, or Expand all.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Collapse all' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Expand all' })).toBeEnabled();
    expect(screen.queryByText(/continued/)).not.toBeInTheDocument();
    // Nothing is stored until the curator opens something.
    expect(localStorage.getItem('promop-concept-to-code:session')).toBeNull();
  });

  it('draws every header on one page however many concepts the list holds', async () => {
    dataset = Array.from({ length: 400 }, (_, index) => filed(1000 + index, `Metric ${index}`, index < 300 ? 'Sleep' : 'Activity'));
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    expect(header(/^Sleep/)).toHaveTextContent('Sleep 300 concepts');
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
  });

  it('opens one category by asking for its rows, with the filters, and folds it again', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Activity/));
    expect(header(/^Activity/)).toHaveAttribute('aria-expanded', 'true');
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-1', 'concept-row-2']));
    expect(header(/^Sleep/)).toHaveAttribute('aria-expanded', 'false');
    expect(header(/^Uncategorized/)).toHaveAttribute('aria-expanded', 'false');
    expect(categoryParams()).toEqual([{ domain: '', scope: 'fields', search: '', page: 1, page_size: 50, category: 'Activity' }]);
    expect(within(screen.getByTestId('concept-group-Activity')).getAllByTestId(/^concept-row-/)).toHaveLength(2);
    // Its header and count stay, and the hint goes while anything is open.
    expect(header(/^Activity/)).toHaveTextContent('2 concepts');
    expect(screen.queryByText(/Categories are folded/)).not.toBeInTheDocument();
    // No pagination for a category that fits on one page.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();

    // Folding discards the rows; opening again asks again.
    fireEvent.click(header(/^Activity/));
    expect(shownRows()).toEqual([]);
    expect(screen.getByText(/Categories are folded/)).toBeInTheDocument();
    fireEvent.click(header(/^Activity/));
    await waitFor(() => expect(shownRows()).toHaveLength(2));
    expect(categoryParams('Activity')).toHaveLength(2);
    expect(categoryParams('Sleep')).toHaveLength(0);
  });

  it('asks for the concepts with no category as __none__', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Uncategorized/));
    expect(await screen.findByTestId('concept-row-4')).toBeInTheDocument();
    expect(categoryParams().map(params => params.category)).toEqual(['__none__']);
  });

  it('sends the filters to the headers and to the opened rows alike', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Activity/));
    await screen.findByTestId('concept-row-1');
    fireEvent.change(screen.getByLabelText('Concept scope'), { target: { value: 'all' } });
    fireEvent.change(screen.getByLabelText('Search standard concepts'), { target: { value: 'step' } });
    fireEvent.click(screen.getByLabelText(/Ranked only/));
    const filters = { scope: 'all', search: 'step', prioritized: '1' };
    await waitFor(() => expect(lastListParams()).toMatchObject({ ...filters, view: 'categories' }));
    await waitFor(() => expect(categoryParams('Activity').at(-1)).toMatchObject({ ...filters, category: 'Activity', page: 1 }));
    expect(categoryParams('Activity').at(-1)).not.toHaveProperty('view');
  });

  it('pages inside a category only when it holds more than one page, each with its own page', async () => {
    dataset = [...rows.slice(0, 2), ...Array.from({ length: 120 }, (_, index) => filed(100 + index, `Sleep metric ${index}`, 'Sleep'))];
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Activity/));
    fireEvent.click(header(/^Sleep/));
    await waitFor(() => expect(shownRows()).toHaveLength(52));
    const sleepPages = within(screen.getByTestId('concept-group-Sleep')).getByRole('navigation', { name: 'Pages of Sleep' });
    expect(sleepPages).toHaveTextContent('Page 1 of 3');
    // Activity fits one page, so it has none.
    expect(within(screen.getByTestId('concept-group-Activity')).queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.getAllByRole('navigation')).toHaveLength(1);

    fireEvent.click(within(sleepPages).getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(within(screen.getByTestId('concept-group-Sleep')).getByRole('navigation')).toHaveTextContent('Page 2 of 3'));
    expect(categoryParams('Sleep').at(-1)).toMatchObject({ category: 'Sleep', page: 2, page_size: 50 });
    // The other category did not move and was not asked again.
    expect(categoryParams('Activity')).toHaveLength(1);
    expect(within(screen.getByTestId('concept-group-Sleep')).getAllByTestId(/^concept-row-/)[0]).toHaveAttribute('data-testid', 'concept-row-150');

    // A bigger page size starts every opened category over, at its first page.
    fireEvent.change(screen.getByLabelText('Concepts per page'), { target: { value: '100' } });
    await waitFor(() => expect(categoryParams('Sleep').at(-1)).toMatchObject({ page: 1, page_size: 100 }));
    await waitFor(() => expect(within(screen.getByTestId('concept-group-Sleep')).getByRole('navigation')).toHaveTextContent('Page 1 of 2'));
    // The headers did not ask for more than the one page they never paged through.
    expect(listParams().every(params => params.page === 1)).toBe(true);
  });

  it('fetches each remembered open category on load, and nothing else', async () => {
    localStorage.setItem('promop-concept-to-code:7', JSON.stringify({ domain: '', expanded: { '': ['Sleep', 'Gone'], wearables: ['Activity'] } }));
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-3']));
    expect(header(/^Sleep/)).toHaveAttribute('aria-expanded', 'true');
    expect(header(/^Activity/)).toHaveAttribute('aria-expanded', 'false');
    await new Promise(resolve => setTimeout(resolve, 400));
    // The headers once; Sleep's rows once; not Activity (another domain's) nor a category that is gone.
    expect(listParams()).toHaveLength(1);
    expect(categoryParams().map(params => params.category)).toEqual(['Sleep']);
  });

  it('opens every category with Expand all, one request each, and folds them with Collapse all', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await expandAll();
    for (const name of [/^Activity/, /^Sleep/, /^Uncategorized/]) expect(header(name)).toHaveAttribute('aria-expanded', 'true');
    expect(categoryParams().map(params => params.category).sort()).toEqual(['Activity', 'Sleep', '__none__']);
    expect(screen.getByRole('button', { name: 'Expand all' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Collapse all' })).toBeEnabled();
    expect(within(screen.getByTestId('concept-group-none')).getByTestId('concept-row-4')).toBeInTheDocument();

    fireEvent.click(header(/^Sleep/));
    expect(shownRows()).toEqual(['concept-row-1', 'concept-row-2', 'concept-row-4']);
    expect(screen.getByRole('button', { name: 'Expand all' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    expect(shownRows()).toEqual([]);
    expect(screen.getByRole('button', { name: 'Collapse all' })).toBeDisabled();
    // Folding asks the server for nothing.
    expect(categoryParams()).toHaveLength(3);
    expect(listParams()).toHaveLength(1);
  });

  it('offers Expand all only for twelve categories or fewer', async () => {
    const many = (count: number) => Array.from({ length: count }, (_, index) => filed(500 + index, `Metric ${index}`, `Class ${String(index).padStart(2, '0')}`));
    dataset = many(12);
    const first = render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByTestId('concept-group-Class 00');
    expect(screen.getByRole('button', { name: 'Expand all' })).toBeEnabled();
    first.unmount();

    dataset = many(13);
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByTestId('concept-group-Class 00');
    const expand = screen.getByRole('button', { name: 'Expand all' });
    expect(expand).toBeDisabled();
    expect(expand).toHaveAttribute('title', expect.stringMatching(/12 categories or fewer: this list has 13/));
    expect(screen.getByText('Categories are folded: open one to see its concepts.')).toBeInTheDocument();
    // Opening one by one still works.
    fireEvent.click(header(/^Class 03/));
    expect(await screen.findByTestId('concept-row-503')).toBeInTheDocument();
  });

  it('keeps no more than six category requests in flight, and answers the rest in turn', async () => {
    dataset = Array.from({ length: 10 }, (_, index) => filed(600 + index, `Metric ${index}`, `Class ${String(index).padStart(2, '0')}`));
    localStorage.setItem('promop-concept-to-code:7', JSON.stringify({ domain: '', expanded: { '': dataset.map(row => row.category) } }));
    let flying = 0;
    let peak = 0;
    const waiting: (() => void)[] = [];
    get.mockImplementation((url: string, config?: { params: Record<string, unknown> }) => {
      if (url !== '/v1/concept-to-code/' || config?.params.category === undefined) {
        return Promise.resolve({ data: url === '/v1/concept-to-code/' ? serve(config?.params ?? {}) : page([source]) });
      }
      flying += 1; peak = Math.max(peak, flying);
      return new Promise(resolve => waiting.push(() => { flying -= 1; resolve({ data: serve(config!.params) }); }));
    });
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByTestId('concept-group-Class 00');
    await waitFor(() => expect(waiting).toHaveLength(6));
    await new Promise(resolve => setTimeout(resolve, 300));
    expect(waiting).toHaveLength(6);
    expect(peak).toBe(6);
    // As each answers, the next starts.
    while (shownRows().length < 10) {
      await act(async () => { waiting.splice(0).forEach(release => release()); await new Promise(resolve => setTimeout(resolve, 20)); });
    }
    expect(peak).toBe(6);
    expect(categoryParams()).toHaveLength(10);
  });

  it('asks again after a write, for the headers and the opened categories only', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Activity/));
    const distance = await screen.findByLabelText('Priority for Distance');
    fireEvent.change(distance, { target: { value: '1' } });
    fireEvent.keyDown(distance, { key: 'Enter' });
    expect(await screen.findByText('Moved Distance to priority 1.')).toBeInTheDocument();
    await waitFor(() => expect(listParams()).toHaveLength(2));
    await waitFor(() => expect(categoryParams('Activity')).toHaveLength(2));
    await new Promise(resolve => setTimeout(resolve, 400));
    expect(categoryParams('Sleep')).toHaveLength(0);
    expect(categoryParams()).toHaveLength(2);
  });

  it('remembers the categories a curator opened per account and per domain', async () => {
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables' }));
    await loaded();
    // A new domain starts folded, whatever was open under another.
    expect(shownRows()).toEqual([]);
    expect(stored(7)).toEqual({ domain: 'wearables' });
    fireEvent.click(header(/^Sleep/));
    fireEvent.click(header(/^Uncategorized/));
    expect(stored(7)).toEqual({ domain: 'wearables', expanded: { wearables: ['Sleep', ''] } });
    first.unmount();

    const again = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    expect(header(/^Sleep/)).toHaveAttribute('aria-expanded', 'true');
    expect(header(/^Activity/)).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-3', 'concept-row-4']));
    // Another domain has its own set, and coming back restores this one.
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'measurement' } });
    expect(header(/^Sleep/)).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(header(/^Activity/));
    expect(stored(7)).toEqual({ domain: 'measurement', expanded: { wearables: ['Sleep', ''], measurement: ['Activity'] } });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-3', 'concept-row-4']));
    // Folding everything leaves nothing to remember for the domain. Wearables
    // has no Collapse all (#2795): each header folds its own category.
    fireEvent.click(header(/^Sleep/));
    fireEvent.click(header(/^Uncategorized/));
    expect(stored(7)).toEqual({ domain: 'wearables', expanded: { measurement: ['Activity'] } });
    again.unmount();

    render(<ConceptToCodeTab canWrite canApprove userId={8} />);
    await loaded();
    expect(shownRows()).toEqual([]);
  });

  it('sends the order only when it is Priority, and remembers it per account', async () => {
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    const order = screen.getByLabelText('Concept order') as HTMLSelectElement;
    expect(Array.from(order.options).map(option => option.textContent)).toEqual(['Category, then priority', 'Priority']);
    expect(order).toHaveValue('category');
    expect(lastListParams()).not.toHaveProperty('order');
    expect(lastListParams()).toMatchObject({ view: 'categories' });

    fireEvent.change(order, { target: { value: 'priority' } });
    // The priority list is the plain paged list: rows, and no headers view.
    await waitFor(() => expect(lastListParams()).toMatchObject({ order: 'priority', page: 1 }));
    expect(lastListParams()).not.toHaveProperty('view');
    await screen.findByTestId('concept-row-1');
    expect(stored(7)).toEqual({ domain: '', order: 'priority' });
    first.unmount();

    get.mockClear();
    const again = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByTestId('concept-row-1');
    expect(screen.getByLabelText('Concept order')).toHaveValue('priority');
    expect(listParams()[0]).toMatchObject({ order: 'priority' });
    fireEvent.change(screen.getByLabelText('Concept order'), { target: { value: 'category' } });
    await waitFor(() => expect(lastListParams()).not.toHaveProperty('order'));
    expect(lastListParams()).toMatchObject({ view: 'categories' });
    expect(stored(7)).toEqual({ domain: '' });
    again.unmount();

    get.mockClear();
    render(<ConceptToCodeTab canWrite canApprove userId={8} />);
    await loaded();
    expect(screen.getByLabelText('Concept order')).toHaveValue('category');
    expect(listParams()[0]).not.toHaveProperty('order');
  });

  it('opens Wearables by category whatever the other domains use, and keeps its own choice (#2795)', async () => {
    localStorage.setItem('promop-concept-to-code:7', JSON.stringify({ domain: 'measurement', order: 'priority' }));
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByTestId('concept-row-1');
    expect(screen.getByLabelText('Concept order')).toHaveValue('priority');

    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    expect(screen.getByLabelText('Concept order')).toHaveValue('category');
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables', view: 'categories' }));
    expect(lastListParams()).not.toHaveProperty('order');
    await loaded();
    // Going to Wearables changed neither order.
    expect(stored(7)).toEqual({ domain: 'wearables', order: 'priority' });

    // Priority chosen here is Wearables' own, and the other domains keep theirs.
    fireEvent.change(screen.getByLabelText('Concept order'), { target: { value: 'priority' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables', order: 'priority' }));
    expect(stored(7)).toEqual({ domain: 'wearables', order: 'priority', wearables_order: 'priority' });
    first.unmount();

    const again = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByTestId('concept-row-1');
    expect(screen.getByLabelText('Concept order')).toHaveValue('priority');
    fireEvent.change(screen.getByLabelText('Concept order'), { target: { value: 'category' } });
    await loaded();
    expect(stored(7)).toEqual({ domain: 'wearables', order: 'priority' });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'measurement' } });
    expect(screen.getByLabelText('Concept order')).toHaveValue('priority');
    again.unmount();
  });

  it('offers no Expand all or Collapse all under Wearables (#2795)', async () => {
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    expect(screen.getByRole('button', { name: 'Expand all' })).toBeInTheDocument();
    expect(screen.getByText('Categories are folded: open one to see its concepts, or Expand all.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'wearables' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ domain: 'wearables' }));
    await loaded();
    expect(screen.queryByRole('group', { name: 'Categories' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Expand all' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Collapse all' })).not.toBeInTheDocument();
    expect(screen.getByText('Categories are folded: open one to see its concepts.')).toBeInTheDocument();
    // A category still opens from its own header.
    fireEvent.click(header(/^Sleep/));
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-3']));
  });

  it('ignores a stored order or opened set of the wrong shape, keeping what is well formed', async () => {
    const key = 'promop-concept-to-code:7';
    localStorage.setItem(key, JSON.stringify({ domain: 'wearables', order: 'sideways', expanded: ['Sleep'] }));
    const first = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    expect(screen.getByLabelText('Concept domain')).toHaveValue('wearables');
    expect(screen.getByLabelText('Concept order')).toHaveValue('category');
    expect(shownRows()).toEqual([]);
    first.unmount();

    // One domain's entry is a string, another's holds a number, a third is no domain.
    localStorage.setItem(key, JSON.stringify({ domain: 'measurement', order: 'priority', wearables_order: 'sideways', expanded: {
      wearables: 'Sleep', measurement: [1], sideways: ['Activity'], condition: ['Activity'] } }));
    const second = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await screen.findByTestId('concept-row-1');
    expect(screen.getByLabelText('Concept order')).toHaveValue('priority');
    fireEvent.change(screen.getByLabelText('Concept order'), { target: { value: 'category' } });
    await loaded();
    expect(shownRows()).toEqual([]);
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'condition' } });
    await waitFor(() => expect(shownRows()).toEqual(['concept-row-1', 'concept-row-2']));
    // The next save writes only what was understood.
    fireEvent.click(header(/^Sleep/));
    expect(stored(7)).toEqual({ domain: 'condition', expanded: { condition: ['Activity', 'Sleep'] } });
    second.unmount();

    for (const broken of ['null', '[]', '"wearables"', '{broken']) {
      localStorage.setItem(key, broken);
      const view = render(<ConceptToCodeTab canWrite canApprove userId={7} />);
      expect(screen.getByLabelText('Concept domain')).toHaveValue('');
      expect(screen.getByLabelText('Concept order')).toHaveValue('category');
      view.unmount();
    }
  });

  it('ignores a stored collapsed list from before categories started folded', async () => {
    const key = 'promop-concept-to-code:7';
    localStorage.setItem(key, JSON.stringify({ domain: 'wearables', collapsed: { wearables: ['Sleep'] } }));
    render(<ConceptToCodeTab canWrite canApprove userId={7} />);
    await loaded();
    expect(screen.getByLabelText('Concept domain')).toHaveValue('wearables');
    // Folded is the default now: the old list said what was shut, so it says nothing about what is open.
    for (const name of [/^Activity/, /^Sleep/, /^Uncategorized/]) expect(header(name)).toHaveAttribute('aria-expanded', 'false');
    expect(shownRows()).toEqual([]);
    fireEvent.click(header(/^Activity/));
    expect(stored(7)).toEqual({ domain: 'wearables', expanded: { wearables: ['Activity'] } });
  });

  it('says so when the list is too long to group, and then shows its rows from the one request', async () => {
    respond = params => ({ ...page(rows), total: 2700000, prioritized: 1, order: 'priority', grouped: false,
      group_limit: 5000, categories: null, ...(params.order === 'priority' ? { total: 4 } : {}) });
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByTestId('concept-row-1');
    const note = screen.getByText(/too\s+many to group by category/);
    expect(note).toHaveTextContent('This list has 2,700,000 concepts');
    expect(note).toHaveTextContent('the limit is 5,000');
    expect(note).toHaveTextContent(/domain or a disease, or search/);
    expect(screen.queryByRole('button', { name: /^Activity/ })).not.toBeInTheDocument();
    expect(document.querySelector('th[scope="rowgroup"]')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Collapse all' })).not.toBeInTheDocument();
    expect(within(screen.getByTestId('concept-row-3')).getAllByRole('cell')[0]).toHaveTextContent('Sleep');
    expect(shownRows()).toHaveLength(4);
    // It is paged as it always was.
    expect(listCalls()).toHaveLength(1);

    // A list the curator asked for in priority order is not "too long": no note.
    fireEvent.change(screen.getByLabelText('Concept order'), { target: { value: 'priority' } });
    await waitFor(() => expect(lastListParams()).toMatchObject({ order: 'priority' }));
    await waitFor(() => expect(screen.queryByText(/too\s+many to group by category/)).not.toBeInTheDocument());
    expect(shownRows()).toHaveLength(4);
  });

  it('keeps opening, ranking and dragging working inside groups, across categories', async () => {
    const moved = () => post.mock.calls.filter(([url]) => url === '/v1/concept-to-code/priority/').map(([, body]) => body);
    render(<ConceptToCodeTab canWrite canApprove />);
    await expandAll();
    const distance = await screen.findByLabelText('Priority for Distance');
    expect(screen.getByLabelText('Priority for Steps')).toHaveValue(1);
    fireEvent.change(distance, { target: { value: '1' } });
    fireEvent.keyDown(distance, { key: 'Enter' });
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 2, position: 1 }]));
    expect(await screen.findByText('Moved Distance to priority 1.')).toBeInTheDocument();

    // A drop on a row of another category still sets the one global position.
    const mystery = screen.getByTestId('concept-row-4');
    const steps = screen.getByTestId('concept-row-1');
    await waitFor(() => expect(mystery).toHaveAttribute('draggable', 'true'));
    fireEvent.dragStart(mystery);
    fireEvent.dragOver(steps);
    fireEvent.drop(steps);
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 2, position: 1 }, { concept_id: 4, position: 1 }]));
    // Onto an unranked row of another category: the end of the list.
    await waitFor(() => expect(screen.getByTestId('concept-row-1')).toHaveAttribute('draggable', 'true'));
    fireEvent.dragStart(screen.getByTestId('concept-row-1'));
    fireEvent.drop(screen.getByTestId('concept-row-3'));
    await waitFor(() => expect(moved()).toHaveLength(3));
    expect(moved()[2]).toEqual({ concept_id: 1, position: 2 });

    fireEvent.click(screen.getByRole('button', { name: /^Lock Sleep duration/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/3/lock/'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Steps' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Steps' }));
    expect(screen.getByRole('dialog', { name: 'Source codes for Steps' })).toBeInTheDocument();
  });

  it('says dragging works among the opened categories', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    expect(screen.getByText(/Categories start folded; dragging reorders the rows of the categories you have opened/)).toBeInTheDocument();
  });

  it('shows a note saved on a row of an opened category at once', async () => {
    const view = render(<ConceptToCodeTab canWrite canApprove />);
    await loaded();
    fireEvent.click(header(/^Sleep/));
    await screen.findByTestId('concept-row-3');
    // The reload that follows the save is held back: the row shows the note before it.
    const held: (() => void)[] = [];
    const answer = get.getMockImplementation()!;
    put.mockResolvedValue({ data: { note: { text: 'Check units', updated_by: 'me' } } });
    fireEvent.click(screen.getByRole('button', { name: 'Add a note for Sleep duration' }));
    fireEvent.change(await screen.findByRole('textbox', { name: 'Note for Sleep duration' }), { target: { value: 'Check units' } });
    get.mockImplementation((...args: [string, { params: Record<string, unknown> }]) => new Promise(resolve => {
      held.push(() => resolve(answer(...args)));
    }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit the note for Sleep duration' })).toHaveAttribute('data-has-note', 'true'));
    await waitFor(() => expect(held.length).toBeGreaterThan(0));
    view.unmount();
  });
});

describe('Download and Share on the Concept tab under Wearables', () => {
  beforeEach(() => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => Promise.resolve({
      data: url === '/v1/concept-to-code/' ? { ...page([]), order: 'category', grouped: true, group_limit: 5000, categories: [], prioritized: 0 }
        : url === '/v1/code-mappings/vote-shares/' ? { results: [], email_configured: true, base_url_configured: true }
          : config ? page([]) : page([]) }));
  });
  const chooseDomain = async (value: string) => {
    fireEvent.change(await screen.findByLabelText('Concept domain'), { target: { value } });
  };

  it('offers the download to a reader and the share to an approver, under Wearables only', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByLabelText('Concept domain');
    expect(screen.queryByRole('group', { name: 'Download all wearable mappings' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();

    await chooseDomain('wearables');
    const download = await screen.findByRole('group', { name: 'Download all wearable mappings' });
    // From (waiting for the systems here), CSV and Word.
    expect(within(download).getAllByRole('button')).toHaveLength(3);
    expect(screen.getByRole('button', { name: 'Share' })).toBeInTheDocument();

    await chooseDomain('measurement');
    expect(screen.queryByRole('group', { name: 'Download all wearable mappings' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });

  it('shows a reader who cannot approve the download but not the share', async () => {
    render(<ConceptToCodeTab canWrite={false} canApprove={false} />);
    await chooseDomain('wearables');
    expect(await screen.findByRole('group', { name: 'Download all wearable mappings' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Share' })).not.toBeInTheDocument();
  });

  it('downloads the whole tab from the Concept tab through the same endpoint', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:x'); URL.revokeObjectURL = vi.fn();
    render(<ConceptToCodeTab canWrite canApprove />);
    await chooseDomain('wearables');
    await screen.findByRole('group', { name: 'Download all wearable mappings' });
    get.mockClear();
    get.mockResolvedValue({ data: new Blob(['a']), headers: {} });
    fireEvent.click(screen.getByRole('button', { name: /CSV/ }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/code-mappings/wearables-export/', expect.objectContaining({
      params: { file_format: 'csv' } })));
  });

  it('reads From the source systems, To the target, CSV and Word, then Share (#2795)', async () => {
    URL.createObjectURL = vi.fn(() => 'blob:x'); URL.revokeObjectURL = vi.fn();
    wearableSystems = [{ id: 'Apple', label: 'HealthKit' }, { id: 'HealthConnect', label: 'Health Connect' },
      { id: 'Garmin', label: 'Garmin' }, { id: 'HL7-PHR', label: 'HL7 PHR' }];
    render(<ConceptToCodeTab canWrite canApprove />);
    await chooseDomain('wearables');
    const row = await screen.findByRole('group', { name: 'Wearable mappings' });
    // No sentence before the controls; the Share button keeps its one word.
    expect(row).not.toHaveTextContent('Download all wearable mappings');
    expect(row).toHaveTextContent(/^From\s*All source systems\s*To\s*OMOP conceptCDISC Biomedical Concept\s*CSV\s*Word\s*Share$/);
    expect(within(row).getAllByRole('button').map(button => button.textContent?.trim()))
      .toEqual(['All source systems', 'CSV', 'Word', 'Share']);

    // Several systems at once: the HL7 PHR codes beside HealthKit's and Health Connect's.
    await userEvent.click(within(row).getByRole('button', { name: /^Source code systems:/ }));
    for (const system of ['HL7 PHR', 'HealthKit', 'Health Connect']) await userEvent.click(screen.getByLabelText(system));
    get.mockClear();
    get.mockResolvedValue({ data: new Blob(['a']), headers: {} });
    await userEvent.click(within(row).getByRole('button', { name: 'Download all wearable mappings as CSV' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/code-mappings/wearables-export/', expect.objectContaining({
      params: { file_format: 'csv', sources: 'Apple,HealthConnect,HL7-PHR' } })));
  });

  it('opens the share dialog from the Concept tab', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await chooseDomain('wearables');
    fireEvent.click(await screen.findByRole('button', { name: 'Share' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

describe('suggest accumulation (#2274)', () => {
  it('sends exclude_keys from a completed inline run on the second Suggest click', async () => {
    const first = { ...source, mapping_id: null, source_code: 'A1', source_vocabulary_id: 'V1',
      evidence: ['lexical'], verdict: 'review', confidence: 0.9, declined: false };
    const firstRun = { ...run(), activity: [{ concept: { concept_id: 123 }, candidates: [first] }] };
    post.mockResolvedValueOnce({ data: firstRun });
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('1 candidates retrieved (limit 25).');
    // The first run's exclude_keys should be empty.
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/',
      expect.objectContaining({ exclude_keys: [] }));

    // Now click Suggest again — the second run should exclude the first run's candidate.
    const second = { ...source, mapping_id: null, source_code: 'B1', source_vocabulary_id: 'V2',
      evidence: ['lexical'], verdict: 'review', confidence: 0.8, declined: false };
    const secondRun = { ...run(), run_id: 'run-2',
      activity: [{ concept: { concept_id: 123 }, candidates: [second] }] };
    post.mockResolvedValueOnce({ data: secondRun });
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    // Second run retrieves 1 new candidate, 2 total across runs.
    await screen.findByText(/1 candidates retrieved \(limit 25\), 2 total across runs/);
    expect(post).toHaveBeenLastCalledWith('/v1/concept-to-code/suggest/',
      expect.objectContaining({ exclude_keys: [['V1', 'A1']] }));
  });
});

describe('axis variants on a source code (#2012)', () => {
  const glucose = { concept_id: 3020491, concept_name: 'Glucose [Moles/volume] in Serum or Plasma', concept_code: '14749-6', vocabulary_id: 'LOINC' };
  const variants = [
    { id: 1, axis_key: 'property=MCnc', axis_values: { property: 'MCnc', scale_type: 'Qn' }, units: ['mg/dL'], occurrence_count: 7,
      status: 'approved', target_concept: { ...glucose, concept_id: 3004501, concept_code: '2345-7', concept_name: 'Glucose [Mass/volume] in Serum or Plasma' }, reviewed_at: null },
    { id: 2, axis_key: 'property=SCnc', axis_values: { property: 'SCnc', scale_type: 'Qn' }, units: ['mmol/L'], occurrence_count: 4,
      status: 'proposed', target_concept: glucose, reviewed_at: null },
  ];
  const flagged = { ...source, axis_variant_count: 2, axis_variants: variants };

  it('lists each variant with its units and suggested LOINC under the code, and approves one in place', async () => {
    post.mockImplementation((url: string) => Promise.resolve({ data: url.endsWith('/axis-variants/')
      ? { mapping_id: 7, applies: true, variants: variants.map(v => ({ ...v, status: 'approved' })) }
      : { ...run(), activity: [{ concept: { concept_id: 123 }, candidates: [flagged] }] } }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    const list = await screen.findByRole('list', { name: 'Axis variants' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(list).toHaveTextContent('Property SCnc · Scale Qn');
    expect(list).toHaveTextContent('mmol/L');
    expect(list).toHaveTextContent(glucose.concept_name);
    // Only the proposed variant can be approved; the approved one is a decision.
    const approve = within(list).getAllByRole('button', { name: /^Approve / });
    expect(approve).toHaveLength(1);
    expect(approve[0]).toHaveAccessibleName('Approve Property SCnc · Scale Qn');
    fireEvent.click(approve[0]);
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/code-mappings/7/axis-variants/', {
      axis_key: 'property=SCnc', target_concept_id: glucose.concept_id, approve: true }));
    await screen.findByText('Approved the mmol/L variant of S1.');
    expect(within(list).queryByRole('button', { name: /^Approve / })).not.toBeInTheDocument();
    // Approving a variant never opened the editor.
    expect(screen.queryByRole('dialog', { name: 'Edit Mapping' })).not.toBeInTheDocument();
  });

  it('keeps a variant approved while the run streams when the next poll replaces the run (#2028)', async () => {
    const streaming = { ...run('running'), activity: [{ concept: { concept_id: 123 }, candidates: [flagged] }] };
    post.mockImplementation((url: string) => Promise.resolve({ data: url.endsWith('/axis-variants/')
      ? { mapping_id: 7, applies: true, variants: variants.map(v => ({ ...v, status: 'approved' })) }
      : streaming }));
    const polls = () => get.mock.calls.filter(([url]) => String(url).includes('suggest-runs')).length;
    // Each poll answers with a fresh object, as the server does, so the run really is replaced.
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('suggest-runs') ? { ...streaming, done: polls() }
      : url === '/v1/concept-to-code/' ? page([concept]) : page([source]) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    const list = await screen.findByRole('list', { name: 'Axis variants' });
    expect(screen.getByRole('button', { name: 'Searching…' })).toBeDisabled();
    // Approving in place is an individual edit, so the running search does not block it.
    const approve = within(list).getByRole('button', { name: /^Approve / });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);
    await screen.findByText('Approved the mmol/L variant of S1.');
    // The next poll brings the run's own copy of the row, still proposed; the approval stands.
    const before = polls();
    await waitFor(() => expect(polls()).toBeGreaterThan(before), { timeout: 5000 });
    expect(within(screen.getByRole('list', { name: 'Axis variants' })).queryByRole('button', { name: /^Approve / })).not.toBeInTheDocument();
  });

  it('shows variants on an existing mapping too, without Approve for curators who cannot approve', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept]) : page([flagged]) }));
    await selectConcept(false);
    const list = screen.getByRole('list', { name: 'Axis variants' });
    expect(screen.getByText('2 axis values')).toBeInTheDocument();
    expect(list).toHaveTextContent('mg/dL');
    expect(within(list).queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('suggestion confidence', () => {
  const scored = [
    { ...source, mapping_id: 1, source_code: 'HIGH', confidence: 0.85 },
    { ...source, mapping_id: 2, source_code: 'MID', confidence: 0.55 },
    { ...source, mapping_id: 3, source_code: 'NONE', confidence: null },
    { ...source, mapping_id: 4, source_code: 'DONE', status: 'approved', confidence: 0.9 },
  ];
  const sourceGets = () => get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/123/');

  beforeEach(() => {
    get.mockImplementation((url: string) => Promise.resolve({
      data: url === '/v1/concept-to-code/' ? page([concept]) : page(scored),
    }));
  });

  async function openScored() {
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    return screen.findByText('SNOMED:HIGH');
  }

  it('shows the confidence of every mapping that has one, regardless of status', async () => {
    await openScored();
    const cell = (code: string) => screen.getByText(`SNOMED:${code}`).closest('tr')!.lastElementChild as HTMLElement;
    expect(cell('HIGH')).toHaveTextContent('Confidence: 85%');
    expect(cell('MID')).toHaveTextContent('Confidence: 55%');
    expect(cell('NONE')).not.toHaveTextContent('Confidence');
    expect(cell('DONE')).toHaveTextContent('Confidence: 90%');
  });

  it('orders by confidence from the last column header and back by Seen', async () => {
    await openScored();
    const header = screen.getByRole('button', { name: /Status \/ evidence · Confidence/ });
    fireEvent.click(header);
    await waitFor(() => expect(sourceGets().at(-1)![1].params).toMatchObject({ order: '-confidence' }));
    expect(header.closest('th')).toHaveAttribute('aria-sort', 'descending');
    await screen.findByText('SNOMED:HIGH');
    fireEvent.click(header);
    await waitFor(() => expect(sourceGets().at(-1)![1].params).toMatchObject({ order: 'confidence' }));
    expect(header.closest('th')).toHaveAttribute('aria-sort', 'ascending');
    await screen.findByText('SNOMED:HIGH');
    fireEvent.click(screen.getByRole('button', { name: /Seen/ }));
    await waitFor(() => expect(sourceGets().at(-1)![1].params).not.toHaveProperty('order'));
    expect(header.closest('th')).toHaveAttribute('aria-sort', 'none');
  });

  it('selects proposed sources above the confidence threshold, 80% by default', async () => {
    await openScored();
    const threshold = screen.getByRole('spinbutton', { name: 'Select sources with confidence above, percent' });
    expect(threshold).toHaveValue(80);
    fireEvent.click(screen.getByRole('button', { name: 'Select' }));
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED HIGH' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED MID' })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Approve selected (1)' })).toBeEnabled();
    fireEvent.change(threshold, { target: { value: '50' } });
    fireEvent.click(screen.getByRole('button', { name: 'Select' }));
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED MID' })).toBeChecked();
    // Unscored and approved rows are never picked by a threshold.
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED NONE' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED DONE' })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Approve selected (2)' })).toBeEnabled();
  });

  it('defaults the ranker to Jev', async () => {
    await openScored();
    expect(screen.getByRole('combobox', { name: 'Source suggestion ranker' })).toHaveValue('jev');
  });

  it('enables Select only when a selectable source is above the threshold', async () => {
    await openScored();
    const threshold = screen.getByRole('spinbutton', { name: 'Select sources with confidence above, percent' });
    expect(screen.getByRole('button', { name: 'Select' })).toBeEnabled();
    // DONE (90%) is approved, so it does not count; HIGH is 85%.
    fireEvent.change(threshold, { target: { value: '85' } });
    expect(screen.getByRole('button', { name: 'Select' })).toBeDisabled();
    fireEvent.change(threshold, { target: { value: '84' } });
    expect(screen.getByRole('button', { name: 'Select' })).toBeEnabled();
    fireEvent.change(threshold, { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Select' })).toBeDisabled();
  });

  it('compares the shown percentage, so a row at the threshold is not selected', async () => {
    await openScored();
    // 0.55 * 100 is 55.00000000000001 in floating point.
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Select sources with confidence above, percent' }),
      { target: { value: '55' } });
    fireEvent.click(screen.getByRole('button', { name: 'Select' }));
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED HIGH' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED MID' })).not.toBeChecked();
  });

  it('orders a suggestion preview by confidence without a request', async () => {
    post.mockResolvedValue({ data: { ...run(), activity: [{ concept: { concept_id: 123 }, candidates: [
      { ...source, mapping_id: 11, source_code: 'C-LOW', confidence: 0.25 },
      { ...source, mapping_id: 12, source_code: 'C-NONE', confidence: null },
      { ...source, mapping_id: 13, source_code: 'C-HIGH', confidence: 0.85 },
    ] }] } });
    await openScored();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('SNOMED:C-LOW');
    const before = sourceGets().length;
    fireEvent.click(screen.getByRole('button', { name: /Status \/ evidence · Confidence/ }));
    const codes = () => screen.getAllByText(/^SNOMED:C-/).map(node => node.textContent);
    expect(codes()).toEqual(['SNOMED:C-HIGH', 'SNOMED:C-LOW', 'SNOMED:C-NONE']);
    fireEvent.click(screen.getByRole('button', { name: /Status \/ evidence · Confidence/ }));
    expect(codes()).toEqual(['SNOMED:C-LOW', 'SNOMED:C-HIGH', 'SNOMED:C-NONE']);
    expect(sourceGets().length).toBe(before);
  });
});

describe('opening a linked concept', () => {
  beforeEach(() => {
    get.mockImplementation((url: string) => {
      if (url === '/v1/concept-to-code/') return Promise.resolve({ data: page([concept]) });
      if (url === '/v1/concept-to-code/999/') return Promise.reject({ response: { status: 404 } });
      return Promise.resolve({ data: { ...page([source]), concept } });
    });
  });

  it('opens the concept\'s source codes ready to suggest more, by id', async () => {
    const handled = vi.fn();
    render(<ConceptToCodeTab canWrite canApprove initialConceptId={123} onInitialConceptHandled={handled} />);
    const dialog = await screen.findByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(within(dialog).getByRole('button', { name: 'Suggested Codes' })).toHaveAttribute('aria-pressed', 'true');
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/');
    // Never the list's text search, which matches the digits in names and codes.
    expect(get).not.toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ search: '123' }) }));
    expect(handled).toHaveBeenCalledTimes(1);
  });

  it('explains a concept with no source-code view', async () => {
    const handled = vi.fn();
    render(<ConceptToCodeTab canWrite canApprove initialConceptId={999} onInitialConceptHandled={handled} />);
    expect(await screen.findByText(/Concept 999 is not a current standard concept/)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(handled).toHaveBeenCalledTimes(1);
  });

  it('opens a concept clicked in the list on Suggested Codes, with Mapped Codes a click away (#2010)', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(within(dialog).getByRole('button', { name: 'Suggested Codes' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(dialog).getByText(/Run Suggest source codes to search/)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.anything());
    await showExisting(dialog);
    expect(within(dialog).getByRole('button', { name: 'Mapped Codes' })).toHaveAttribute('aria-pressed', 'true');
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({
      params: expect.objectContaining({ mode: 'linked', status: 'proposed' }) })));
  });
});

describe('editing a source code from a concept (#1915)', () => {
  const mappingRow = {
    mapping_id: 7, domain_id: 'Measurement', source_vocabulary_id: 'SNOMED', source_code: 'S1',
    source_code_description: 'Albumin in blood', destination_concept_id: null, destination_concept_name: '',
    destination_concept_code: '', destination_vocabulary_id: '', destination_concept_class_id: '',
    destination_omop_table: 'measurement', status: 'proposed', notes: '', origin: '', origin_system: 'HT-One',
    suggest_strategy: '', umls_cui: '', created_by: '', occurrence_count: 8, destination_count: 0, has_mapping: true,
  };
  const examples = [
    { person_id: 11, omop_table: 'measurement', occurrence_count: 4, source_unit: 'g/dL', example_quantity: '4.1',
      source_metadata: { category: 'laboratory', specimen: 'serum' }, last_seen: null },
    { person_id: 12, omop_table: 'measurement', occurrence_count: 2, source_unit: '', example_quantity: '',
      source_metadata: { category: 'vital-signs', specimen: 'plasma' }, last_seen: null },
  ];
  const routes = (overrides: Record<string, () => Promise<unknown>> = {}) => (url: string) => {
    if (overrides[url]) return overrides[url]();
    if (url === '/v1/concept-to-code/') return Promise.resolve({ data: page([concept]) });
    if (url === '/v1/code-mappings/reference/') return Promise.resolve({ data: {} });
    if (url === '/v1/code-mappings/7/') return Promise.resolve({ data: { ...mappingRow, destination_options: [] } });
    if (url === '/v1/concept-to-code/source-examples/') return Promise.resolve({ data: { examples } });
    return Promise.resolve({ data: page([source]) });
  };

  beforeEach(() => {
    get.mockImplementation(routes());
    post.mockResolvedValue({ data: {} });
  });

  it('opens the code in Edit Mapping with the concept as destination, over the concept dialog', async () => {
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'SNOMED:S1' }));

    const editor = await screen.findByRole('dialog', { name: 'Edit Mapping' });
    expect(get).toHaveBeenCalledWith('/v1/code-mappings/7/');
    // The row had no destination: the concept being curated is offered.
    await waitFor(() => expect(within(editor).getByLabelText('Destination Concept ID')).toHaveValue(123));
    expect(screen.getByRole('dialog', { name: 'Source codes for Serum albumin' })).toBeInTheDocument();

    fireEvent.click(within(editor).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit Mapping' })).not.toBeInTheDocument());
    expect(screen.getByRole('dialog', { name: 'Source codes for Serum albumin' })).toBeInTheDocument();
  });

  it('opens a source code for editing while the Suggest run is still running (#2028)', async () => {
    post.mockResolvedValue({ data: run('running') });
    get.mockImplementation(routes({ '/v1/concept-to-code/suggest-runs/run-1/': () => Promise.resolve({ data: run('running') }) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    const code = await screen.findByRole('button', { name: 'SNOMED:S1' });
    // The run's own controls wait for it; the rows do not.
    expect(screen.getByRole('button', { name: 'Searching…' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED S1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Mapped Codes' })).toBeEnabled();
    expect(code).toBeEnabled();
    fireEvent.click(code);
    expect(await screen.findByRole('dialog', { name: 'Edit Mapping' })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/v1/code-mappings/7/');
  });

  it('opens from a click anywhere on the row, but not from its checkbox', async () => {
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    expect(screen.queryByRole('dialog', { name: 'Edit Mapping' })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith('/v1/code-mappings/7/');

    fireEvent.click(screen.getByText('Albumin in blood'));
    expect(await screen.findByRole('dialog', { name: 'Edit Mapping' })).toBeInTheDocument();
  });

  it('loads up to three more example patients with the More patients button', async () => {
    const mk = (id: number) => ({ person_id: id, omop_table: 'measurement', occurrence_count: 1, source_unit: '',
      example_quantity: '', source_metadata: { specimen: `s${id}` }, last_seen: null });
    get.mockImplementation(routes({ '/v1/concept-to-code/source-examples/': () => Promise.resolve({ data: { examples: [mk(11), mk(12), mk(13)] } }) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'SNOMED:S1' }));
    const editor = await screen.findByRole('dialog', { name: 'Edit Mapping' });
    const picker = await within(editor).findByRole('combobox', { name: 'Example patient' });
    expect(within(picker).getAllByRole('option')).toHaveLength(3);

    get.mockImplementation(routes({ '/v1/concept-to-code/source-examples/': () => Promise.resolve({ data: { examples: [mk(14)] } }) }));
    fireEvent.click(within(editor).getByRole('button', { name: 'More patients' }));

    await waitFor(() => expect(within(picker).getAllByRole('option')).toHaveLength(4));
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/source-examples/', {
      params: { source_vocabulary_id: 'SNOMED', source_code: 'S1', omop_table: 'measurement', exclude_person_ids: '11,12,13' },
    });
    expect(await within(editor).findByText(/"specimen": "s14"/)).toBeInTheDocument();
    // A short page means nothing is left.
    expect(within(editor).queryByRole('button', { name: 'More patients' })).not.toBeInTheDocument();
  });

  it('shows the selected example patient\'s Source Metadata and switches patients without writing', async () => {
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'SNOMED:S1' }));
    const editor = await screen.findByRole('dialog', { name: 'Edit Mapping' });

    const picker = await within(editor).findByRole('combobox', { name: 'Example patient' });
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/source-examples/', {
      params: { source_vocabulary_id: 'SNOMED', source_code: 'S1', omop_table: 'measurement' },
    });
    expect(within(picker).getAllByRole('option').map(option => option.textContent))
      .toEqual(['Patient 11 · Seen 4', 'Patient 12 · Seen 2']);
    expect(within(editor).getByText(/"specimen": "serum"/)).toBeInTheDocument();

    fireEvent.change(picker, { target: { value: '12' } });
    expect(await within(editor).findByText(/"specimen": "plasma"/)).toBeInTheDocument();
    expect(within(editor).queryByText(/"specimen": "serum"/)).not.toBeInTheDocument();
    // Viewing a patient never writes to that patient.
    expect([...post.mock.calls, ...patch.mock.calls].filter(([url]) => String(url).includes('source-codes'))).toEqual([]);
  });

  it('says so when no patient carries the code, and when examples are not allowed', async () => {
    get.mockImplementation(routes({ '/v1/concept-to-code/source-examples/': () => Promise.resolve({ data: { examples: [] } }) }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'SNOMED:S1' }));
    expect(await screen.findByTestId('source-examples-empty')).toBeInTheDocument();
  });

  it('explains a forbidden example lookup', async () => {
    get.mockImplementation(routes({
      '/v1/concept-to-code/source-examples/': () => Promise.reject({ response: { status: 403 } }),
    }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'SNOMED:S1' }));
    const editor = await screen.findByRole('dialog', { name: 'Edit Mapping' });
    expect(await within(editor).findByText('Patient Source Metadata is limited to administrators.')).toBeInTheDocument();
  });

  it('opens a suggested code with no mapping row as a New Mapping for this concept', async () => {
    post.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/suggest/'
      ? { ...run(), activity: [{ concept: { concept_id: 123 }, candidates: [
        { ...source, mapping_id: null, source_code: 'NEW1', status: 'unmapped', confidence: 0.9 }] }] }
      : {} }));
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    fireEvent.click(await screen.findByRole('button', { name: 'SNOMED:NEW1' }));

    const editor = await screen.findByRole('dialog', { name: 'New Mapping' });
    expect(get).not.toHaveBeenCalledWith('/v1/code-mappings/null/');
    await waitFor(() => expect(within(editor).getByLabelText('Destination Concept ID')).toHaveValue(123));
  });
});

describe('suggestions without a confidence score (#1915)', () => {
  const candidates = [
    { ...source, source_code: 'SCORED', confidence: 0.72 },
    { ...source, source_code: 'UNSCORED', confidence: null, declined: true },
  ];
  const suggest = (ranking_model: string) => {
    post.mockResolvedValue({ data: { ...run(), selection: { limit: 25, include_zero_seen: false, ranking_model },
      activity: [{ concept: { concept_id: 123 }, candidates }] } });
  };

  it('hides unscored suggestions after a ranked run', async () => {
    suggest('jev');
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    expect(await screen.findByText('SNOMED:SCORED')).toBeInTheDocument();
    expect(screen.queryByText('SNOMED:UNSCORED')).not.toBeInTheDocument();
    expect(screen.getByText(/1 declined by the ranker and hidden; the next run moves on to other codes/)).toBeInTheDocument();
  });

  it('shows every suggestion after an unranked run', async () => {
    suggest('none');
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    expect(await screen.findByText('SNOMED:SCORED')).toBeInTheDocument();
    expect(screen.getByText('SNOMED:UNSCORED')).toBeInTheDocument();
    expect(screen.queryByText(/declined by the ranker and hidden/)).not.toBeInTheDocument();
  });

  it('leaves existing mappings visible whether or not they were scored', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept]) : page(candidates) }));
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    expect(await screen.findByText('SNOMED:SCORED')).toBeInTheDocument();
    expect(screen.getByText('SNOMED:UNSCORED')).toBeInTheDocument();
  });
});

describe('concept priority (#1914)', () => {
  const named = (id: number, name: string, priority: number | null = null) => ({ ...concept, concept_id: id, concept_name: name, priority });
  let list: ReturnType<typeof named>[];
  beforeEach(() => {
    list = [named(1, 'Albumin', 1), named(2, 'Bilirubin', 2), named(3, 'Calcium'), named(4, 'Creatinine')];
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? { ...page(list), prioritized: 2 } : page([source]) }));
    post.mockImplementation((_url: string, body: { position: number | null }) => Promise.resolve({ data: { position: body.position } }));
  });
  const moved = () => post.mock.calls.filter(([url]) => url === '/v1/concept-to-code/priority/').map(([, body]) => body);

  it('shows each concept\'s priority and ranks one by typing its number', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const calcium = await screen.findByLabelText('Priority for Calcium');
    expect(screen.getByLabelText('Priority for Albumin')).toHaveValue(1);
    expect(calcium).toHaveValue(null);

    fireEvent.change(calcium, { target: { value: '1' } });
    fireEvent.keyDown(calcium, { key: 'Enter' });

    await waitFor(() => expect(moved()).toEqual([{ concept_id: 3, position: 1 }]));
    expect(await screen.findByText('Moved Calcium to priority 1.')).toBeInTheDocument();
    // The list is reloaded in the server's new order.
    await waitFor(() => expect(get.mock.calls.filter(([url]) => url === '/v1/concept-to-code/').length).toBeGreaterThan(1));
  });

  it('unranks a concept when its number is cleared, and ignores an invalid number', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const albumin = await screen.findByLabelText('Priority for Albumin');
    fireEvent.change(albumin, { target: { value: '0' } });
    fireEvent.blur(albumin);
    expect(moved()).toEqual([]);
    expect(albumin).toHaveValue(1);

    fireEvent.change(albumin, { target: { value: '' } });
    fireEvent.blur(albumin);
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: null }]));
    expect(await screen.findByText('Removed Albumin from the priority list.')).toBeInTheDocument();
  });

  it('moves one step with the arrow buttons, and an unranked concept up to the end of the list', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    expect(await screen.findByRole('button', { name: 'Move Albumin up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Bilirubin down' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Calcium down' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Move Albumin down' }));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 2 }]));
    fireEvent.click(await screen.findByRole('button', { name: 'Move Calcium up' }));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 2 }, { concept_id: 3, position: 3 }]));
  });

  it('drops a dragged concept at the position of the ranked concept it lands on', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const creatinine = await screen.findByTestId('concept-row-4');
    const albumin = screen.getByTestId('concept-row-1');
    fireEvent.dragStart(creatinine);
    fireEvent.dragOver(albumin);
    fireEvent.drop(albumin);
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 4, position: 1 }]));
  });

  it('drops onto an unranked concept at the end of the priority list', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    const albumin = await screen.findByTestId('concept-row-1');
    fireEvent.dragStart(albumin);
    fireEvent.drop(screen.getByTestId('concept-row-3'));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 3 }]));
  });

  it('shows more concepts per page and only ranked ones on request', async () => {
    render(<ConceptToCodeTab canWrite canApprove />);
    await screen.findByLabelText('Priority for Albumin');
    fireEvent.change(screen.getByRole('combobox', { name: 'Concepts per page' }), { target: { value: '250' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ page_size: 250, page: 1 }) })));
    fireEvent.click(screen.getByRole('checkbox', { name: /Ranked only/ }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ prioritized: '1' }) })));
  });

  it('marks a concept Done from the Priority cell, which then reads Done in italics and offers Not done (#2388)', async () => {
    list = [named(1, 'Albumin', 1), { ...named(2, 'Bilirubin'), done: true }];
    render(<ConceptToCodeTab canWrite canApprove />);
    // A click on the cell around the number opens the menu; the number input keeps its own behaviour.
    const albuminCell = (await screen.findByLabelText('Priority for Albumin')).parentElement!;
    fireEvent.click(albuminCell);
    fireEvent.click(within(screen.getByRole('menu', { name: 'Options for Albumin' })).getByRole('menuitem', { name: 'Done' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/done/', { concept_id: 1, done: true }));
    expect(await screen.findByText('Marked Albumin as Done.')).toBeInTheDocument();

    // A Done concept: no number, no arrows, "Done" in italics, the row in italics.
    expect(screen.queryByLabelText('Priority for Bilirubin')).toBeNull();
    const done = screen.getByRole('button', { name: 'Bilirubin is Done; options' });
    expect(done).toHaveTextContent('Done');
    expect(done.className).toContain('italic');
    expect(screen.getByTestId('concept-row-2').className).toContain('italic');
    fireEvent.click(done);
    const notDone = screen.getByRole('menuitem', { name: 'Not done' });
    expect(notDone).toHaveFocus();
    // Escape closes the menu without acting; a Done row cannot be dragged.
    fireEvent.keyDown(notDone, { key: 'Escape' });
    expect(screen.queryByRole('menuitem', { name: 'Not done' })).toBeNull();
    expect(screen.getByTestId('concept-row-2')).toHaveAttribute('draggable', 'false');
    fireEvent.click(done);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Not done' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/done/', { concept_id: 2, done: false }));
  });

  it('locks a concept from its row, shows the holder on other rows and keeps a locked concept review-only (#2390)', async () => {
    list = [named(1, 'Albumin', 1), { ...named(2, 'Bilirubin'), lock: { username: 'lisa@healthtree.org', mine: false } },
            { ...named(3, 'Calcium'), lock: { username: 'me@healthtree.org', mine: true } }];
    post.mockImplementation((url: string) => Promise.resolve({ data: url.endsWith('/lock/')
      ? { concept_id: 1, lock: { username: 'me@healthtree.org', mine: true } } : page([source]) }));
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Lock Albumin' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/1/lock/'));
    expect(await screen.findByText('Locked Albumin for you.')).toBeInTheDocument();
    // Another curator's lock is a badge with their name; the holder sees Unlock.
    expect(screen.getByLabelText('Locked by lisa@healthtree.org')).toHaveTextContent('lisa@healthtree.org');
    expect(screen.getByRole('button', { name: 'Unlock Calcium' })).toHaveTextContent('me@healthtree.org');
    // Opening a locked concept is review only: the notice shows and the write controls are off.
    fireEvent.click(screen.getByRole('button', { name: 'Bilirubin' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('status')).toHaveTextContent('Locked by lisa@healthtree.org');
    await showExisting(dialog);
    await screen.findByText('SNOMED:S1');
    expect(within(dialog).getByRole('button', { name: /Approve selected/ })).toBeDisabled();
  });

  it('shows a note icon before the lock, and writes, clears and fails to save a note in its popup (#2411)', async () => {
    list = [named(1, 'Albumin', 1), { ...named(2, 'Bilirubin'), note: { text: 'Ask Lisa first', updated_by: 'lisa@healthtree.org' },
                                      lock: { username: 'lisa@healthtree.org', mine: false } }];
    put.mockReset();
    render(<ConceptToCodeTab canWrite canApprove />);
    const add = await screen.findByRole('button', { name: 'Add a note for Albumin' });
    expect(add).toHaveAttribute('data-has-note', 'false');
    // The icon sits between the concept's name and its lock.
    expect(add.compareDocumentPosition(screen.getByRole('button', { name: 'Lock Albumin' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Albumin' }).compareDocumentPosition(add) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // A written note fills the icon and shows on hover, even on a concept someone else holds.
    const existing = screen.getByRole('button', { name: 'Edit the note for Bilirubin' });
    expect(existing).toHaveAttribute('data-has-note', 'true');
    expect(existing).toHaveAttribute('title', 'Ask Lisa first');

    // The popup opens with focus in the textarea; Escape closes it without saving.
    const user = userEvent.setup();
    await user.click(add);
    const box = await screen.findByLabelText('Note for Albumin', { selector: 'textarea' });
    await waitFor(() => expect(box).toHaveFocus());
    await user.type(box, 'Draft');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Note for Albumin' })).toBeNull());
    expect(put).not.toHaveBeenCalled();

    // A failed save says so and keeps the popup and the text.
    put.mockRejectedValueOnce({ response: { status: 403, data: { detail: 'Professional access required.' } } });
    await user.click(add);
    const again = await screen.findByLabelText('Note for Albumin', { selector: 'textarea' });
    expect(again).toHaveValue('');
    // Nothing to save yet: the button is off and the shortcut does nothing.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    await user.keyboard('{Control>}{Enter}{/Control}');
    expect(put).not.toHaveBeenCalled();
    await user.type(again, 'Parked until the LOINC reload');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save the note. Professional access required.');
    expect(again).toHaveValue('Parked until the LOINC reload');

    // Saving closes the popup and fills the icon.
    list = [{ ...list[0], note: { text: 'Parked until the LOINC reload', updated_by: 'me@healthtree.org' } }, list[1]];
    put.mockResolvedValueOnce({ data: { concept_id: 1, note: { text: 'Parked until the LOINC reload', updated_by: 'me@healthtree.org' } } });
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(put).toHaveBeenLastCalledWith('/v1/concept-to-code/1/note/', { note: 'Parked until the LOINC reload' }));
    const edit = await screen.findByRole('button', { name: 'Edit the note for Albumin' });
    expect(edit).toHaveAttribute('title', 'Parked until the LOINC reload');
    expect(screen.getByText('Saved the note for Albumin.')).toBeInTheDocument();

    // A drag inside the popup is not a drag of the row: it never reaches the priority list.
    await user.click(edit);
    const open = await screen.findByLabelText('Note for Albumin', { selector: 'textarea' });
    expect(screen.getByText('Last saved by me@healthtree.org')).toBeInTheDocument();
    fireEvent.dragStart(open);
    fireEvent.drop(screen.getByTestId('concept-row-2'));
    expect(moved()).toEqual([]);
    await user.keyboard('{Escape}');

    // Emptying the note clears it.
    list = [{ ...list[0], note: null }, list[1]];
    put.mockResolvedValueOnce({ data: { concept_id: 1, note: null } });
    await user.click(edit);
    await user.clear(await screen.findByLabelText('Note for Albumin', { selector: 'textarea' }));
    await user.click(screen.getByRole('button', { name: 'Clear note' }));
    await waitFor(() => expect(put).toHaveBeenLastCalledWith('/v1/concept-to-code/1/note/', { note: '' }));
    expect(await screen.findByRole('button', { name: 'Add a note for Albumin' })).toHaveAttribute('data-has-note', 'false');
  });

  it('reports a failed move and keeps the list as it was', async () => {
    post.mockRejectedValue({ response: { status: 403, data: { detail: 'Professional access required.' } } });
    render(<ConceptToCodeTab canWrite canApprove />);
    const calcium = await screen.findByLabelText('Priority for Calcium');
    fireEvent.change(calcium, { target: { value: '1' } });
    fireEvent.blur(calcium);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not change the priority of Calcium.');
  });

  it('estimates below Suggest how many source codes it could find and how many runs that takes (#1953)', async () => {
    const estimates: Record<string, unknown>[] = [];
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === '/v1/concept-to-code/') return Promise.resolve({ data: page([concept]) });
      if (url.endsWith('/estimate/')) {
        estimates.push(config?.params ?? {});
        return Promise.resolve({ data: config?.params?.strategies === 'umls'
          ? { total: 0, seen: 0, umls: 0, lexical: 0, capped: false, timed_out: false }
          : { total: 60, seen: 58, umls: 4, lexical: 57, capped: false, timed_out: false } });
      }
      return Promise.resolve({ data: page([source]) });
    });
    await openSuggested();
    const estimate = await screen.findByTestId('source-estimate');
    expect(estimate).toHaveTextContent('About 60 candidate source codes (57 by name, 4 by UMLS) · about 3 runs of 25.');
    expect(estimates[0]).toEqual({ strategies: 'umls,lexical', include_zero_seen: '0', include_declined: '0' });
    // The run count follows the limit as it is typed.
    fireEvent.change(screen.getByLabelText('Maximum source candidates'), { target: { value: '60' } });
    expect(estimate).toHaveTextContent('about 1 run of 60.');
    // And the estimate follows the search settings.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Names and synonyms' }));
    await waitFor(() => expect(screen.getByTestId('source-estimate'))
      .toHaveTextContent('Suggest source codes would find no candidate source codes with these settings.'));
    expect(estimates.at(-1)).toEqual({ strategies: 'umls', include_zero_seen: '0', include_declined: '0' });
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    await waitFor(() => expect(estimates.at(-1)).toEqual({ strategies: 'umls', include_zero_seen: '1', include_declined: '0' }));
  });

  it('marks a capped estimate as a floor and says when the estimate timed out', async () => {
    let response: Record<string, unknown> = { total: 1000, seen: 1000, umls: 0, lexical: 1000, capped: true, timed_out: false };
    get.mockImplementation((url: string) => url === '/v1/concept-to-code/' ? Promise.resolve({ data: page([concept]) })
      : url.endsWith('/estimate/') ? Promise.resolve({ data: response }) : Promise.resolve({ data: page([source]) }));
    await openSuggested();
    expect(await screen.findByTestId('source-estimate'))
      .toHaveTextContent('About 1,000+ candidate source codes (1,000+ by name) · about 40+ runs of 25.');
    response = { total: 12, umls: 0, lexical: 12, capped: true, partial: true, timed_out: false };
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    await waitFor(() => expect(screen.getByTestId('source-estimate'))
      .toHaveTextContent('About 12+ candidate source codes (12+ by name) · about 1+ runs of 25. Vocabulary codes never seen were not all counted in time.'));
    response = { total: null, timed_out: true };
    fireEvent.click(screen.getByRole('checkbox', { name: 'UMLS' }));
    await waitFor(() => expect(screen.getByTestId('source-estimate')).toHaveTextContent(/search took too long/));
  });

  it('labels an earlier or sampled estimate as approximate', async () => {
    let response: Record<string, unknown> = { total: 6767, umls: 0, lexical: 6767, capped: false, partial: false, approximate: 'earlier', timed_out: false };
    get.mockImplementation((url: string) => url === '/v1/concept-to-code/' ? Promise.resolve({ data: page([concept]) })
      : url.endsWith('/estimate/') ? Promise.resolve({ data: response }) : Promise.resolve({ data: page([source]) }));
    await openSuggested();
    expect(await screen.findByTestId('source-estimate')).toHaveTextContent(
      'About 6,767 candidate source codes (6,767 by name) · about 271 runs of 25. From an earlier count: this one took too long, so codes handled since may still be counted.');
    response = { total: 6400, umls: 0, lexical: 6400, capped: false, partial: true, approximate: 'sample', timed_out: false };
    fireEvent.click(screen.getByRole('checkbox', { name: SEEN_FILTER }));
    await waitFor(() => expect(screen.getByTestId('source-estimate')).toHaveTextContent(
      'Roughly 6,400 candidate source codes (6,400 by name) · about 256 runs of 25. Estimated from a sample: the full count took too long. Vocabulary codes never seen were not counted.'));
    response = { total: 0, umls: 0, lexical: 0, capped: false, partial: false, approximate: 'sample', timed_out: false };
    fireEvent.click(screen.getByRole('checkbox', { name: 'UMLS' }));
    await waitFor(() => expect(screen.getByTestId('source-estimate'))
      .toHaveTextContent('A sample found no candidate source codes; the full count took too long, so there may be a few.'));
  });

  it('has no Refresh button; a failed load offers Retry', async () => {
    let fail = true;
    get.mockImplementation((url: string) => url === '/v1/concept-to-code/' ? Promise.resolve({ data: page([concept]) })
      : url.endsWith('/estimate/') ? Promise.resolve({ data: { total: 1, timed_out: false } })
        : fail ? Promise.reject(new Error('Unavailable')) : Promise.resolve({ data: page([source]) }));
    render(<ConceptToCodeTab canWrite canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    await showExisting();
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load source codes.');
    expect(screen.queryByRole('button', { name: 'Refresh' })).not.toBeInTheDocument();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('SNOMED:S1')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
