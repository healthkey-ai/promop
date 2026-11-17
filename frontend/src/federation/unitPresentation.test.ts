import { describe, expect, it } from 'vitest';
import { displayUnitLabel, labelNamesUnit } from './unitPresentation';

describe('unit presentation (#2610)', () => {
  it.each([
    ['mm[Hg]', 'mmHg'],
    ['/min', 'bpm'],
    ['10*3/uL', '10³/µL'],
    ['10*6/uL', '10⁶/µL'],
    ['10*9/L', '10⁹/L'],
    ['10*12/L', '10¹²/L'],
    ['mL/min/1.73m2', 'mL/min/1.73m²'],
    ['g/dL', 'g/dL'],
  ])('shows %s as %s', (stored, shown) => {
    expect(displayUnitLabel(stored)).toBe(shown);
  });

  it('has nothing to show for a placeholder or an empty unit', () => {
    expect(displayUnitLabel('{score}')).toBeNull();
    expect(displayUnitLabel('')).toBeNull();
    expect(displayUnitLabel(null)).toBeNull();
  });

  it.each([
    ['Systolic Blood Pressure (mmHg)', 'mm[Hg]', true],
    ['Average Sleep Hours per Night', 'h', true],
    ['Weight (kg)', 'kg', true],
    ['Weight (kg) (self-reported)', 'kg', true],
    ['Weight', 'kg', false],
    ['Stress Level', 's', false],
    ['COVID-19 severity', 'h', false],
  ])('label %s with unit %s names it: %s', (label, unit, expected) => {
    expect(labelNamesUnit(label, unit)).toBe(expected);
  });
});
