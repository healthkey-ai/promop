import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ValueMappingEditor } from './ValueMappingEditor';
import api from '@/api/axios';

vi.mock('@/api/axios', () => ({ default: { get: vi.fn(), patch: vi.fn() } }));

describe('ValueMappingEditor', () => {
  beforeEach(() => vi.clearAllMocks());
  it('saves an explicit unmapped review without a fake concept', async () => {
    vi.mocked(api.patch).mockResolvedValue({ data: {} });
    const saved = vi.fn();
    render(<ValueMappingEditor choiceId={3} onSaved={saved} />);
    fireEvent.click(screen.getByRole('button', { name: /Review value mapping/ }));
    fireEvent.change(screen.getByLabelText('Disposition'), { target: { value: 'no_equivalent' } });
    fireEvent.change(screen.getByLabelText('Evidence / rationale'), { target: { value: 'Reviewed source meaning.' } });
    fireEvent.change(screen.getByLabelText('Review status'), { target: { value: 'approved' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
    expect(api.patch).toHaveBeenCalledWith('/v1/field-choices/3/mapping/', expect.objectContaining({
      target_concept: null, question_concept: null, outcome: 'no_equivalent', status: 'approved',
    }));
  });
  it('keeps server validation errors and draft evidence visible', async () => {
    vi.mocked(api.patch).mockRejectedValue({ response: { data: { notes: ['Record evidence.'] } } });
    render(<ValueMappingEditor choiceId={3} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /Review value mapping/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Record evidence.');
  });
  it('separates answer candidates from question concepts', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { results: [
      { concept_id: 9191, concept_name: 'Positive', vocabulary_id: 'SNOMED', concept_code: '10828004', domain_id: 'Meas Value' },
      { concept_id: 1, concept_name: 'ER assay', vocabulary_id: 'LOINC', concept_code: '85337-4', domain_id: 'Measurement' },
    ] } });
    render(<ValueMappingEditor choiceId={3} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /Review value mapping/ }));
    fireEvent.change(screen.getByLabelText('Disposition'), { target: { value: 'mapped' } });
    fireEvent.change(screen.getByLabelText('Search value concepts'), { target: { value: 'positive' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('Positive')).toBeInTheDocument();
    expect(screen.queryByText('ER assay')).not.toBeInTheDocument();
  });
});
