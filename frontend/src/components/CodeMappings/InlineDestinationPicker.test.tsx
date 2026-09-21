import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import InlineDestinationPicker from './InlineDestinationPicker';

const { get, post, patch, remove } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), remove: vi.fn() }));
vi.mock('@/api/axios', () => ({ default: { get, post, patch, delete: remove } }));
const concept = { concept_id: 42, concept_name: 'Protein.monoclonal', concept_code: '33358-3', vocabulary_id: 'LOINC', domain_id: 'Measurement', concept_class_id: 'Lab Test', standard_concept: 'S' };
const mapping = { mapping_id: 7, destination_concept_id: null, status: 'proposed', mapping_origin: 'healthkey' };
const onSaved = vi.fn();
const onCancel = vi.fn();
const show = (canApprove = false) => render(<InlineDestinationPicker mappingId={7} sourceLabel="Local:M-PROTEIN" canApprove={canApprove}
  vocabularies={[{ vocabulary_id: 'LOINC', vocabulary_name: 'LOINC' }]} onSaved={onSaved} onCancel={onCancel} />);
const searchAndChoose = async () => {
  fireEvent.change(screen.getByRole('combobox', { name: 'Search destination concepts inline' }), { target: { value: 'monoclonal' } });
  fireEvent.click(await screen.findByRole('option', { name: /Protein.monoclonal/ }));
};

beforeEach(() => {
  vi.resetAllMocks();
  get.mockImplementation((url: string) => Promise.resolve({ data: url.includes('/search/') ? { results: [concept] } : mapping }));
  post.mockResolvedValue({ data: {} });
  remove.mockResolvedValue({ data: {} });
  patch.mockResolvedValue({ data: { ...mapping, destination_concept_id: 42 } });
});

describe('inline destination picking', () => {
  it('debounces search, uses standard defaults, and saves only destination fields under a lock', async () => {
    show();
    fireEvent.change(screen.getByRole('combobox', { name: 'Inline search vocabulary' }), { target: { value: 'LOINC' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Search destination concepts inline' }), { target: { value: 'mon' } });
    await searchAndChoose();
    expect(get.mock.calls.filter(([url]) => url.includes('/search/'))).toHaveLength(1);
    expect(get).toHaveBeenCalledWith('/v1/concepts/search/', { params: { q: 'monoclonal', limit: '25', vocabulary_id: 'LOINC' }, signal: expect.any(AbortSignal) });
    expect(screen.queryByRole('button', { name: 'Save & approve' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save choice' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith('/v1/code-mappings/7/', { destination_concept_id: 42, status: 'proposed' });
    expect(post.mock.calls).toEqual([['/v1/code-mappings/7/lock/']]);
    expect(remove).toHaveBeenCalledWith('/v1/code-mappings/7/lock/');
    expect(post.mock.invocationCallOrder[0]).toBeLessThan(patch.mock.invocationCallOrder[0]);
    expect(patch.mock.invocationCallOrder[0]).toBeLessThan(remove.mock.invocationCallOrder[0]);
  });

  it('lets authorized reviewers explicitly save and approve', async () => {
    show(true);
    await searchAndChoose();
    fireEvent.click(screen.getByRole('button', { name: 'Save & approve' }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/v1/code-mappings/7/', { destination_concept_id: 42, status: 'approved' }));
  });

  it('supports choosing search results from the keyboard', async () => {
    show();
    const input = screen.getByRole('combobox', { name: 'Search destination concepts inline' });
    fireEvent.change(input, { target: { value: 'monoclonal' } });
    await screen.findByRole('option', { name: /Protein.monoclonal/ });
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByText('Selected: Protein.monoclonal')).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it('reports the lock owner and never writes or unlocks their lock', async () => {
    post.mockRejectedValue({ response: { status: 423, data: { locked_by: 'Other curator' } } });
    show();
    await searchAndChoose();
    fireEvent.click(screen.getByRole('button', { name: 'Save choice' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Locked by Other curator');
    expect(patch).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledTimes(1);
    expect(remove).not.toHaveBeenCalled();
  });

  it('releases the lock after a failed write and keeps the choice for retry', async () => {
    patch.mockRejectedValue({ response: { data: { detail: 'Save failed' } } });
    show();
    await searchAndChoose();
    fireEvent.click(screen.getByRole('button', { name: 'Save choice' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Save failed');
    expect(remove).toHaveBeenCalledWith('/v1/code-mappings/7/lock/');
    expect(screen.getByText('Selected: Protein.monoclonal')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save choice' })).toBeEnabled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('refuses to overwrite a destination changed during the search', async () => {
    show();
    await searchAndChoose();
    get.mockResolvedValue({ data: { ...mapping, destination_concept_id: 99 } });
    fireEvent.click(screen.getByRole('button', { name: 'Save choice' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('destination changed');
    expect(patch).not.toHaveBeenCalled();
    expect(remove).toHaveBeenCalledWith('/v1/code-mappings/7/lock/');
  });

  it('does not downgrade a mapping approved while the picker was open', async () => {
    show();
    await searchAndChoose();
    get.mockResolvedValue({ data: { ...mapping, status: 'approved' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save choice' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already approved');
    expect(patch).not.toHaveBeenCalled();
  });

  it('ignores a late result for an obsolete search', async () => {
    let finishOld: (value: unknown) => void = () => undefined;
    get.mockImplementation((url: string, config?: { params?: { q?: string } }) => {
      if (config?.params?.q === 'old') return new Promise(resolve => { finishOld = resolve; });
      return Promise.resolve({ data: url.includes('/search/') ? { results: [concept] } : mapping });
    });
    show();
    const input = screen.getByRole('combobox', { name: 'Search destination concepts inline' });
    fireEvent.change(input, { target: { value: 'old' } });
    await waitFor(() => expect(get).toHaveBeenCalledWith('/v1/concepts/search/', expect.objectContaining({ params: { q: 'old', limit: '25' } })));
    fireEvent.change(input, { target: { value: 'monoclonal' } });
    await screen.findByRole('option', { name: /Protein.monoclonal/ });
    finishOld({ data: { results: [{ ...concept, concept_name: 'Obsolete result' }] } });
    await waitFor(() => expect(screen.queryByText('Obsolete result')).not.toBeInTheDocument());
    expect(screen.getByRole('option', { name: /Protein.monoclonal/ })).toBeInTheDocument();
  });
});
