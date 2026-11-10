import { useEffect, useMemo, useState } from 'react';
import type { AxiosInstance } from 'axios';

import { editorBasePath, type EditorConnection } from './editorSession';

/** One descriptor entry, as far as bindings matter here. */
interface DescriptorEntry {
  bindings?: string[];
}
interface Answer {
  client: AxiosInstance;
  base: string;
  key: string;
  descriptor: Record<string, DescriptorEntry>;
}

export interface PromopFieldBindings {
  /** False until the descriptor answered (or failed) for THIS connection.
   *  While false the host keeps its own editor rather than drawing PROMOP's
   *  "no binding" notice. */
  ready: boolean;
  /** Whether PROMOP can resolve this trial-row name to a descriptor key. */
  canResolve: (name: string) => boolean;
  /** The descriptor entry a trial-row name resolves to (canonical key or a
   *  declared binding), for callers that need its `kind`. */
  entryFor: (name: string) => Record<string, unknown> | undefined;
}

/**
 * Which trial-row field names PROMOP's editor can actually resolve.
 *
 * The trial catalog names a row by its own attribute (`hemoglobinLevel`), not
 * by the record's canonical key (`hemoglobin_g_dl`). The descriptor carries the
 * mapping as `bindings`, so the host can ask before it hands the row over: a
 * row PROMOP cannot resolve must return `null` from `renderFieldEditor`, which
 * keeps EXACT's own editor.
 *
 * Same endpoint and `X-Patient-Form` header the store itself uses, through the
 * host's client — not the metadata-only `/patient-info/descriptor/`, which is a
 * different contract and would hide editors the widget can render.
 *
 * The answer carries the connection it was read for. A new client or base is a
 * new identity/session, so the previous answer stops counting immediately
 * without clearing state from an effect: readiness is derived, not stored.
 */
export function usePromopFieldBindings(
  connection: EditorConnection,
  options: { enabled?: boolean } = {},
): PromopFieldBindings {
  const { apiClient, connectionKey } = connection;
  const base = editorBasePath(connection);
  const key = connectionKey ?? '';
  const enabled = options.enabled ?? true;
  const [answer, setAnswer] = useState<Answer | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const controller = new AbortController();
    apiClient
      .get<Record<string, DescriptorEntry>>(`${base}/v1/patient-records/writable-fields/`, {
        headers: { 'X-Patient-Form': '1' },
        signal: controller.signal,
      })
      .then((response) => {
        if (alive) setAnswer({ client: apiClient, base, key, descriptor: response.data ?? {} });
      })
      .catch((error: { name?: string }) => {
        // A failed descriptor must fall back to the host's editor, never to
        // PROMOP's "no binding" notice.
        if (alive && error?.name !== 'CanceledError') {
          setAnswer({ client: apiClient, base, key, descriptor: {} });
        }
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [apiClient, base, key, enabled]);

  const current = enabled && answer?.client === apiClient && answer.base === base
    && answer.key === key
    ? answer.descriptor
    : null;
  const resolvable = useMemo(() => {
    const entries = new Map<string, Record<string, unknown>>();
    for (const [name, entry] of Object.entries(current ?? {})) {
      entries.set(name, entry as Record<string, unknown>);
      for (const alias of entry.bindings ?? []) {
        entries.set(alias, entry as Record<string, unknown>);
      }
    }
    return entries;
  }, [current]);

  return {
    ready: current !== null,
    canResolve: (name: string) => resolvable.has(name),
    entryFor: (name: string) => resolvable.get(name),
  };
}
