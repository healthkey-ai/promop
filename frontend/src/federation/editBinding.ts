/** Which descriptor entry a trial-row name edits.
 *
 * A trial row is named by the catalog's own attribute (`hemoglobinLevel`), not
 * by the record's canonical key, and the descriptor carries the mapping as
 * `bindings`. A binding is not unique: a normalised read-model and the source
 * pair it is derived from can both declare it
 * (`hemoglobin_g_dl` and `hemoglobin_level`), and the pair is what the row
 * edits. Taking the first declaration made the editor depend on dictionary
 * order — one lab row edited its source, its sibling edited the derived column
 * — so the choice is made here, once, for the store and for the host's
 * pre-flight check.
 *
 * Rules, in order:
 *
 * 1. An exact key is its own answer. Asking for `hemoglobin_g_dl` means the
 *    canonical column and never silently redirects to the pair.
 * 2. A binding resolves to the editable source: `direct`, writable, targeting
 *    the patient record. Several such entries are ambiguous and resolve to
 *    nothing rather than to whichever came first; the host then keeps its own
 *    editor instead of writing a column the descriptor did not choose.
 * 3. With no source, a binding resolves as before, but a normalised read-model
 *    is the last resort — it is derived, so it must never win over a real entry.
 */
export interface BindingEntry {
  bindings?: string[];
  kind?: string | null;
  writable?: boolean;
  target?: string | null;
  normalised_read_model?: boolean;
}

/** An entry a row may edit: the source pair, not its derived projection. */
export function isEditableSource(entry: BindingEntry | undefined): boolean {
  return Boolean(entry)
    && entry!.kind === 'direct'
    && entry!.writable === true
    && entry!.target === 'patient_record'
    && entry!.normalised_read_model !== true;
}

/** The descriptor key that `name` edits, or undefined when it is ambiguous. */
export function resolveEditField<T extends BindingEntry>(
  fields: Record<string, T>,
  name: string,
): string | undefined {
  if (name in fields) return name;
  const candidates = Object.keys(fields).filter((key) => fields[key].bindings?.includes(name));
  if (!candidates.length) return undefined;

  const sources = candidates.filter((key) => isEditableSource(fields[key]));
  if (sources.length === 1) return sources[0];
  // Two writable sources behind one binding is a contract error, not a tie to
  // break: guessing would send a clinical value to the wrong column.
  if (sources.length > 1) return undefined;

  const declared = candidates.filter((key) => fields[key].normalised_read_model !== true);
  // Keep today's behaviour for the remaining shapes (a computed row with
  // editable inputs, an explicit-answer row), which only ever had one entry.
  return declared.length ? declared[0] : candidates[0];
}
