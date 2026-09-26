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
});
