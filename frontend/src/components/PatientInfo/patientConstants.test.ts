import { describe, expect, it } from 'vitest';
import {
  BINET_STAGE_OPTIONS,
  BONE_LESIONS_OPTIONS,
  DISEASE_OPTIONS,
  FL_TUMOR_GRADE_OPTIONS,
  GELF_OPTIONS,
  LYMPHOMA_HISTOLOGIC_TYPE_OPTIONS,
} from './patientConstants';

describe('disease option lists', () => {
  it('keeps the FL grade code as the option value so an API value of 2 matches', () => {
    const grade = FL_TUMOR_GRADE_OPTIONS.find((option) => option.value === '2');
    expect(grade).toBeDefined();
    expect(grade?.label).toContain('Grade 2');
  });

  it('offers the lymphoma values the projection can store', () => {
    expect(LYMPHOMA_HISTOLOGIC_TYPE_OPTIONS).toEqual(
      expect.arrayContaining([
        'Follicular lymphoma, grade 2',
        'Mantle cell lymphoma',
      ]),
    );
  });

  it('offers the GELF high-tumor-burden value used by the demo record', () => {
    expect(GELF_OPTIONS).toContain('High tumor burden');
  });

  it('renders a CLL Binet code as a labelled option', () => {
    expect(BINET_STAGE_OPTIONS.find((option) => option.startsWith('Binet Stage B'))).toBeDefined();
  });

  it('has a disease option whose value matches the stored CLL value', () => {
    expect(DISEASE_OPTIONS).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ value: 'chronic lymphocytic leukemia' }),
      ]),
    );
  });

  it('has a disease option whose value matches the stored mantle-cell value', () => {
    expect(DISEASE_OPTIONS).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ value: 'mantle cell lymphoma' }),
      ]),
    );
  });

  it('offers the present/absent bone-lesion values the projection stores', () => {
    expect(BONE_LESIONS_OPTIONS).toEqual(
      expect.arrayContaining(['Present', 'Absent']),
    );
  });
});
