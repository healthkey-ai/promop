import { describe, expect, it } from 'vitest';

import { parseWritableDescriptor } from './descriptorVersion';

describe('the descriptor envelope', () => {
  it('accepts a legacy mapping with no envelope', () => {
    const parsed = parseWritableDescriptor({
      weight: { writable: true, target: 'patient_record' },
    });
    expect(parsed.error).toBeUndefined();
    expect(parsed.schemaVersion).toBeNull();
    expect(parsed.fields.weight).toEqual({ writable: true, target: 'patient_record' });
  });

  it('accepts schema v1 and never treats the envelope as a field', () => {
    const parsed = parseWritableDescriptor({
      __descriptor__: { schema_version: 1, documentation: 'docs/x.md' },
      weight: { writable: true, target: 'patient_record' },
    });
    expect(parsed.error).toBeUndefined();
    expect(parsed.schemaVersion).toBe(1);
    expect(Object.keys(parsed.fields)).toEqual(['weight']);
  });

  it('fails closed on a newer contract instead of writing under old semantics', () => {
    const parsed = parseWritableDescriptor({
      __descriptor__: { schema_version: 2 },
      weight: { writable: true, target: 'patient_record' },
    });
    expect(parsed.schemaVersion).toBe(2);
    expect(parsed.fields).toEqual({});
    expect(parsed.error).toMatch(/too old/i);
  });

  it('fails closed on a malformed version', () => {
    for (const version of ['1', 0, null, undefined, 1.5]) {
      const parsed = parseWritableDescriptor({ __descriptor__: { schema_version: version } });
      expect(parsed.error, String(version)).toMatch(/cannot read/i);
      expect(parsed.fields, String(version)).toEqual({});
    }
  });

  it('fails closed when the response is not a mapping', () => {
    for (const payload of [null, undefined, [], 'nope', 3]) {
      const parsed = parseWritableDescriptor(payload);
      expect(parsed.error).toBeTruthy();
      expect(parsed.fields).toEqual({});
    }
  });
});
