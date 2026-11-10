import { useEffect, useMemo } from 'react';
import type { AxiosInstance } from 'axios';

import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { patientFormForClient } from './patientFormRuntime';
import type { PatientFormStore } from './patientFormStore';

/**
 * What a host supplies to mount a PROMOP editor: a client, not credentials.
 *
 * The standalone widget build (`widget.patient-info.tsx`) takes a DRF `token`
 * because its hosts (CB's `ui.v2`) cannot share a React tree and cannot hand
 * over an axios instance. A host that CAN share the tree — ht-phr, whose React
 * is the same major — has its own authenticated client with Firebase Bearer
 * interceptors, per-connection retries and an origin that is not PROMOP's.
 * Minting a second client from a token would either send the wrong auth scheme
 * or lose those interceptors, and would also fork the identity: the same
 * browser tab would hold two sessions for one patient.
 *
 * So the constructors below take that client and a base path. The base path is
 * the prefix before `/patient-info/me/` and `/v1/…`; it is `""` when the
 * client's own `baseURL` already ends at the API root (ht-phr's `/api`).
 */
export interface EditorConnection {
  /** The host's authenticated client. PROMOP never changes its headers. */
  apiClient: AxiosInstance;
  /** Prefix before `/patient-info/me/` and `/v1/…`. Defaults to none. */
  apiBasePath?: string;
  /**
   * Identity revision, when the host's client object outlives a sign-in or
   * impersonation change. The store is keyed by client + base + this value, so
   * a new identity never reads the previous session's pending drafts or writes
   * its edits into that queue.
   *
   * This is the weaker of the two fixes. A batch already queued belongs to the
   * store that captured the client, so it still drains through whatever
   * credentials that client now sends. A host that must keep an old batch on
   * the old credentials has to supply a NEW client object per identity; the
   * key is for a host that cannot do that and accepts the in-flight window.
   */
  connectionKey?: string;
}

/** Trim trailing slashes once; `/patient-info/me/` is appended verbatim. */
export function editorBasePath(connection: EditorConnection): string {
  return (connection.apiBasePath ?? '').replace(/\/+$/, '');
}

/**
 * The patient-form store for one connection, loaded on mount.
 *
 * The store is shared per (client, base, connectionKey) by
 * `patientFormForClient`, so several editors mounted for the same session read
 * one queue: a therapy-line write is visible to the field slot without a
 * second read.
 *
 * Nothing here touches the module-level clinical transport. The therapy panel
 * and its dialog take this connection explicitly, the genomics dialog takes the
 * client and URL explicitly, and the field slot writes through the store — so
 * two editors on one page cannot inherit each other's connection, and
 * unmounting one cannot leave a helper pointed at the wrong host.
 */
export function useEditorSession(connection: EditorConnection): PatientFormStore {
  const { apiClient, connectionKey } = connection;
  const base = editorBasePath(connection);
  const store = useMemo(
    () => patientFormForClient(apiClient, base, connectionKey ?? ''),
    [apiClient, base, connectionKey],
  );
  useEffect(() => {
    // The writability descriptor is cached per deployment, not per client. A
    // host that mounted the full form elsewhere may have cached another
    // deployment's permissions for the same person.
    __resetWritableFieldsCache();
    void store.load();
    return () => { __resetWritableFieldsCache(); };
  }, [store]);
  return store;
}
