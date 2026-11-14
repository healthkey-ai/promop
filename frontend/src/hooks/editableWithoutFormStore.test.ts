import { describe, expect, it } from 'vitest';
import { editableWithoutFormStore, type FieldDescriptor } from './useWritableFields';

const d = (entry: Record<string, unknown>) => entry as unknown as FieldDescriptor;

describe('editableWithoutFormStore', () => {
  it('follows writable', () => {
    expect(editableWithoutFormStore(d({ writable: true, target: 'patient_record' }))).toBe(true);
    expect(editableWithoutFormStore(d({ writable: false, kind: 'computed' }))).toBe(false);
    expect(editableWithoutFormStore(undefined)).toBe(false);
  });

  it('offers a patient-record normalised read-model (#2425)', () => {
    expect(editableWithoutFormStore(d({ writable: false, target: 'patient_record', normalised_read_model: true }))).toBe(true);
    expect(editableWithoutFormStore(d({ writable: false, target: 'genomics', normalised_read_model: true }))).toBe(false);
  });

  it('lets a caller restriction win', () => {
    expect(editableWithoutFormStore(d({ writable: true, read_only_for_caller: true }))).toBe(false);
    expect(editableWithoutFormStore(d({ normalised_read_model: true, target: 'patient_record', read_only_for_caller: true }))).toBe(false);
  });
});
