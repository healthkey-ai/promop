import { describe, expect, it } from 'vitest';
import {
  BINET_STAGE_OPTIONS,
  BONE_LESIONS_OPTIONS,
  DISEASE_OPTIONS,
  ER_OPTIONS,
  FL_TUMOR_GRADE_OPTIONS,
  GELF_OPTIONS,
  HER2_OPTIONS,
  ISS_STAGE_OPTIONS,
  PR_OPTIONS,
  LYMPHOMA_HISTOLOGIC_TYPE_OPTIONS,
  binetStageCode,
} from './patientConstants';

describe('disease option lists', () => {
  it('keeps the FL grade code as the option value so an API value of 2 matches', () => {
    const grade = FL_TUMOR_GRADE_OPTIONS.find((option) => option.value === '2');
    expect(grade).toBeDefined();
    expect(grade?.label).toContain('Grade 2');
  });

  it('offers only integer FL grades supported by the integer API field', () => {
    expect(FL_TUMOR_GRADE_OPTIONS.map((option) => option.value)).toEqual(['1', '2', '3']);
    expect(FL_TUMOR_GRADE_OPTIONS.every((option) => Number.isInteger(Number(option.value)))).toBe(true);
  });

  it('offers the ISS values generated records may store', () => {
    expect(ISS_STAGE_OPTIONS).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ value: 'ISS 2' }),
        expect.objectContaining({ value: 'ISS II' }),
      ]),
    );
  });

  it('offers the word receptor values imported records may store', () => {
    expect(ER_OPTIONS).toEqual(expect.arrayContaining(['Positive', 'Negative']));
    expect(PR_OPTIONS).toEqual(expect.arrayContaining(['Positive', 'Negative']));
    expect(HER2_OPTIONS).toEqual(expect.arrayContaining(['Positive', 'Negative']));
  });

  it('offers the lymphoma values the projection can store', () => {
    expect(LYMPHOMA_HISTOLOGIC_TYPE_OPTIONS).toEqual(
      expect.arrayContaining([
        'Follicular lymphoma, grade 2',
        'Mantle cell lymphoma',
      ]),
    );
  });

  it('offers the GELF values the projection can store', () => {
    expect(GELF_OPTIONS).toEqual(
      expect.arrayContaining(['High tumor burden', 'Yes', 'No']),
    );
  });

  it('keeps the CLL Binet code as the option value so an API value of B matches', () => {
    expect(BINET_STAGE_OPTIONS).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ value: 'B', label: expect.stringContaining('Binet Stage B') }),
      ]),
    );
  });

  it('normalizes both stored Binet codes and prose labels without inventing stage A', () => {
    expect(binetStageCode(null)).toBe('');
    expect(binetStageCode('')).toBe('');
    expect(binetStageCode('B')).toBe('B');
    expect(binetStageCode('Binet Stage B (≥3 lymphoid areas involved)')).toBe('B');
    expect(binetStageCode('C')).toBe('C');
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
