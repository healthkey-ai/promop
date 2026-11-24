/**
 * Helpers for the record's proposal marks (#2659): `provisional_fields` maps a
 * field the server filled with a proposed value to its confidence percentage.
 * Shared by the provider editor and the federated patient view so both treat a
 * proposal the same way.
 */
import patientVisibleFields from './patientVisibleFields.json';

/** The scalar fields the always-shown patient tabs render (generated; see `omop_core/services/patient_visible_fields.py`). */
const VISIBLE: ReadonlySet<string> = new Set(patientVisibleFields.fields);

export type DiseaseType = 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'mcl' | 'other';

/** The disease type the page picks from a disease text: the first keyword found in the
 *  lowercased text wins, and no match is `other`, which has no disease tab. The rules
 *  are the generated file's, so the server reads the same ones. */
export function diseaseTypeOf(disease: unknown): DiseaseType {
  const text = typeof disease === 'string' ? disease.toLowerCase() : '';
  const rule = (patientVisibleFields.disease_rules as string[][]).find(([keyword]) => text.includes(keyword));
  return (rule ? rule[1] : 'other') as DiseaseType;
}

/** The disease type whose section renders a Disease Category's extension, or `other` for a
 *  category that has no hand-authored section. */
export function diseaseTypeOfCategory(category: string): DiseaseType {
  const byType = patientVisibleFields.disease_categories as Record<string, string>;
  return (Object.keys(byType).find((type) => byType[type] === category) ?? 'other') as DiseaseType;
}

/** The scalar fields a patient sees for a record: the always-shown tabs plus the disease
 *  tab's section for its disease. `category` is the record's Disease Category (`''` for the
 *  base record alone), which picks the section (#2332); without it the disease text does. */
function onScreen(info: Record<string, unknown> | null | undefined, category?: string): Set<string> {
  const byDisease = patientVisibleFields.disease_fields as Record<string, string[]>;
  const type = category === undefined ? diseaseTypeOf(info?.disease) : diseaseTypeOfCategory(category);
  return new Set([...VISIBLE, ...(byDisease[type] ?? [])]);
}

/** How a created field (``CustomPatientField``, #2857) is named in the marks: the key of
 *  `custom_fields` it lives under, prefixed as a pending edit of it is in
 *  `user_edited_fields`. The server reports `provisional_fields["custom_fields.<name>"]`. */
export const ADDED_MARK_PREFIX = 'custom_fields.';

/** The mark name of the created field `name`. */
export const addedMark = (name: string): string => ADDED_MARK_PREFIX + name;

/** The mark names of several created fields. */
export const addedMarks = (names: Iterable<string>): string[] => [...names].map(addedMark);

/** What a created field's definition says about where a patient sees it. */
export interface CreatedFieldScope {
  field_name: string;
  tab: string;
  mode: string;
  diseases?: string[];
}

/** Whether a patient's page shows created field `field`, by the rule the server applies in
 *  `custom_field_values.shown_to_patient`: placed on a tab a patient page has
 *  (`custom_field_tabs` in the generated file: every tab but the unplaced one), part of the
 *  record (no scope, or the record's Disease Category), and on the disease tab only when
 *  the record has one (a Disease Category with a hand-authored section, or a field placed
 *  there for it). `fields` is every definition, which says whether the disease tab exists.
 *  A computed field is its formula's result, so it is not an editable field. */
export function createdFieldShown(
  field: CreatedFieldScope,
  category: string,
  fields: readonly CreatedFieldScope[],
): boolean {
  if (field.mode !== 'editable' || !(patientVisibleFields.custom_field_tabs as string[]).includes(field.tab)) {
    return false;
  }
  const diseases = field.diseases ?? [];
  if (diseases.length > 0 && !diseases.includes(category)) return false;
  if (field.tab !== 'disease') return true;
  return category !== '' && (
    diseaseTypeOfCategory(category) !== 'other'
    || fields.some((f) => f.tab === 'disease' && (f.diseases ?? []).includes(category))
  );
}

/** The record's proposal marks (field to confidence), or none. */
export function marksOf(info: Record<string, unknown> | null | undefined): Record<string, number> {
  const raw = info?.provisional_fields;
  return raw && typeof raw === 'object' ? (raw as Record<string, number>) : {};
}

