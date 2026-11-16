import { describe, expect, it } from 'vitest';
import { intentLabel, valueLabel } from './therapyIntent';

describe('therapy line wording (#2613)', () => {
  it.each([
    ['DEFINITIVE_LOCAL', 'Definitive local'],
    ['ADJUVANT', 'Adjuvant'],
    ['METASTATIC_DISEASE_CONTROL', 'Metastatic disease control'],
    ['PALLIATIVE_SYMPTOM', 'Palliative or symptom control'],
    ['adjuvant', 'Adjuvant'],
  ])('maps %s to %s', (token, expected) => {
    expect(intentLabel(token)).toBe(expected);
  });

  it('humanises a token the table does not know', () => {
    expect(intentLabel('SOME_NEW_CODE')).toBe('Some new code');
    expect(valueLabel('PARTIAL_RESPONSE')).toBe('Partial response');
  });

  it('leaves already-written wording alone and renders nothing for absent values', () => {
    expect(intentLabel('Curative')).toBe('Curative');
    expect(intentLabel('Palliative')).toBe('Palliative');
    expect(intentLabel(null)).toBeNull();
    expect(intentLabel('  ')).toBeNull();
    expect(valueLabel(undefined)).toBeNull();
  });
});
