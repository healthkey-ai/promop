/**
 * The scalar fields a patient tab renders (#2761).
 *
 * `patientVisibleFields.json` decides which proposal marks stop a patient
 * confirming their record, so a name no tab renders would be a block no patient
 * can lift. It is generated from the tab sources by
 * `python -m omop_core.services.patient_visible_fields --write`, which also writes
 * the backend's copy; the backend suite checks the full match, and this check
 * runs when only the frontend changes.
 */
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import patientVisibleFields from './patientVisibleFields.json';

const TABS = ['GeneralTab.tsx', 'LabsTab.tsx', 'BehaviorTab.tsx', 'WearableTab.tsx'];
const here = (...parts: string[]) => resolve(__dirname, ...parts);
const quoted = (source: string) => new Set([...source.matchAll(/['"]([a-z][a-z0-9_]*)['"]/g)].map((m) => m[1]));

describe('patient visible fields', () => {
  it('lists only names a patient tab still mentions', () => {
    const mentioned = new Set<string>();
    for (const tab of TABS) {
      for (const name of quoted(readFileSync(here('tabs', tab), 'utf8'))) mentioned.add(name);
    }
    const gone = patientVisibleFields.fields.filter((name) => !mentioned.has(name));
    expect(gone, `regenerate with python -m omop_core.services.patient_visible_fields --write`).toEqual([]);
  });

  it('is sorted and has no duplicates', () => {
    const fields = patientVisibleFields.fields;
    expect([...fields]).toEqual([...new Set(fields)].sort());
    expect(fields).toContain('hemoglobin_g_dl');
    expect(fields).not.toContain('concomitant_medications');
  });

  it('matches the backend copy', () => {
    const backend = here('..', '..', '..', '..', 'omop_core', 'data', 'patient_visible_fields.json');
    // The frontend image is built from frontend/ alone, where the backend copy is absent.
    if (!existsSync(backend)) return;
    expect(JSON.parse(readFileSync(backend, 'utf8'))).toEqual(patientVisibleFields);
  });
});
