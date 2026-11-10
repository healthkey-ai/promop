/** The descriptor schema this build understands. */
export const SUPPORTED_DESCRIPTOR_SCHEMA_VERSION = 1;

export interface ParsedDescriptor {
  /** Field entries only: the reserved envelope is never one. */
  fields: Record<string, Record<string, unknown>>;
  schemaVersion: number | null;
  /** Why this build cannot honour the contract, when it cannot. */
  error?: string;
}

/**
 * Read the descriptor envelope before any editor trusts it.
 *
 * The mapping is additive by design: `__descriptor__` carries the producer's
 * `schema_version`, and a reader that does not know it must not guess
 * (`docs/writable-field-descriptor.md`). A pre-envelope response is the legacy
 * contract and is accepted; a newer or malformed version fails closed with a
 * sentence the reader can act on, instead of writing under old semantics.
 */
export function parseWritableDescriptor(
  payload: unknown,
  supported = SUPPORTED_DESCRIPTOR_SCHEMA_VERSION,
): ParsedDescriptor {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return {
      fields: {},
      schemaVersion: null,
      error: 'The patient record did not describe its editable fields. Reload to try again.',
    };
  }
  const record = payload as Record<string, unknown>;
  const fields: Record<string, Record<string, unknown>> = {};
  for (const [name, entry] of Object.entries(record)) {
    if (name.startsWith('__')) continue;
    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
      fields[name] = entry as Record<string, unknown>;
    }
  }

  const meta = record.__descriptor__;
  if (meta === undefined) {
    // Legacy descriptor: no envelope, no version to check.
    return { fields, schemaVersion: null };
  }
  const version = (meta as { schema_version?: unknown } | null)?.schema_version;
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    return {
      fields: {},
      schemaVersion: null,
      error: 'This version of the patient form cannot read the record contract. Update the app to edit.',
    };
  }
  if (version > supported) {
    return {
      fields: {},
      schemaVersion: version,
      error: `This version of the patient form is too old for the record contract (v${version}). Update the app to edit.`,
    };
  }
  return { fields, schemaVersion: version };
}
