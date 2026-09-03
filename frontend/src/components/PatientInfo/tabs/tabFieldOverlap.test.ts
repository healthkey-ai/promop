/**
 * Blood and Labs must not render the same field.
 *
 * Four of the Blood tab's five sections once rendered the same field keys as
 * sections already on Labs — fourteen analytes with two editable boxes each, on
 * two tabs (#955). Asserting the absence of four section *titles* does not hold
 * that line: re-adding `psa_ng_ml` under a section named anything else passes.
 *
 * This reads the sources because the field lists are module-private consts, and
 * it matches both declaration styles — the `[label, key]` arrays these two tabs
 * use and the inline `<ClinicalField name=...>` form DiseaseTab uses. An
 * ad-hoc scan missing the second is what let a duplicate through once already.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect } from 'vitest';

const HERE = join(process.cwd(), 'src/components/PatientInfo/tabs');

function fieldKeys(file: string): Set<string> {
  const src = readFileSync(join(HERE, file), 'utf8');
  const keys = [
    ...src.matchAll(/\[\s*'[^']*'\s*,\s*'([a-z0-9_]+)'\s*\]/g),
    ...src.matchAll(/<ClinicalField[^>]*?\bname="([a-z0-9_]+)"/gs),
    ...src.matchAll(/field\('[^']*',\s*'([a-z0-9_]+)'/g),
  ].map((m) => m[1]);
  return new Set(keys);
}

describe('tab field ownership', () => {
  it('renders no field on both the Blood and Labs tabs', () => {
    const blood = fieldKeys('BloodTab.tsx');
    const labs = fieldKeys('LabsTab.tsx');
    const shared = [...blood].filter((k) => labs.has(k));
    expect(shared).toEqual([]);
  });

  it('finds the fields it is scanning for', () => {
    // A regex that silently matched nothing would make the test above pass
    // for the wrong reason.
    expect(fieldKeys('BloodTab.tsx').size).toBeGreaterThan(5);
    expect(fieldKeys('LabsTab.tsx').size).toBeGreaterThan(20);
  });
});
