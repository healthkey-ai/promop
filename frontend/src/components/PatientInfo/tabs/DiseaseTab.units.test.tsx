/**
 * Unit captions on the Disease tab already contain the unit, so the shared
 * editor must not append the descriptor's suffix beside them (#5733, the same
 * defect #5704 fixed on Labs).
 *
 * The assertions are on the prop, not on rendered text: the suffix is drawn by
 * PatientFieldEditor and covered by its own tests, so this file only has to
 * prove which captions are declared as unit-carrying.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import DiseaseTab from './DiseaseTab';

vi.mock('@/hooks/useWritableFields', () => ({
  useWritableFields: () => ({ descriptors: {}, loading: false, error: false }),
}));

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], loading: false, error: false, source: undefined }),
}));

vi.mock('@/components/UI/VocabularyTooltip', () => ({
  VocabularyTooltip: () => null,
}));

vi.mock('../controls/MultiSelectControl', () => ({
  default: () => null,
}));

vi.mock('../ClinicalField', () => ({
  default: ({ label, name, labelIncludesUnit }: {
    label: string; name: string; labelIncludesUnit?: boolean;
  }) => (
    <div data-testid={`field-${name}`} data-label-includes-unit={String(Boolean(labelIncludesUnit))}>
      {label}
    </div>
  ),
}));

vi.mock('../Section', () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

function renderTab(diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'mcl' | 'other') {
  return render(<DiseaseTab formData={{}} onChange={() => {}} diseaseType={diseaseType} />);
}

function includesUnit(name: string): string | null {
  return screen.getByTestId(`field-${name}`).getAttribute('data-label-includes-unit');
}

describe('Disease tab units', () => {
  it('marks the two percentage rows named in the report (breast)', () => {
    renderTab('breast');
    expect(includesUnit('ki67_proliferation_index')).toBe('true');
    expect(includesUnit('pd_l1_tumor_cells')).toBe('true');
    expect(includesUnit('pd_l1_ic_percentage')).toBe('true');
  });

  it('marks the percentage row on the MCL section too', () => {
    renderTab('mcl');
    expect(includesUnit('ki67_proliferation_index')).toBe('true');
  });

  it('states the count scale the field is actually stored in', () => {
    // `absolute_lymphocyte_count` is edited in cells/µL (the descriptor sets
    // `unit: CELLS/UL` and there is no unit column), so a ×10⁹/L caption would
    // mislabel the typed number by 1000× (#5733 review).
    renderTab('cll');
    expect(screen.getByTestId('field-absolute_lymphocyte_count').textContent)
      .toBe('Absolute Lymphocyte Count (cells/µL)');
  });

  it('marks the unit-carrying lymphoma, myeloma and CLL captions', () => {
    renderTab('lymphoma');
    expect(includesUnit('clonal_bone_marrow_b_lymphocytes')).toBe('true');
    expect(includesUnit('ldh_upper_limit_normal')).toBe('true');

    renderTab('myeloma');
    expect(includesUnit('clonal_plasma_cells')).toBe('true');
    expect(includesUnit('monoclonal_protein_serum')).toBe('true');
    expect(includesUnit('monoclonal_protein_urine')).toBe('true');

    renderTab('cll');
    expect(includesUnit('absolute_lymphocyte_count')).toBe('true');
    expect(includesUnit('qtcf_value')).toBe('true');
    expect(includesUnit('largest_lymph_node_size')).toBe('true');
  });

  it('does not mark labels whose parentheses are not a unit', () => {
    renderTab('breast');
    // "(ER)" and "(PR)" name the receptor, not a unit.
    expect(includesUnit('estrogen_receptor_status')).toBe('false');
    expect(includesUnit('progesterone_receptor_status')).toBe('false');
    // Unitless boolean and score rows, including the ones whose parentheses
    // name a staging system rather than a unit.
    expect(includesUnit('measurable_disease_by_recist_status')).toBe('false');
    expect(includesUnit('oncotype_dx_score')).toBe('false');
    renderTab('myeloma');
    expect(includesUnit('measurable_disease_imwg')).toBe('false');
    renderTab('cll');
    expect(includesUnit('measurable_disease_iwcll')).toBe('false');
  });
});
