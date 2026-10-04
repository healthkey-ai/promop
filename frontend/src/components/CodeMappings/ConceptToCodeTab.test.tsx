import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ConceptToCodeTab from './ConceptToCodeTab';

const get = vi.fn();
const post = vi.fn();
vi.mock('@/api/axios', () => ({ default: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) } }));
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
  run_id: 'run-1', state, error: '', total: 1, done: state === 'success' ? 1 : 0,
  selection: { limit: 25, include_zero_seen: false },
  activity: [{ concept: { concept_id: 123 }, candidates: [{ ...source, evidence: ['umls'], verdict: 'review' }] }],
});

beforeEach(() => {
  get.mockReset(); post.mockReset();
  get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/' ? page([concept]) : page([source]) }));
  post.mockResolvedValue({ data: { ...source, destination_concept_id: 123, status: 'approved' } });
});

async function selectConcept(canApprove = true) {
  render(<ConceptToCodeTab canApprove={canApprove} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
  await screen.findByText('SNOMED:S1');
}

describe('concept-first curation', () => {
  it('opens a dialog immediately from a long concept list and shows source loading errors inside it', async () => {
    const concepts = Array.from({ length: 50 }, (_, index) => ({
      ...concept, concept_id: 123 + index, concept_name: index ? `Concept ${index}` : concept.concept_name,
    }));
    get.mockImplementation((url: string) => url === '/v1/concept-to-code/'
      ? Promise.resolve({ data: page(concepts) }) : Promise.reject(new Error('Unavailable')));
    render(<ConceptToCodeTab canApprove />);
    const trigger = await screen.findByRole('button', { name: 'Serum albumin' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText('Standard LOINC:A1 · OMOP 123 · Measurement')).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Standard concept coverage' })).not.toBeInTheDocument();
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Could not load source codes.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(screen.getByRole('table', { name: 'Standard concept coverage' })).toBeInTheDocument();
  });

  it('supports keyboard opening, traps focus, and returns focus on Escape', async () => {
    const user = userEvent.setup();
    render(<ConceptToCodeTab canApprove />);
    const trigger = await screen.findByRole('button', { name: 'Serum albumin' });
    trigger.focus();
    await user.keyboard('{Enter}');
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Serum albumin' });
    const close = within(dialog).getByRole('button', { name: 'Close' });
    expect(close).toHaveFocus();
    await within(dialog).findByText('SNOMED:S1');
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
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only source codes with Seen greater than zero' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Total protein' }));
    const dialog = screen.getByRole('dialog', { name: 'Source codes for Total protein' });
    await within(dialog).findByText('SNOMED:S2');
    expect(within(dialog).queryByText('SNOMED:S1')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('checkbox', { name: 'Only source codes with Seen greater than zero' })).toBeChecked();
    expect(within(dialog).getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/456/', expect.objectContaining({ params: expect.objectContaining({ mode: 'linked', seen_only: '1', page: 1 }) }));
  });

  it('prevents dismissal during a batch save and allows closing when the whole batch completes', async () => {
    const onWritingChange = vi.fn();
    let finishFirst!: (value: unknown) => void;
    post.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }));
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept]) : page([source, { ...source, mapping_id: 8, source_code: 'S2' }]) }));
    render(<ConceptToCodeTab canApprove onWritingChange={onWritingChange} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
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
    render(<ConceptToCodeTab canApprove />);
    const table = await screen.findByRole('table', { name: 'Standard concept coverage' });
    await screen.findByRole('button', { name: 'Serum albumin' });
    expect(within(table).getByText('10')).toBeInTheDocument();
    expect(within(table).getByText('albumin')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Concept scope'), { target: { value: 'all' } });
    fireEvent.change(screen.getByLabelText('Concept domain'), { target: { value: 'drug_exposure' } });
    fireEvent.change(screen.getByLabelText('Search standard concepts'), { target: { value: 'codeine' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({ params: {
      domain: 'drug_exposure', scope: 'all', search: 'codeine', page: 1,
    } })));
  });

  it('defaults source rows to Seen > 0 and allows zero-Seen codes', async () => {
    await selectConcept();
    const checkbox = screen.getByRole('checkbox', { name: 'Only source codes with Seen greater than zero' });
    expect(checkbox).toBeChecked();
    expect(checkbox.closest('th')).toHaveAttribute('aria-sort', 'descending');
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({ params: expect.objectContaining({ seen_only: '1' }) }));
    fireEvent.click(checkbox);
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({ params: expect.objectContaining({ seen_only: '0', page: 1 }) })));
  });

  it('finds new source codes and approves only reviewed selections with revision guards', async () => {
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Find source codes' }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({ params: expect.objectContaining({ mode: 'available' }) })));
    await screen.findByText('SNOMED:S1');
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/123/mappings/7/', {
      status: 'approved', expected_updated_at: source.updated_at,
    });
  });

  it('confirms unit evidence before retrying concept-first approval', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    post
      .mockRejectedValueOnce({ response: { data: {
        code: 'unit_mismatch_confirmation_required',
        unit_consistency: {
          source_code: 'S1', destination_concept_name: 'Serum albumin', property: 'MCnc',
          expected_units: ['g/dL'],
          observed_units: [{ unit: 'mmol/L', count: 8, compatible: false }],
        },
      } } })
      .mockResolvedValueOnce({ data: { ...source, destination_concept_id: 123, status: 'approved' } });
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));

    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('mmol/L (8 results)'));
    expect(post).toHaveBeenLastCalledWith('/v1/concept-to-code/123/mappings/7/', {
      status: 'approved', expected_updated_at: source.updated_at,
      confirm_unit_mismatch: true,
    });
    confirm.mockRestore();
  });

  it('allows proposals but hides approval for non-admin curators', async () => {
    await selectConcept(false);
    expect(screen.queryByRole('button', { name: /Approve selected/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Propose selected (1)' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/concept-to-code/123/mappings/7/', expect.objectContaining({ status: 'proposed' })));
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
      concept_ids: [123], strategies: ['umls', 'lexical'], limit: 25, include_zero_seen: false, ranking_model: 'none',
    });
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
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

  it('keeps new vocabulary candidates distinct and requires proposal before approval', async () => {
    const fresh = { ...source, mapping_id: null, status: 'unmapped', occurrence_count: 0,
      source_code: 'NEW1', origin_system: 'vocabulary', destination_concept_id: null, destination_concept_name: '' };
    const second = { ...fresh, source_code: 'NEW2' };
    const preview = { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [fresh, second] }] };
    post.mockResolvedValueOnce({ data: preview });
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only source codes with Seen greater than zero' }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('2 candidates retrieved (limit 25).');
    expect(post).toHaveBeenCalledWith('/v1/concept-to-code/suggest/', expect.objectContaining({ include_zero_seen: true }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED NEW1' }));
    expect(screen.getByRole('checkbox', { name: 'Select SNOMED NEW2' })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Approve selected (0)' })).toBeDisabled();
    const proposed = { ...fresh, mapping_id: 17, status: 'proposed', updated_at: 'new-revision', destination_concept_id: 123 };
    post.mockResolvedValueOnce({ data: proposed });
    fireEvent.click(screen.getByRole('button', { name: 'Propose selected (1)' }));
    await screen.findByText('Proposed 1 of 1 selected mappings.');
    expect(post).toHaveBeenLastCalledWith('/v1/concept-to-code/123/mappings/', {
      run_id: 'run-1', source_vocabulary_id: 'SNOMED', source_code: 'NEW1', status: 'proposed',
    });
    expect(screen.getAllByText('Not yet mapped')).toHaveLength(1);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select displayed proposed sources' }));
    expect(screen.getByRole('button', { name: 'Propose selected (2)' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Approve selected (1)' })).toBeEnabled();
    post.mockResolvedValueOnce({ data: { ...proposed, status: 'approved' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve selected (1)' }));
    await screen.findByText('Approved 1 of 1 selected mappings.');
    expect(post).toHaveBeenLastCalledWith('/v1/concept-to-code/123/mappings/17/', {
      status: 'approved', expected_updated_at: 'new-revision',
    });
    expect(post).toHaveBeenCalledTimes(3);
    expect(screen.getByText('Not yet mapped')).toBeInTheDocument();
  });

  it('retains a new vocabulary candidate when its preview is stale', async () => {
    post.mockResolvedValueOnce({ data: { ...run(), selection: { limit: 25, include_zero_seen: true },
      activity: [{ concept: { concept_id: 123 }, candidates: [{ ...source, mapping_id: null, status: 'unmapped', occurrence_count: 0 }] }] } });
    await selectConcept(false);
    fireEvent.click(screen.getByRole('checkbox', { name: 'Only source codes with Seen greater than zero' }));
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    await screen.findByText('Not yet mapped');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    post.mockRejectedValueOnce({ response: { status: 409, data: { detail: 'The source vocabulary changed after the preview.' } } });
    fireEvent.click(screen.getByRole('button', { name: 'Propose selected (1)' }));
    await screen.findByText('Proposed 0 of 1 selected mappings.');
    expect(screen.getByRole('alert')).toHaveTextContent('The source vocabulary changed after the preview.');
    expect(screen.getByText('Not yet mapped')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Approve selected/ })).not.toBeInTheDocument();
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
    render(<ConceptToCodeTab canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
    return screen.findByText('SNOMED:HIGH');
  }

  it('shows the confidence of every proposed mapping in the last column', async () => {
    await openScored();
    const cell = (code: string) => screen.getByText(`SNOMED:${code}`).closest('tr')!.lastElementChild as HTMLElement;
    expect(cell('HIGH')).toHaveTextContent('Confidence: 85%');
    expect(cell('MID')).toHaveTextContent('Confidence: 55%');
    expect(cell('NONE')).toHaveTextContent('Confidence: not scored');
    expect(cell('DONE')).not.toHaveTextContent('Confidence');
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
  it('opens the concept\'s source codes ready to find more', async () => {
    render(<ConceptToCodeTab canApprove initialConceptId={123} />);
    const dialog = await screen.findByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(within(dialog).getByRole('button', { name: 'Find source codes' })).toHaveAttribute('aria-pressed', 'true');
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', { params: { scope: 'all', search: '123' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({
      params: expect.objectContaining({ mode: 'available' }),
    })));
  });

  it('explains a concept with no source-code view', async () => {
    render(<ConceptToCodeTab canApprove initialConceptId={999} />);
    expect(await screen.findByText(/Concept 999 is not a current standard concept/)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens a concept clicked in the list on its existing mappings', async () => {
    await selectConcept();
    expect(screen.getByRole('button', { name: 'Existing mappings' })).toHaveAttribute('aria-pressed', 'true');
  });
});
