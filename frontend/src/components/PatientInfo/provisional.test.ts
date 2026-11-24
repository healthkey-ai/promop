import { describe, it, expect } from 'vitest';
import {
  addedMark, addedMarks, attestationBlockers, createdFieldShown, diseaseTypeOf, diseaseTypeOfCategory, marksOf,
  rejectedProposals, withoutMarks,
} from './provisional';
import createdFieldCases from './createdFieldAttestationCases.json';
import patientVisibleFields from './patientVisibleFields.json';

describe('rejectedProposals', () => {
  const proposed = { hemoglobin_g_dl: 82, smoking_status: 70 };

  it('names a field the record showed a proposal on that is now cleared', () => {
    expect(rejectedProposals({ hemoglobin_g_dl: null, smoking_status: '' }, proposed))
      .toEqual(['hemoglobin_g_dl', 'smoking_status']);
  });

  it('names nothing for a value, a field with no proposal, or zero and false', () => {
    expect(rejectedProposals({ hemoglobin_g_dl: 13, sodium_meq_l: null, smoking_status: false }, proposed)).toEqual([]);
    expect(rejectedProposals({ hemoglobin_g_dl: 0 }, proposed)).toEqual([]);
  });
});

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
    esr: { writable: true },
    bmi: { writable: false },
  };

  it('is the marked fields a patient tab shows and the patient can edit, sorted', () => {
    const info = { provisional_fields: { esr: 70, hemoglobin_g_dl: 82, bmi: 80 } };
    expect(attestationBlockers(info, descriptors)).toEqual(['esr', 'hemoglobin_g_dl']);
  });

  it('leaves out a marked field no patient tab shows', () => {
    const info = {
      provisional_fields: { concomitant_medications: 85, diagnosis_date: 88, blood_type: 90, esr: 70 },
    };
    expect(attestationBlockers(info, descriptors)).toEqual(['esr']);
    expect(attestationBlockers(info, null)).toEqual(['esr']);
  });

  it('leaves out a list that still holds proposed items', () => {
    const info = {
      provisional_fields: { prior_procedures: 72, esr: 70 },
      provisional_items: { prior_procedures: [{ procedure: 'PET' }] },
    };
    expect(attestationBlockers(info, descriptors)).toEqual(['esr']);
  });

  it('blocks on a shown field the descriptor does not know, as the server does', () => {
    expect(attestationBlockers({ provisional_fields: { heartrate: 80 } }, descriptors)).toEqual(['heartrate']);
  });

  it('uses the shown fields alone until a descriptor has loaded', () => {
    const info = { provisional_fields: { bmi: 80, esr: 70, concomitant_medications: 85 } };
    expect(attestationBlockers(info, null)).toEqual(['bmi', 'esr']);
    expect(attestationBlockers(info, {})).toEqual(['bmi', 'esr']);
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

// The same cases are in tests/test_patient_visible_fields.py: the page and the server must
// pick the same disease section from one text.
const DISEASE_TYPE_CASES: [unknown, string][] = [
  ['Breast Cancer', 'breast'],
  ['Mantle Cell Lymphoma', 'mcl'],
  ['Follicular Lymphoma', 'lymphoma'],
  ['Multiple Myeloma', 'myeloma'],
  ['CLL', 'cll'],
  ['Chronic Lymphocytic Leukemia', 'cll'],
  ['Squamous cell carcinoma', 'other'],
  ['', 'other'],
  [null, 'other'],
  [42, 'other'],
  ['breast lymphoma', 'breast'],
  ['MANTLE CELL LYMPHOMA', 'mcl'],
];

describe('diseaseTypeOf', () => {
  it.each(DISEASE_TYPE_CASES)('reads %j as %s', (text, expected) => {
    expect(diseaseTypeOf(text)).toBe(expected);
  });
});

describe('attestationBlockers with the disease tab and the checklists (#2761)', () => {
  const lymphoma = (marks: Record<string, number>) => ({ disease: 'Follicular Lymphoma', provisional_fields: marks });

  it('counts a field of the disease section the record shows', () => {
    expect(attestationBlockers(lymphoma({ b_symptom_fever: 75, esr: 70 }), null)).toEqual(['b_symptom_fever', 'esr']);
  });

  it('counts the GELF checklist on a lymphoma record', () => {
    expect(attestationBlockers(lymphoma({ gelf_criteria_options: 80 }), null)).toEqual(['gelf_criteria_options']);
  });

  it('leaves out a disease field the page does not show for this disease', () => {
    const myeloma = { disease: 'Multiple Myeloma', provisional_fields: { b_symptom_fever: 75, gelf_criteria_options: 80, esr: 70 } };
    expect(attestationBlockers(myeloma, null)).toEqual(['esr']);
    expect(attestationBlockers({ provisional_fields: { b_symptom_fever: 75 } }, null)).toEqual([]);
  });

  it('follows the disease of the record it is given', () => {
    const marks = { b_symptom_fever: 75 };
    expect(attestationBlockers({ disease: 'Follicular Lymphoma', provisional_fields: marks }, null)).toEqual(['b_symptom_fever']);
    expect(attestationBlockers({ disease: 'Breast Cancer', provisional_fields: marks }, null)).toEqual([]);
  });

  it('leaves out a calculated field once the descriptor says it has no write path', () => {
    const descriptors = { flipi_score: { writable: false }, b_symptom_fever: { writable: true } };
    expect(attestationBlockers(lymphoma({ flipi_score: 66, b_symptom_fever: 75 }), descriptors)).toEqual(['b_symptom_fever']);
  });
});

describe('attestationBlockers with the record\'s Disease Category (#2332)', () => {
  const marks = { provisional_fields: { b_symptom_fever: 75, esr: 70 }, disease: 'Follicular Lymphoma' };

  it('counts the section of the category, whatever the disease text says', () => {
    // A diffuse large B-cell lymphoma the name match would read as follicular, filed under a category with no section.
    const dlbcl = { ...marks, disease: 'Diffuse Large B-Cell Lymphoma' };
    expect(attestationBlockers(dlbcl, null)).toEqual(['b_symptom_fever', 'esr']);
    expect(attestationBlockers(dlbcl, null, 'DLBCL')).toEqual(['esr']);
    expect(attestationBlockers(dlbcl, null, 'FL')).toEqual(['b_symptom_fever', 'esr']);
  });

  it('counts only the always-shown tabs for the base record', () => {
    expect(attestationBlockers(marks, null, '')).toEqual(['esr']);
  });
});

describe('diseaseTypeOfCategory', () => {
  it.each([
    ['BC', 'breast'], ['FL', 'lymphoma'], ['MM', 'myeloma'], ['CLL', 'cll'], ['MCL', 'mcl'],
    ['DLBCL', 'other'], ['', 'other'],
  ])('reads %j as %s', (code, expected) => {
    expect(diseaseTypeOfCategory(code)).toBe(expected);
  });
});

describe('created fields in the marks (#2857)', () => {
  const field = (name: string, extra: Record<string, unknown> = {}) => ({
    field_name: name, tab: 'general', mode: 'editable', diseases: [] as string[], ...extra,
  });

  it('names a created field by the key it lives under, as a pending edit of it is', () => {
    expect(addedMark('frailty_index')).toBe('custom_fields.frailty_index');
    expect(addedMarks(['a', 'b'])).toEqual(['custom_fields.a', 'custom_fields.b']);
  });

  it('drops the mark of an edited created field and leaves its neighbour', () => {
    const info = { provisional_fields: { 'custom_fields.a': 80, 'custom_fields.b': 70 } };
    expect(withoutMarks(info, addedMarks(['a'])).provisional_fields).toEqual({ 'custom_fields.b': 70 });
  });

  it('blocks on a marked, placed, editable created field the page loaded', () => {
    const info = { provisional_fields: { 'custom_fields.frailty_note': 82 } };
    const fields = [field('frailty_note')];
    expect(attestationBlockers(info, { frailty_note: { writable: true } }, '', fields)).toEqual(['custom_fields.frailty_note']);
    // Counted with the column marks, in one sorted list.
    expect(attestationBlockers({ provisional_fields: { 'custom_fields.frailty_note': 82, esr: 70 } },
      { frailty_note: { writable: true } }, '', fields)).toEqual(['custom_fields.frailty_note', 'esr']);
  });

  it('counts none of them until the definitions have loaded, and none for an undefined name', () => {
    const info = { provisional_fields: { 'custom_fields.frailty_note': 82, 'custom_fields.gone': 70 } };
    expect(attestationBlockers(info, null, '', [])).toEqual([]);
    expect(attestationBlockers(info, null, '', [field('frailty_note')])).toEqual(['custom_fields.frailty_note']);
  });

  it('does not block on a field the write descriptor says the patient cannot write', () => {
    const info = { provisional_fields: { 'custom_fields.frailty_note': 82 } };
    expect(attestationBlockers(info, { frailty_note: { writable: false } }, '', [field('frailty_note')])).toEqual([]);
  });

  it('lists the same tabs the server reads from the generated file', () => {
    expect(patientVisibleFields.custom_field_tabs).toEqual(
      ['general', 'disease', 'treatment', 'blood', 'labs', 'genomics', 'behavior', 'wearable']);
    expect(createdFieldShown(field('x', { tab: 'omop' }), '', [])).toBe(false);
  });

  // The server runs these very cases against `custom_field_values.shown_to_patient`
  // (tests/test_provisional_custom_fields.py), so the rule cannot differ between the two.
  describe.each(createdFieldCases.cases)('$name', (c) => {
    it('agrees with the server rule', () => {
      const definition = field('frailty_note', { tab: c.tab, mode: c.mode ?? 'editable', diseases: c.diseases });
      const approved = c.approved !== false && (c.mode ?? 'editable') === 'editable';
      const info = { disease: c.disease, provisional_fields: { 'custom_fields.frailty_note': 82 } };

      const blockers = attestationBlockers(info, { frailty_note: { writable: approved } }, c.category, [definition]);

      expect(blockers).toEqual(c.blocks ? ['custom_fields.frailty_note'] : []);
    });
  });
});