/** The same record without the named fields' marks: an edited field is approved data now.
 *  A list field that still holds proposed items (`provisional_items`, #2534) keeps its
 *  mark: the server never approves a proposed list item through a save, so the list
 *  stays partly proposed whatever was touched.
 *  Never mutates `info` or its marks; returns `info` itself when nothing changes. */
export function withoutMarks(
  info: Record<string, unknown>,
  fields: Iterable<string>,
): Record<string, unknown> {
  const marks = { ...marksOf(info) };
  const items = (info?.provisional_items ?? {}) as Record<string, unknown[]>;
  let changed = false;
  for (const field of fields) {
    if (Array.isArray(items[field]) && items[field].length > 0) continue;
    if (field in marks) { delete marks[field]; changed = true; }
  }
  return changed ? { ...info, provisional_fields: marks } : info;
}

/** The fields that stop the patient confirming their record (#2761): the proposal
 *  marks on display that a patient tab renders and the patient can edit.
 *
 *  The same rule as `attestation_blockers` on the server, which refuses the
 *  confirmation. A mark counts only for a field in `patientVisibleFields.json`, the
 *  scalar fields the patient tabs name (the disease tab's own section being the one
 *  for the record's disease): a field no tab shows is marked in the record
 *  but never on screen, and a list field has proposed items (`provisional_items`) and
 *  no tab edits it. Then, once the write descriptor has loaded, a field with no write
 *  path of its own (a calculated or mirrored value, a unit picker) is left out, since
 *  it clears with the field it follows, and a field the descriptor does not know
 *  counts. Editing a field removes its mark from `info`, so the count falls as the
 *  patient enters values.
 *
 *  `category` is the record's Disease Category as the page resolves it (`recordDiseaseCategory`);
 *  the server's `disease_profiles.record_category` is the same rule. */
export function attestationBlockers(
  info: Record<string, unknown> | null | undefined,
  descriptors?: Record<string, { writable?: boolean }> | null,
  category?: string,
  createdFields?: readonly CreatedFieldScope[] | null,
): string[] {
  const items = (info?.provisional_items ?? {}) as Record<string, unknown[]>;
  const known = descriptors && Object.keys(descriptors).length > 0 ? descriptors : null;
  const shown = onScreen(info, category);
  const marked = Object.keys(marksOf(info));
  const columns = marked
    .filter((field) => !field.startsWith(ADDED_MARK_PREFIX))
    .filter((field) => !(field in items) && shown.has(field))
    .filter((field) => !known || !(field in known) || !!known[field].writable);
  // A created field is runtime data, so the generated file cannot list it: its definition
  // decides (`createdFieldShown`), with the page's own category, and a field the page has
  // no definition of is not on screen. The record's Disease Category is what scopes it.
  const defined = createdFields ?? [];
  const recordCategory = category === undefined ? diseaseCategoryOfInfo(info) : category;
  const created = marked
    .filter((field) => field.startsWith(ADDED_MARK_PREFIX))
    .filter((field) => {
      const name = field.slice(ADDED_MARK_PREFIX.length);
      const definition = defined.find((f) => f.field_name === name);
      return !!definition && createdFieldShown(definition, recordCategory, defined)
        && (!known || !(name in known) || !!known[name].writable);
    });
  return [...columns, ...created].sort();
}

/** The Disease Category the disease text names, when the caller gave none: the page always does. */
function diseaseCategoryOfInfo(info: Record<string, unknown> | null | undefined): string {
  const byType = patientVisibleFields.disease_categories as Record<string, string>;
  return byType[diseaseTypeOf(info?.disease)] ?? '';
}

/** The proposals the user rejected in this save (#2671): the fields of `patch` that the
 *  record showed a proposal on (`proposed`, its marks) and that are now cleared. The
 *  server rejects a proposal only for a field named here, so a save that merely carries
 *  a blank (a stale page, a whole-record echo) rejects nothing. Sent as
 *  `rejected_proposals` beside the fields themselves. */
export function rejectedProposals(
  patch: Record<string, unknown>,
  proposed: Record<string, number>,
): string[] {
  return Object.entries(patch)
    .filter(([field, value]) => field in proposed && (value === null || value === ''))
    .map(([field]) => field)
    .sort();
}
