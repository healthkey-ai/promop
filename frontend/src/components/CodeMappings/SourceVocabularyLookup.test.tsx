import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SourceVocabularyLookup, { type SourceTerm } from './SourceVocabularyLookup';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('@/api/axios', () => ({ default: { get } }));
const term: SourceTerm = { vocabulary_id: 'NCIt', code: 'C141394', name: 'RISS Stage I Multiple Myeloma',
  definition: 'Publisher definition.', synonyms: ['R-ISS Stage I'], parents: ['C141393'],
  semantic_types: ['Neoplastic Process'], status: '', retired: false, release_version: '26.08e', source_url: 'https://example.org' };
beforeEach(() => { vi.resetAllMocks(); });

it('shows publisher metadata for the entered code', async () => {
  get.mockResolvedValue({ data: { available: true, term, results: [] } });
  render(<SourceVocabularyLookup vocabularyId="NCIt" code={term.code} onSelect={vi.fn()} />);
  expect(await screen.findByText('Publisher definition.')).toBeInTheDocument();
  expect(screen.getByText(/R-ISS Stage I/)).toBeInTheDocument();
  expect(screen.getByText(/26.08e/)).toBeInTheDocument();
});

it('searches synonyms without losing input focus and selects a source', async () => {
  get.mockResolvedValue({ data: { available: true, term: null, results: [] } });
  const select = vi.fn();
  render(<SourceVocabularyLookup vocabularyId="NCIt" code="" onSelect={select} />);
  const input = await screen.findByLabelText('Search NCIt source codes');
  input.focus();
  get.mockResolvedValue({ data: { available: true, term: null, results: [term] } });
  fireEvent.change(input, { target: { value: 'r-iss' } });
  expect(input).toHaveFocus();
  fireEvent.click(await screen.findByRole('button', { name: /C141394/ }));
  expect(select).toHaveBeenCalledWith(term);
});

it('discards late results when the source vocabulary changes', async () => {
  let resolve!: (value: unknown) => void;
  get.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const view = render(<SourceVocabularyLookup vocabularyId="NCIt" code={term.code} onSelect={vi.fn()} />);
  await waitFor(() => expect(get).toHaveBeenCalledOnce());
  get.mockResolvedValue({ data: { available: false, term: null, results: [] } });
  view.rerender(<SourceVocabularyLookup vocabularyId="LOINC" code="123" onSelect={vi.fn()} />);
  resolve({ data: { available: true, term, results: [] } });
  await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  expect(screen.queryByText('Publisher definition.')).not.toBeInTheDocument();
});

it('flags an explicitly looked-up retired code', async () => {
  get.mockResolvedValue({ data: { available: true, term: { ...term, retired: true }, results: [] } });
  render(<SourceVocabularyLookup vocabularyId="NCIt" code={term.code} onSelect={vi.fn()} />);
  expect(await screen.findByRole('status')).toHaveTextContent('retired or obsolete');
});
