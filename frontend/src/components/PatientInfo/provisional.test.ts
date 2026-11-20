import { describe, it, expect } from 'vitest';
import { attestationBlockers, marksOf, withoutMarks } from './provisional';

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

describe('attestationBlockers (#2761)', () => {
  const descriptors = {
    hemoglobin_g_dl: { writable: true },
    hemoglobin_level: { writable: false },
    esr: { writable: true },
  };

  it('is the marked fields the patient can edit, sorted', () => {
    const info = { provisional_fields: { esr: 70, hemoglobin_g_dl: 82, hemoglobin_level: 82 } };
    expect(attestationBlockers(info, descriptors)).toEqual(['esr', 'hemoglobin_g_dl']);
  });

  it('leaves out a list that still holds proposed items', () => {
    const info = {
      provisional_fields: { prior_procedures: 72, esr: 70 },
      provisional_items: { prior_procedures: [{ procedure: 'PET' }] },
    };
    expect(attestationBlockers(info, descriptors)).toEqual(['esr']);
  });

  it('blocks on a field the descriptor does not know, as the server does', () => {
    expect(attestationBlockers({ provisional_fields: { mystery: 80 } }, descriptors)).toEqual(['mystery']);
  });

  it('counts every scalar mark until a descriptor has loaded', () => {
    const info = { provisional_fields: { hemoglobin_level: 82, esr: 70 } };
    expect(attestationBlockers(info, null)).toEqual(['esr', 'hemoglobin_level']);
    expect(attestationBlockers(info, {})).toEqual(['esr', 'hemoglobin_level']);
  });

  it('is empty for a record with no marks', () => {
    expect(attestationBlockers(null, descriptors)).toEqual([]);
    expect(attestationBlockers({ provisional_fields: {} }, descriptors)).toEqual([]);
  });

  it('falls as the patient edits a marked field', () => {
    const info = { provisional_fields: { esr: 70, hemoglobin_g_dl: 82 } };
    expect(attestationBlockers(withoutMarks(info, ['esr']), descriptors)).toEqual(['hemoglobin_g_dl']);
  });
});
