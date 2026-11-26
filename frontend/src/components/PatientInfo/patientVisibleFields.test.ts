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
const byDisease = patientVisibleFields.disease_fields as Record<string, string[]>;
const columns = new Set(patientVisibleFields.columns);
const never = new Set([...patientVisibleFields.not_for_patients, ...patientVisibleFields.not_editable]);

// A `//` counts at the start of a line or after whitespace, so a URL in a string is left alone.
const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|\s)\/\/.*$/gm, '$1');
const isPrecisionCompanion = (name: string) => name.endsWith('_precision') && columns.has(name.slice(0, -10));
/** The columns a source names as a quoted string, less what never blocks. */
const named = (source: string) =>
  [...stripComments(source).matchAll(/['"]([a-z][a-z0-9_]*)['"]/g)].map((m) => m[1])
    .filter((name) => columns.has(name) && !never.has(name) && !isPrecisionCompanion(name));

const functionBlocks = (source: string) => {
  const blocks = new Map<string, string>();
  for (const part of stripComments(source).split(/\n(?=(?:export default )?function \w+)/)) {
    const match = part.match(/^(?:export default )?function (\w+)/);
    if (match) blocks.set(match[1], part);
  }
  return blocks;
};

describe('patient visible fields', () => {
  it('is exactly the columns the patient tabs name, less the staff-only ones', () => {
    const found = new Set<string>();
    for (const tab of TABS) named(readFileSync(here('tabs', tab), 'utf8')).forEach((n) => found.add(n));
    expect(patientVisibleFields.fields, REGENERATE).toEqual([...found].sort());
  });

  it('lists, per disease type, exactly the columns that type\'s disease-tab functions name', () => {
    const blocks = functionBlocks(readFileSync(here('tabs', 'DiseaseTab.tsx'), 'utf8'));
    // A section no disease type names would render fields the attestation never counts.
    const mapped = new Set(Object.values(patientVisibleFields.disease_sections).flat());
    const sections = [...blocks.keys()].filter((n) => n.endsWith('Section'));
    expect([...mapped].sort(), 'map each DiseaseTab section in DISEASE_SECTIONS').toEqual(sections.sort());
    for (const [disease, sectionNames] of Object.entries(patientVisibleFields.disease_sections)) {
      const found = new Set<string>();
      for (const name of sectionNames as string[]) named(blocks.get(name) ?? '').forEach((n) => found.add(n));
      expect(byDisease[disease], REGENERATE).toEqual([...found].sort());
    }
  });

  it('maps each section to the disease type the DiseaseTab switch renders it for', () => {
    // Swapping two `case` lines would put a section's fields on the wrong disease's list.
    const body = functionBlocks(readFileSync(here('tabs', 'DiseaseTab.tsx'), 'utf8')).get('DiseaseSections') ?? '';
    const rendered: Record<string, string[]> = {};
    const pairs = [
      ...body.matchAll(/case '(\w+)':\s*return <(\w+)/g),
      ...body.matchAll(/diseaseType === '(\w+)' && <(\w+)/g),
    ];
    for (const [, kind, section] of pairs) (rendered[kind] ??= []).push(section);
    const mapped = patientVisibleFields.disease_sections as Record<string, string[]>;
    expect(Object.keys(rendered).sort()).toEqual(Object.keys(mapped).sort());
    for (const kind of Object.keys(mapped)) {
      expect([...rendered[kind]].sort(), `DiseaseTab renders these for ${kind}`).toEqual([...mapped[kind]].sort());
    }
  });

  it('finds the controls that bypass Field, in the lymphoma section only', () => {
    expect(byDisease.lymphoma).toEqual(expect.arrayContaining(['gelf_criteria_options', 'flipi_score']));
    for (const other of ['breast', 'myeloma', 'cll', 'mcl']) {
      expect(byDisease[other]).not.toContain('gelf_criteria_options');
      expect(byDisease[other]).not.toContain('flipi_score');
    }
    // Not a column that can hold a proposal (the picker is identity and contact data).
    expect(patientVisibleFields.fields).not.toContain('facility_name');
  });

  it('leaves out a date precision, which follows its date, and a name shown only as text', () => {
    // The precision picker is neither a Field nor a MarkedControl, so it shows no
    // indicator and cannot lift a block; only the date blocks, and entering it lifts it.
    for (const list of [patientVisibleFields.fields, ...Object.values(byDisease)]) {
      expect(list.filter(isPrecisionCompanion)).toEqual([]);
    }
    expect(byDisease.myeloma).toContain('sct_date');
    expect(byDisease.myeloma).not.toContain('sct_date_precision');
    expect(byDisease.lymphoma).toContain('dlbcl_transformation_date');
    expect(byDisease.lymphoma).not.toContain('dlbcl_transformation_date_precision');
    // The legacy cytogenetic summary is text, not a control.
    expect(byDisease.myeloma).not.toContain('cytogenetic_markers');
  });

  it('ignores a column named only in a comment, and keeps a URL in a string', () => {
    expect(named('{/* hemoglobin_g_dl */} // "sodium_meq_l"\n')).toEqual([]);
    expect(named('<a href="https://x.test/">a</a> name="hemoglobin_g_dl" // note')).toEqual(['hemoglobin_g_dl']);
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
