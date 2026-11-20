/**
 * The disease tab's controls that do not render through `Field` (the GELF and
 * FLIPI checklists) show the same provisional indicator as the fields that do,
 * and no indicator for a field that holds no proposal (#2659).
 */
import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import DiseaseTab from './DiseaseTab';
import { ProvisionalContext } from '../ProvisionalContext';
import { __resetWritableFieldsCache, fetchWritableFields } from '@/hooks/useWritableFields';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: vi.fn(() => ({ options: [], source: null, loading: false })),
}));
vi.mock('@/api/axios', () => ({
  default: {
    get: vi.fn(() => Promise.resolve({ data: {} })),
    post: vi.fn(), patch: vi.fn(),
  },
}));
vi.mock('@/components/UI/VocabularyTooltip', () => ({ VocabularyTooltip: () => null }));

const renderTab = (diseaseType: 'lymphoma' | 'myeloma', marks: Record<string, number>, patientMode = false) =>
  render(
    <ProvisionalContext.Provider value={{ marks, patientMode }}>
      <DiseaseTab diseaseType={diseaseType} formData={{ person_id: 7 }} onChange={vi.fn()} />
    </ProvisionalContext.Provider>,
  );

describe('DiseaseTab provisional indicators', () => {
  beforeEach(async () => {
    __resetWritableFieldsCache();
    await fetchWritableFields();
  });

  it('marks the GELF checklist when its field holds a proposal', () => {
    renderTab('lymphoma', { gelf_criteria_options: 80 });

    expect(screen.getByTestId('provisional-gelf_criteria_options')).toHaveTextContent('Provisional 80%');
  });

  it('marks the FLIPI control by the score it shows', () => {
    renderTab('lymphoma', { flipi_score: 66 });

    expect(screen.getByTestId('provisional-flipi_score')).toHaveTextContent('Provisional 66%');
  });

  it('marks a field that renders through Field in the same way', () => {
    renderTab('lymphoma', { b_symptom_fever: 75, gelf_criteria_options: 80 });

    expect(screen.getByTestId('provisional-b_symptom_fever')).toBeInTheDocument();
    expect(screen.getByTestId('provisional-gelf_criteria_options')).toBeInTheDocument();
  });

  it('does not tell a patient to enter the calculated FLIPI score themselves', () => {
    renderTab('lymphoma', { flipi_score: 66 }, true);

    const title = screen.getByTestId('provisional-flipi_score').title;
    expect(title).toMatch(/Not yet confirmed/);
    expect(title).toMatch(/choosing the risk factors below/);
    expect(title).not.toMatch(/enter it yourself/);
  });

  it('words the indicator for a patient as not yet confirmed', () => {
    renderTab('lymphoma', { gelf_criteria_options: 80 }, true);

    expect(screen.getByTestId('provisional-gelf_criteria_options').title).toMatch(/Not yet confirmed/);
  });

  it('shows no indicator, and no gap, when nothing is proposed', () => {
    renderTab('lymphoma', {});

    expect(screen.queryByTestId('provisional-gelf_criteria_options')).not.toBeInTheDocument();
    expect(screen.queryByTestId('provisional-flipi_score')).not.toBeInTheDocument();
    // The checklist itself still renders.
    expect(screen.getByText('GELF high tumor burden')).toBeInTheDocument();
    expect(screen.getByText('FLIPI-1 assessment')).toBeInTheDocument();
  });

  it('shows the lymphoma checklists nowhere else', () => {
    renderTab('myeloma', { gelf_criteria_options: 80, flipi_score: 66 });

    expect(screen.queryByTestId('provisional-gelf_criteria_options')).not.toBeInTheDocument();
    expect(screen.queryByText('GELF high tumor burden')).not.toBeInTheDocument();
  });
});
