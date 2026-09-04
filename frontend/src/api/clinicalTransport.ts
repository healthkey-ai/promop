import type { AxiosInstance } from 'axios';
import defaultApi from '@/api/axios';

/**
 * Which axios client the OMOP write helpers talk through.
 *
 * The standalone app owns its client and its `/api` base URL, so the helpers can
 * just import it. The federation remote cannot: the host injects the client —
 * carrying the host's auth, interceptors and origin — and supplies its own base
 * path. A remote that reached for the app's singleton would send the host's
 * clinical writes unauthenticated to whatever origin the remote happened to be
 * served from.
 *
 * The alternative was threading a client through `useWritableFields()` into every
 * tab, which is prop drilling through components that are shared between the two
 * apps precisely because they don't know which one they're in. This keeps that
 * knowledge at the edge, alongside the descriptor cache, which is already
 * module-level for the same reason.
 */

let client: AxiosInstance = defaultApi;
let basePath = '';

/** Called by the federation provider. Returns whether anything actually moved,
 *  so callers can drop caches keyed to the old deployment. */
export function setClinicalTransport(next: AxiosInstance, nextBasePath = ''): boolean {
  if (client === next && basePath === nextBasePath) return false;
  client = next;
  basePath = nextBasePath;
  return true;
}

/** Restores the standalone app's client. Used by tests and on provider unmount. */
export function resetClinicalTransport(): boolean {
  return setClinicalTransport(defaultApi, '');
}

export function clinicalClient(): AxiosInstance {
  return client;
}

/** `/v1/measurements/` under the standalone app, `<host base>/v1/measurements/`
 *  under federation. */
export function clinicalUrl(path: string): string {
  return `${basePath}${path}`;
}
