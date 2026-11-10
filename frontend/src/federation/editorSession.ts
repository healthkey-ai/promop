import { useEffect, useMemo } from 'react';
import type { AxiosInstance } from 'axios';

import { pushClinicalTransport } from '@/api/clinicalTransport';
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
}

/** Trim trailing slashes once; `/patient-info/me/` is appended verbatim. */
export function editorBasePath(connection: EditorConnection): string {
  return (connection.apiBasePath ?? '').replace(/\/+$/, '');
}

/**
 * The patient-form store for one connection, loaded on mount.
 *
 * The store is shared per (client, base) by `patientFormForClient`, so several
 * editors mounted for the same session read one queue: a therapy-line write is
 * visible to the field slot without a second read. `pushClinicalTransport` is
 * stack-scoped, so mounting and unmounting these editors cannot leave the
 * module-level clinical helpers pointed at a previous host, and the release
 * restores whatever was underneath.
 *
 * The transport is pushed in an effect, which runs after the children's
 * effects. That is safe for these editors: the therapy panel reads the
 * transport only from event handlers, and the genomics dialog takes the client
 * and URL explicitly. Anything added below that fetches during its own mount
 * effect would need the transport pushed before the first render instead.
 */
export function useEditorSession(connection: EditorConnection): PatientFormStore {
  const { apiClient } = connection;
  const base = editorBasePath(connection);
  const store = useMemo(
    () => patientFormForClient(apiClient, base),
    [apiClient, base],
  );
  useEffect(() => {
    const releaseTransport = pushClinicalTransport(apiClient, base);
    // The writability descriptor is cached per deployment, not per client.
    // A different connection may see different permissions on the same field.
    __resetWritableFieldsCache();
    void store.load();
    return () => {
      releaseTransport();
      __resetWritableFieldsCache();
    };
  }, [apiClient, base, store]);
  return store;
}
