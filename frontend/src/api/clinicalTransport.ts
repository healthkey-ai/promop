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

/** Mounted widgets that asked for a transport, oldest first. Popping one must
 *  put the previous owner back: a page can have several patient widgets, and the
 *  plain reset used to yank the transport out from under the ones still open. */
const owners: Array<{ client: AxiosInstance; basePath: string }> = [];

function applyTopOwner(): boolean {
  const top = owners[owners.length - 1];
  return top ? setClinicalTransport(top.client, top.basePath) : resetClinicalTransport();
}

/** Register a transport owner and get the matching release. Last in wins; the
 *  release restores whatever was active underneath. */
export function pushClinicalTransport(next: AxiosInstance, nextBasePath = ''): () => void {
  const owner = { client: next, basePath: nextBasePath };
  owners.push(owner);
  setClinicalTransport(owner.client, owner.basePath);
  return () => {
    const index = owners.lastIndexOf(owner);
    if (index === -1) return;
    owners.splice(index, 1);
    applyTopOwner();
  };
}

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
