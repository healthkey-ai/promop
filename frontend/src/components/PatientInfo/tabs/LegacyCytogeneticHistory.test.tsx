import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import LegacyCytogeneticHistory from './LegacyCytogeneticHistory';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock('@/api/clinicalTransport', () => ({ clinicalClient: () => mocks, clinicalUrl: (path: string) => path }));
const original = { id: 'observation:1', date: '2018-04-03', source_value: 'mm-cytogenetic-markers',
  text: '1q21 gain/amplification, del(17p13), source prose', value_concept_name: null,
  state: 'recorded', unresolved_note: false };
const page = (results = [original], next_cursor: string | null = null) => ({ data: { results, next_cursor } });

beforeEach(() => { vi.resetAllMocks(); mocks.get.mockResolvedValue(page()); });

it('preserves original wording and dates and distinguishes editing history from negative tests', async () => {
  mocks.get.mockResolvedValue(page([original,
    { ...original, id: 'observation:2', text: '1q_amp', state: 'selection_cleared' },
    { ...original, id: 'measurement:1', text: '[note:123]', state: 'marked_in_error', unresolved_note: true }]));
  render(<LegacyCytogeneticHistory personId={42} />);
  expect(await screen.findByText(original.text)).toBeInTheDocument();
  expect(screen.getAllByText('2018-04-03')).toHaveLength(3);
  expect(screen.getByText('1q_amp')).toBeInTheDocument();
  expect(screen.getByText('Historical selection cleared')).toBeInTheDocument();
  expect(screen.getByText('Marked in error')).toBeInTheDocument();
  expect(screen.getByText('Full source text is unavailable.')).toBeInTheDocument();
  expect(screen.getByText(/does not establish a negative test/)).toBeInTheDocument();
  expect(mocks.post).not.toHaveBeenCalled();
  expect(mocks.patch).not.toHaveBeenCalled();
  expect(mocks.delete).not.toHaveBeenCalled();
});

it('shows cache-only source text with an unavailable date', async () => {
  mocks.get.mockResolvedValue({ data: { results: [], next_cursor: null,
    legacy_summary: { text: 'Undated imported aggregate', date: null } } });
  render(<LegacyCytogeneticHistory personId={42} />);
  expect(await screen.findByText('Undated imported aggregate')).toBeInTheDocument();
  expect(screen.getByText('Legacy summary — date unavailable')).toBeInTheDocument();
  expect(screen.queryByText('No legacy cytogenetic results recorded.')).not.toBeInTheDocument();
});

it('retries a failed earlier page without discarding or duplicating the first page', async () => {
  const cursor = '2018-04-03:observation:1';
  mocks.get.mockResolvedValueOnce(page([original], cursor)).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(page([{ ...original, id: 'measurement:2', date: '2017-01-02', text: '1q_gain' }]));
  render(<LegacyCytogeneticHistory personId={42} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load earlier results', hidden: true }));
  fireEvent.click(await screen.findByRole('button', { name: 'Retry legacy results', hidden: true }));
  expect(await screen.findByText('1q_gain')).toBeInTheDocument();
  expect(screen.getAllByText(original.text)).toHaveLength(1);
  expect(mocks.get).toHaveBeenLastCalledWith('/v1/patient-records/42/genomics-legacy-cytogenetics/', { params: { cursor } });
  expect(screen.queryByRole('button', { name: 'Load earlier results', hidden: true })).not.toBeInTheDocument();
});

it('isolates history and late responses when the keyed patient changes', async () => {
  let resolveOld!: (value: ReturnType<typeof page>) => void;
  mocks.get.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(page([{ ...original, text: 'Second patient source' }]));
  const { rerender } = render(<LegacyCytogeneticHistory key={42} personId={42} />);
  rerender(<LegacyCytogeneticHistory key={43} personId={43} />);
  await screen.findByText('Second patient source');
  await act(async () => resolveOld(page()));
  expect(screen.queryByText(original.text)).not.toBeInTheDocument();
  expect(screen.getByText('Second patient source')).toBeInTheDocument();
});

it('offers retry on initial failure and then shows an empty history', async () => {
  mocks.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page([]));
  render(<LegacyCytogeneticHistory personId={42} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Retry legacy results', hidden: true }));
  await screen.findByText('No legacy cytogenetic results recorded.');
  await waitFor(() => expect(screen.queryByText('Loading legacy cytogenetic results…')).not.toBeInTheDocument());
});
