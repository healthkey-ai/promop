import { describe, expect, it } from 'vitest';
import { deriveTnbcStatus, genderCode, normalizeGender, receptorSign, stringsToOptions } from './utils';

describe('stringsToOptions', () => {
  it('turns strings into value/label pairs', () => {
    expect(stringsToOptions(['Male', 'Female'])).toEqual([
      { value: 'Male', label: 'Male' },
      { value: 'Female', label: 'Female' },
    ]);
  });

  it('preserves explicit option objects so a code can carry a richer label', () => {
    const options = [{ value: '20', label: 'Grade 2 (6–15 centroblasts/HPF)' }];
    expect(stringsToOptions(options)).toEqual(options);
  });
});

describe('receptorSign', () => {
  it.each([
    ['ER+', 1],
    ['ER+ with low expression', 1],
    ['ER+ with high expression', 1],
    ['ER-', -1],
    ['Positive', 1],
    ['NEGATIVE', -1],
    ['ER+/-', null],
    ['ER+ and PR-', null],
    ['IHC 2+', null],
    ['HER2 3+', null],
    ['Equivocal', null],
    ['HER2 low', null],
    ['Unknown', null],
    ['', null],
  ])('maps %o to %o', (raw, expected) => {
    expect(receptorSign(raw)).toBe(expected);
  });
});

describe('deriveTnbcStatus', () => {
  it('is true only when all three receptors are explicitly negative', () => {
    expect(deriveTnbcStatus('ER-', 'PR-', 'HER2-')).toBe(true);
  });

  it('is false when either hormone receptor is positive', () => {
    expect(deriveTnbcStatus('ER+ with low expression', 'PR-', 'HER2-')).toBe(false);
  });

  it('does not turn an ambiguous result into TNBC', () => {
    expect(deriveTnbcStatus('ER+/-', 'PR-', 'HER2-')).toBeNull();
  });
});

describe('normalizeGender', () => {
  it.each([
    ['M', 'Male'],
    ['m', 'Male'],
    ['Male', 'Male'],
    ['male', 'Male'],
    ['F', 'Female'],
    ['f', 'Female'],
    ['Female', 'Female'],
    ['O', 'Other'],
    ['U', 'Unknown'],
    ['', ''],
  ])('maps %o to the widget option label %o', (raw, expected) => {
    expect(normalizeGender(raw)).toBe(expected);
  });

  it('leaves an unrecognised value alone so it is visible rather than silently dropped', () => {
    expect(normalizeGender('Non-binary')).toBe('Non-binary');
  });
});

describe('genderCode', () => {
  it.each([
    ['Male', 'M'],
    ['Female', 'F'],
    ['Other', 'O'],
    ['Unknown', 'U'],
    ['M', 'M'],
    ['Non-binary', 'Non-binary'],
  ])('maps the widget label %o back to %o', (raw, expected) => {
    expect(genderCode(raw)).toBe(expected);
  });
});
