/**
 * The scalar fields a patient tab renders (#2761).
 *
 * `patientVisibleFields.json` decides which proposal marks stop a patient
 * confirming their record: a name no tab renders would be a block no patient can
 * lift, and a name a tab does render but the list lacks would let a patient
 * attest to a value they can see marked. It is generated from the tab sources by
 * `python -m omop_core.services.patient_visible_fields --write` and read by the
 * server too. The backend suite checks the same match against the models; this
 * one redoes the scan from the JSON's own column list, so it fails in both
 * directions when only the frontend changes (a frontend-only change skips the
 * backend suites).
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import patientVisibleFields from './patientVisibleFields.json';

const TABS = ['GeneralTab.tsx', 'LabsTab.tsx', 'BehaviorTab.tsx', 'WearableTab.tsx'];
const REGENERATE = 'regenerate with: python -m omop_core.services.patient_visible_fields --write';
const here = (...parts: string[]) => resolve(__dirname, ...parts);
const quoted = (source: string) => [...source.matchAll(/['"]([a-z][a-z0-9_]*)['"]/g)].map((m) => m[1]);

describe('patient visible fields', () => {
  it('is exactly the columns the patient tabs name, less the staff-only ones', () => {
    const columns = new Set(patientVisibleFields.columns);
    const staffOnly = new Set(patientVisibleFields.not_for_patients);
    const named = new Set<string>();
    for (const tab of TABS) {
      for (const name of quoted(readFileSync(here('tabs', tab), 'utf8'))) {
        if (columns.has(name) && !staffOnly.has(name)) named.add(name);
      }
    }
    expect(patientVisibleFields.fields, REGENERATE).toEqual([...named].sort());
  });

  it('is sorted, has no duplicates, and never lists a field only staff see', () => {
    const fields = patientVisibleFields.fields;
    expect([...fields]).toEqual([...new Set(fields)].sort());
    expect(fields).toContain('hemoglobin_g_dl');
    expect(fields).not.toContain('concomitant_medications');
    for (const staffOnly of ['validated', 'validated_by', 'validation_date']) {
      expect(fields).not.toContain(staffOnly);
    }
  });
});
