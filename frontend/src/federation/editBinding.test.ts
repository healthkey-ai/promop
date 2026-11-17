import { describe, expect, it } from 'vitest';

import { resolveEditField, type BindingEntry } from './editBinding';

/** The two shapes that made the row depend on dictionary order (#5902). */
const hemoglobin = {
  hemoglobin_g_dl: {
    kind: 'computed', writable: false, target: 'patient_record',
    bindings: ['hemoglobinLevel'], normalised_read_model: true,
  },
  hemoglobin_level: {
    kind: 'direct', writable: true, target: 'patient_record',
    bindings: ['hemoglobinLevel'],
  },
  hemoglobin_level_units: {
    kind: 'selectable', writable: false, qualifies: 'hemoglobin_level',
  },
};

const platelet = {
  platelet_count: {
    kind: 'direct', writable: true, target: 'patient_record',
    bindings: ['plateletCount'],
  },
  platelet_count_thousand_per_ul: {
    kind: 'computed', writable: false, target: 'patient_record',
    bindings: ['plateletCount'], normalised_read_model: true,
  },
};

const reversed = <T,>(fields: Record<string, T>): Record<string, T> =>
  Object.fromEntries(Object.entries(fields).reverse());

describe('resolveEditField', () => {
  it('edits the source pair, whichever entry the descriptor lists first', () => {
    const cases: Array<[Record<string, BindingEntry>, string, string]> = [
      [hemoglobin, 'hemoglobinLevel', 'hemoglobin_level'],
      [reversed(hemoglobin), 'hemoglobinLevel', 'hemoglobin_level'],
      [platelet, 'plateletCount', 'platelet_count'],
      [reversed(platelet), 'plateletCount', 'platelet_count'],
    ];
    for (const [fields, alias, expected] of cases) {
      expect(resolveEditField(fields, alias)).toBe(expected);
    }
  });

  it('keeps an exact canonical key exact instead of redirecting to the pair', () => {
    expect(resolveEditField(hemoglobin, 'hemoglobin_g_dl')).toBe('hemoglobin_g_dl');
    expect(resolveEditField(platelet, 'platelet_count_thousand_per_ul'))
      .toBe('platelet_count_thousand_per_ul');
  });

  it('never resolves to a normalised read-model when a real entry exists', () => {
    const fields = {
      weight_projection: { kind: 'computed', writable: false, normalised_read_model: true,
        bindings: ['weight'] },
      weight: { kind: 'direct', writable: true, target: 'patient_record', bindings: ['weight'] },
    };
    expect(resolveEditField(fields, 'weight')).toBe('weight');
  });

  it('refuses to choose between two writable sources', () => {
    const fields = {
      platelet_count: { kind: 'direct', writable: true, target: 'patient_record',
        bindings: ['plateletCount'] },
      platelet_count_other: { kind: 'direct', writable: true, target: 'patient_record',
        bindings: ['plateletCount'] },
    };
    expect(resolveEditField(fields, 'plateletCount')).toBeUndefined();
  });

  it('keeps the old answer for shapes that only ever had one entry', () => {
    // A computed row with editable inputs (BMI, MIPI) and a selectable unit.
    const fields = {
      bmi: { kind: 'computed', writable: false, inputs: ['height', 'weight'],
        bindings: ['bmiMin'] },
    };
    expect(resolveEditField(fields, 'bmiMin')).toBe('bmi');
    // An exact key is its own answer, including the unit companion.
    expect(resolveEditField(hemoglobin, 'hemoglobin_level_units'))
      .toBe('hemoglobin_level_units');
    expect(resolveEditField(hemoglobin, 'notAField')).toBeUndefined();
  });
});
