import { describe, it, expect } from 'vitest';
import { ACTIONABLE_SKIPS, confirmableFields, skipText } from './provisionalConfirm';

describe('confirmableFields', () => {
  it('is the server list among the fields still marked, sorted', () => {
    const info = {
      provisional_fields: { weight: 80, height: 75, bmi: 75 },
      provisional_confirm: { confirmable: ['weight', 'height', 'sodium_meq_l'], skipped: { bmi: 'calculated' } },
    };
    expect(confirmableFields(info)).toEqual(['height', 'weight']);
  });

  it('is empty when the server sent no list: a patient, an analyst, a credential', () => {
    expect(confirmableFields({ provisional_fields: { weight: 80 } })).toEqual([]);
    expect(confirmableFields(null)).toEqual([]);
    expect(confirmableFields({ provisional_fields: { weight: 80 }, provisional_confirm: { confirmable: 'weight' } })).toEqual([]);
  });

  it('drops a field the user has since edited (its mark is gone)', () => {
    expect(confirmableFields({ provisional_fields: {}, provisional_confirm: { confirmable: ['weight'], skipped: {} } })).toEqual([]);
  });
});

describe('skipText', () => {
  it('says what to do about a changed proposal and shows a validation detail', () => {
    expect(skipText({ field: 'weight', reason: 'changed' })).toMatch(/changed since this page was loaded/);
    expect(skipText({ field: 'weight', reason: 'invalid', detail: 'Too high.' })).toContain('Too high.');
  });

  it('counts only the skips a person should look at', () => {
    expect(ACTIONABLE_SKIPS.has('changed')).toBe(true);
    expect(ACTIONABLE_SKIPS.has('calculated')).toBe(false);
    expect(ACTIONABLE_SKIPS.has('list_field')).toBe(false);
  });
});
