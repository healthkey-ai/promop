/**
 * Tests for TreatmentTab — therapy component concept_ids display (#189/#231)
 *
 * Each therapy line section shows a read-only "Component concept IDs" line
 * when the server-derived component id list is present; nothing when absent.
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import TreatmentTab from './TreatmentTab';
import { useVocabulary } from '@/hooks/useVocabulary';

vi.mock('@/hooks/useVocabulary', () => ({ useVocabulary: vi.fn() }));

vi.mock('@/components/UI/VocabularyTooltip', () => ({
  VocabularyTooltip: () => null,
}));

describe('TreatmentTab - component concept ids', () => {
  beforeEach(() => {
    (useVocabulary as Mock).mockReturnValue({ options: [], source: null, loading: false });
  });

  const baseFormData: Record<string, unknown> = {
    therapy_lines_count: 2,
    first_line_therapy: 'RVD',
    second_line_therapy: 'Kd',
  };

  it('renders component ids for lines that have them', () => {
    render(
      <TreatmentTab
        formData={{
          ...baseFormData,
          first_line_component_ids: [35900001, 35900002, 1900001],
          second_line_component_ids: [35900003],
        }}
        onChange={vi.fn()}
        diseaseType="myeloma"
      />,
    );
    expect(screen.getByText('Component concept IDs: 35900001, 35900002, 1900001')).toBeInTheDocument();
    expect(screen.getByText('Component concept IDs: 35900003')).toBeInTheDocument();
  });

  it('renders nothing for lines without component ids', () => {
    render(
      <TreatmentTab formData={baseFormData} onChange={vi.fn()} diseaseType="myeloma" />,
    );
    expect(screen.queryByText(/Component concept IDs:/)).not.toBeInTheDocument();
  });

  it('ignores empty component id lists', () => {
    render(
      <TreatmentTab
        formData={{ ...baseFormData, first_line_component_ids: [] }}
        onChange={vi.fn()}
        diseaseType="myeloma"
      />,
    );
    expect(screen.queryByText(/Component concept IDs:/)).not.toBeInTheDocument();
  });

  it('renders therapy-type class ids for lines that have them', () => {
    render(
      <TreatmentTab
        formData={{
          ...baseFormData,
          first_line_therapy_type_ids: [35807295, 35807403],
          second_line_therapy_type_ids: [35807295],
        }}
        onChange={vi.fn()}
        diseaseType="myeloma"
      />,
    );
    expect(screen.getByText('Therapy type concept IDs: 35807295, 35807403')).toBeInTheDocument();
    expect(screen.getByText('Therapy type concept IDs: 35807295')).toBeInTheDocument();
  });

  it('renders nothing for lines without type class ids', () => {
    render(
      <TreatmentTab formData={baseFormData} onChange={vi.fn()} diseaseType="myeloma" />,
    );
    expect(screen.queryByText(/Therapy type concept IDs:/)).not.toBeInTheDocument();
  });

  it('labels each later line with its own line number (not always 3)', () => {
    render(
      <TreatmentTab
        formData={{
          ...baseFormData,
          therapy_lines_count: 5,
          later_therapy: 'RegA',
          later_therapies: [
            { lineNumber: 3, therapy: 'RegA', startDate: '2024-01-01', endDate: null },
            { lineNumber: 5, therapy: 'RegC', startDate: '2024-06-01', endDate: null },
          ],
        }}
        onChange={vi.fn()}
        diseaseType="myeloma"
      />,
    );
    expect(screen.getByText('Line 3:')).toBeInTheDocument();
    expect(screen.getByText('Line 5:')).toBeInTheDocument();
  });
});
