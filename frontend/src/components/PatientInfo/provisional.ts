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
 *  Never mutates `info` or its marks; returns `info` itself when nothing changes. */
export function withoutMarks(
  info: Record<string, unknown>,
  fields: Iterable<string>,
): Record<string, unknown> {
  const marks = { ...marksOf(info) };
  let changed = false;
  for (const field of fields) {
    if (field in marks) { delete marks[field]; changed = true; }
  }
  return changed ? { ...info, provisional_fields: marks } : info;
}
