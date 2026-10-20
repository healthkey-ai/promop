import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ConceptToCodeTab from './ConceptToCodeTab';

const get = vi.fn();
const post = vi.fn();
const remove = vi.fn(() => Promise.resolve({ data: {} }));
const patch = vi.fn(() => Promise.resolve({ data: {} }));
vi.mock('@/api/axios', () => ({ default: {
  get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args),
  delete: (...args: unknown[]) => remove(...args), patch: (...args: unknown[]) => patch(...args),
} }));
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
      domain: 'drug_exposure', scope: 'all', search: 'codeine', page: 1, page_size: 50,
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
      concept_ids: [123], strategies: ['umls', 'lexical'], limit: 25, include_zero_seen: false, ranking_model: 'jev',
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

  it('opens the concept\'s source codes ready to find more, by id', async () => {
    const handled = vi.fn();
    render(<ConceptToCodeTab canApprove initialConceptId={123} onInitialConceptHandled={handled} />);
    const dialog = await screen.findByRole('dialog', { name: 'Source codes for Serum albumin' });
    expect(within(dialog).getByRole('button', { name: 'Find source codes' })).toHaveAttribute('aria-pressed', 'true');
    expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/');
    // Never the list's text search, which matches the digits in names and codes.
    expect(get).not.toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ search: '123' }) }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/123/', expect.objectContaining({
      params: expect.objectContaining({ mode: 'available' }),
    })));
    expect(handled).toHaveBeenCalledTimes(1);
  });

  it('explains a concept with no source-code view', async () => {
    const handled = vi.fn();
    render(<ConceptToCodeTab canApprove initialConceptId={999} onInitialConceptHandled={handled} />);
    expect(await screen.findByText(/Concept 999 is not a current standard concept/)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(handled).toHaveBeenCalledTimes(1);
  });

  it('opens a concept clicked in the list on its existing mappings', async () => {
    await selectConcept();
    expect(screen.getByRole('button', { name: 'Existing mappings' })).toHaveAttribute('aria-pressed', 'true');
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

  it('opens from a click anywhere on the row, but not from its checkbox', async () => {
    await selectConcept();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select SNOMED S1' }));
    expect(screen.queryByRole('dialog', { name: 'Edit Mapping' })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith('/v1/code-mappings/7/');

    fireEvent.click(screen.getByText('Albumin in blood'));
    expect(await screen.findByRole('dialog', { name: 'Edit Mapping' })).toBeInTheDocument();
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
    { ...source, source_code: 'UNSCORED', confidence: null },
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
    expect(screen.getByText(/1 without a confidence score hidden/)).toBeInTheDocument();
  });

  it('shows every suggestion after an unranked run', async () => {
    suggest('none');
    await selectConcept();
    fireEvent.click(screen.getByRole('button', { name: 'Suggest source codes' }));
    expect(await screen.findByText('SNOMED:SCORED')).toBeInTheDocument();
    expect(screen.getByText('SNOMED:UNSCORED')).toBeInTheDocument();
    expect(screen.queryByText(/without a confidence score hidden/)).not.toBeInTheDocument();
  });

  it('leaves existing mappings visible whether or not they were scored', async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/concept-to-code/'
      ? page([concept]) : page(candidates) }));
    render(<ConceptToCodeTab canApprove />);
    fireEvent.click(await screen.findByRole('button', { name: 'Serum albumin' }));
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
    render(<ConceptToCodeTab canApprove />);
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
    render(<ConceptToCodeTab canApprove />);
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
    render(<ConceptToCodeTab canApprove />);
    expect(await screen.findByRole('button', { name: 'Move Albumin up' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Bilirubin down' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move Calcium down' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Move Albumin down' }));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 2 }]));
    fireEvent.click(await screen.findByRole('button', { name: 'Move Calcium up' }));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 2 }, { concept_id: 3, position: 3 }]));
  });

  it('drops a dragged concept at the position of the ranked concept it lands on', async () => {
    render(<ConceptToCodeTab canApprove />);
    const creatinine = await screen.findByTestId('concept-row-4');
    const albumin = screen.getByTestId('concept-row-1');
    fireEvent.dragStart(creatinine);
    fireEvent.dragOver(albumin);
    fireEvent.drop(albumin);
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 4, position: 1 }]));
  });

  it('drops onto an unranked concept at the end of the priority list', async () => {
    render(<ConceptToCodeTab canApprove />);
    const albumin = await screen.findByTestId('concept-row-1');
    fireEvent.dragStart(albumin);
    fireEvent.drop(screen.getByTestId('concept-row-3'));
    await waitFor(() => expect(moved()).toEqual([{ concept_id: 1, position: 3 }]));
  });

  it('shows more concepts per page and only ranked ones on request', async () => {
    render(<ConceptToCodeTab canApprove />);
    await screen.findByLabelText('Priority for Albumin');
    fireEvent.change(screen.getByRole('combobox', { name: 'Concepts per page' }), { target: { value: '250' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ page_size: 250, page: 1 }) })));
    fireEvent.click(screen.getByRole('checkbox', { name: /Ranked only/ }));
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concept-to-code/', expect.objectContaining({
      params: expect.objectContaining({ prioritized: '1' }) })));
  });

  it('reports a failed move and keeps the list as it was', async () => {
    post.mockRejectedValue({ response: { status: 403, data: { detail: 'Professional access required.' } } });
    render(<ConceptToCodeTab canApprove />);
    const calcium = await screen.findByLabelText('Priority for Calcium');
    fireEvent.change(calcium, { target: { value: '1' } });
    fireEvent.blur(calcium);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not change the priority of Calcium.');
  });
});
