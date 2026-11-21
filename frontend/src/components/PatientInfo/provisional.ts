/**
 * Helpers for the record's proposal marks (#2659): `provisional_fields` maps a
 * field the server filled with a proposed value to its confidence percentage.
 * Shared by the provider editor and the federated patient view so both treat a
 * proposal the same way.
 */
import patientVisibleFields from './patientVisibleFields.json';

/** The scalar fields a patient tab renders (generated; see `omop_core/services/patient_visible_fields.py`). */
const VISIBLE: ReadonlySet<string> = new Set(patientVisibleFields.fields);

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
 *  scalar fields the patient tabs name: a field no tab shows is marked in the record
 *  but never on screen, and a list field has proposed items (`provisional_items`) and
 *  no tab edits it. Then, once the write descriptor has loaded, a field with no write
 *  path of its own (a calculated or mirrored value, a unit picker) is left out, since
 *  it clears with the field it follows, and a field the descriptor does not know
 *  counts. Editing a field removes its mark from `info`, so the count falls as the
 *  patient enters values. */
export function attestationBlockers(
  info: Record<string, unknown> | null | undefined,
  descriptors?: Record<string, { writable?: boolean }> | null,
): string[] {
  const items = (info?.provisional_items ?? {}) as Record<string, unknown[]>;
  const known = descriptors && Object.keys(descriptors).length > 0 ? descriptors : null;
  return Object.keys(marksOf(info))
    .filter((field) => !(field in items) && VISIBLE.has(field))
    .filter((field) => !known || !(field in known) || !!known[field].writable)
    .sort();
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
