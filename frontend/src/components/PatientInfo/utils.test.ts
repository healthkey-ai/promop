import { describe, expect, it } from 'vitest';
import { normalizeGender, stringsToOptions } from './utils';

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
