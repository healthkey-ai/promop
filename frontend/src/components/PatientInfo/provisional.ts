/**
 * Helpers for the record's proposal marks (#2659): `provisional_fields` maps a
 * field the server filled with a proposed value to its confidence percentage.
 * Shared by the provider editor and the federated patient view so both treat a
 * proposal the same way.
 */

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
 *  marks on display that the patient can clear by editing the field.
 *
 *  The same rule as `attestation_blockers` on the server, which refuses the
 *  confirmation: a list field (it has proposed items) is left out because no
 *  patient tab shows it, and so is a field the write descriptor says has no write
 *  path of its own (a calculated or mirrored value, a unit picker), which clears
 *  with the field it follows. A field the descriptor does not know blocks, and
 *  with no descriptor yet every scalar mark does. Editing a field removes its
 *  mark from `info`, so the count falls as the patient confirms values. */
export function attestationBlockers(
  info: Record<string, unknown> | null | undefined,
  descriptors?: Record<string, { writable?: boolean }> | null,
): string[] {
  const items = (info?.provisional_items ?? {}) as Record<string, unknown[]>;
  const known = descriptors && Object.keys(descriptors).length > 0 ? descriptors : null;
  return Object.keys(marksOf(info))
    .filter((field) => !(field in items))
    .filter((field) => !known || !(field in known) || !!known[field].writable)
    .sort();
}
