/**
 * Edits to added fields (#2669). An added field has no column: its value is
 * a key of the record's `custom_fields` object, which the editor holds whole.
 *
 * Only the keys the user changed are sent. The object the editor holds also
 * carries computed fields' results and other people's values as they were
 * when the record was read, and sending those back would ask the server to
 * put a stale value over a newer one.
 */
export const ADDED_FIELDS = "custom_fields";

const asValues = (values: unknown): Record<string, unknown> => (
  values && typeof values === "object" && !Array.isArray(values)
    ? values as Record<string, unknown>
    : {}
);

/** The named keys of an added-fields object; a missing one is a cleared value. */
export function pickAddedFields(values: unknown, names: Iterable<string>): Record<string, unknown> {
  const all = asValues(values);
  const picked: Record<string, unknown> = {};
  for (const name of names) picked[name] = all[name] ?? null;
  return picked;
}

/** An added-fields object with some keys replaced. */
export function mergeAddedFields(base: unknown, over: unknown): Record<string, unknown> {
  return { ...asValues(base), ...asValues(over) };
}
