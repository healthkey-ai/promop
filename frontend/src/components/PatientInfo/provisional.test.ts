import { describe, it, expect } from 'vitest';
import { marksOf, withoutMarks } from './provisional';

describe('provisional marks', () => {
  it('reads none from a record without them', () => {
    expect(marksOf(null)).toEqual({});
    expect(marksOf({})).toEqual({});
    expect(marksOf({ provisional_fields: null })).toEqual({});
  });

  it('drops only the named fields and never mutates the record', () => {
    const info = { a: 1, provisional_fields: { a: 80, b: 70 } };
    const next = withoutMarks(info, ['a', 'zzz']);

    expect(next.provisional_fields).toEqual({ b: 70 });
    expect(info.provisional_fields).toEqual({ a: 80, b: 70 });
  });

  it('returns the record itself when nothing changes', () => {
    const info = { provisional_fields: { b: 70 } };
    expect(withoutMarks(info, ['a'])).toBe(info);
  });

  it('keeps the mark of a list that still holds proposed items, touched or not (#2534)', () => {
    const info = {
      prior_procedures: [{ procedure: 'X-ray' }, { procedure: 'PET' }],
      provisional_fields: { prior_procedures: 72, a: 80 },
      provisional_items: { prior_procedures: [{ procedure: 'PET' }] },
    };
    const next = withoutMarks(info, ['prior_procedures', 'a']);

    // The server never approves a proposed list item through a save.
    expect(next.provisional_fields).toEqual({ prior_procedures: 72 });
    expect(withoutMarks(info, ['prior_procedures'])).toBe(info);
  });

  it('drops the mark of a list with no proposed items left', () => {
    const info = {
      provisional_fields: { prior_procedures: 72 },
      provisional_items: { prior_procedures: [] },
    };
    expect(withoutMarks(info, ['prior_procedures']).provisional_fields).toEqual({});
  });
});
