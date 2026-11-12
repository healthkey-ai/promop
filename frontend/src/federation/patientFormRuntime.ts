import { QueryClient } from '@tanstack/react-query';
import axios, { type AxiosInstance } from 'axios';
import { PatientFormStore } from './patientFormStore';

export interface PatientFormConnection {
  apiBase?: string;
  token?: string;
  impToken?: string;
  /** Patient/account context revision supplied by the host. No clinical data. */
  sessionKey?: string;
}
interface Runtime {
  client: AxiosInstance; queryClient: QueryClient;
  /** The connection's current store. A getter, so a renewal (the linked
   *  patient changed) is what mount helpers and retirement see next. */
  readonly store: PatientFormStore;
  references: number; retirement?: ReturnType<typeof setTimeout>; watchIdle?: () => void;
  /** Re-check the *current* store after a renewal swaps it under a retained
   *  runtime (the waiter's subscription belongs to the old store). */
  rearm?: () => void;
  /** Mount-level save-state watchers; re-target when a renewal swaps the store. */
  storeRenewals?: Set<() => void>;
}
const widgets = new Map<string, Runtime>();

/** A React key per store, so a fresh store remounts an editor's local state
 *  (dated drafts, open dialogs) instead of carrying it to the next patient. */
const storeIds = new WeakMap<PatientFormStore, number>();
let nextStoreId = 0;
export function patientFormStoreId(store: PatientFormStore): number {
  let id = storeIds.get(store);
  if (id === undefined) { id = ++nextStoreId; storeIds.set(store, id); }
  return id;
}
const clients = new WeakMap<AxiosInstance, Map<string, PatientFormStore>>();
/** Drafts lost when a store was retired, per client, until a mounted surface
 *  can tell the reader. The provider may remount between the switch and the
 *  notice, so this cannot live in its local state. */
const retiredDrafts = new WeakMap<AxiosInstance, number>();

/** Retire a store and remember any drafts that will never be sent. */
export function noteRetiredDrafts(client: AxiosInstance, store: PatientFormStore) {
  store.retire();
  const count = store.takeRetiredDrafts();
  if (count) retiredDrafts.set(client, (retiredDrafts.get(client) ?? 0) + count);
}

/** Consume the drafts lost since the last call, for a one-time notice. */
export function takeRetiredDraftCount(client: AxiosInstance) {
  const count = retiredDrafts.get(client) ?? 0;
  retiredDrafts.delete(client);
  return count;
}

export function patientFormForClient(client: AxiosInstance, base = '', sessionKey = '') {
  let stores = clients.get(client);
  if (!stores) { stores = new Map(); clients.set(client, stores); }
  // The client object identifies the connection; `sessionKey` identifies the
  // identity inside it. A host whose axios instance survives a sign-in change
  // must pass a new key, or a pending edit from the old session would drain
  // through the new credentials.
  const key = `${base}\u0000${sessionKey}`;
  let store = stores.get(key);
  // A retired store answers for a client that moved on to someone else; it is
  // never handed out again. Every cache reads through here (the provider, the
  // editor session, the widget runtime's `store`, renewals), so evicting on
  // lookup keeps all of them off it.
  if (store?.isRetired) {
    // A store retired by its own load (another patient answered) may still
    // hold drafts nobody has surfaced. Move the count before evicting it.
    noteRetiredDrafts(client, store);
    stores.delete(key);
    store = undefined;
  }
  if (!store) {
    // A new connection key on the same client and base means the host
    // switched sign-in or impersonation on that client. A store under an
    // earlier key would drain its queue through the new credentials; retire
    // it. The default key ('') is left alone: hosts that do not pass keys
    // may legitimately mount keyed and unkeyed views side by side.
    if (sessionKey) {
      for (const [otherKey, other] of stores) {
        const [otherBase, otherSession] = otherKey.split('\u0000');
        if (otherBase === base && otherSession && otherSession !== sessionKey) {
          noteRetiredDrafts(client, other);
          stores.delete(otherKey);
        }
      }
    }
    store = new PatientFormStore(client, base);
    stores.set(key, store);
  }
  return store;
}

/**
 * Replace the store for this connection with a fresh one, but only if it is
 * still `stale`: the store a caller saw answer for another patient. The old
 * store is not reused or retargeted; it keeps its own patient and queue. A
 * concurrent renewal that already replaced it wins, so two callers end up on
 * the same fresh store.
 */
export function renewPatientFormForClient(
  client: AxiosInstance, base: string, sessionKey: string, stale: PatientFormStore,
): PatientFormStore {
  const stores = clients.get(client);
  const key = `${base}\u0000${sessionKey}`;
  if (stores?.get(key) === stale) stores.delete(key);
  noteRetiredDrafts(client, stale);
  const fresh = patientFormForClient(client, base, sessionKey);
  // A retained runtime that was waiting on the retired store must watch the
  // fresh one; otherwise its 30 s clean-up never runs and the runtime (with
  // its credentials) is retained for the life of the page.
  for (const runtime of widgets.values()) {
    if (runtime.client !== client) continue;
    runtime.storeRenewals?.forEach((reattach) => reattach());
    if (runtime.references === 0) runtime.rearm?.();
  }
  return fresh;
}

/** Credentials are captured, never changed on a queue that still has edits.
 * Changing login or impersonation gets a different transport/store/cache. */
export function patientFormRuntime(connection: PatientFormConnection): Runtime {
  const apiBase = (connection.apiBase ?? '').replace(/\/+$/, '');
  const key = JSON.stringify([apiBase, connection.token, connection.impToken, connection.sessionKey]);
  let runtime = widgets.get(key);
  if (!runtime) {
    const client = axios.create({ headers: {
      ...(connection.token ? { Authorization: `Token ${connection.token}` } : {}),
      ...(connection.impToken ? { 'X-Imp-Token': connection.impToken } : {}),
    } });
    runtime = {
      client, queryClient: new QueryClient(), references: 0,
      get store() { return patientFormForClient(client, apiBase); },
    };
    widgets.set(key, runtime);
  }
  return runtime;
}

/** Bound credential/data retention after the last root leaves. A pending queue
 * keeps its immutable transport only until it drains; no polling or retargeting. */
export function retainPatientFormRuntime(connection: PatientFormConnection) {
  const runtime = patientFormRuntime(connection);
  ++runtime.references;
  clearTimeout(runtime.retirement);
  runtime.watchIdle?.();
  runtime.watchIdle = undefined;
  return () => {
    if (--runtime.references > 0) return;
    let watched = runtime.store;
    const retireWhenIdle = () => {
      const current = runtime.store;
      if (current !== watched) {
        // A renewal swapped the store while this runtime waited. The old
        // subscription will never see the new store's queue drain.
        runtime.watchIdle?.();
        watched = current;
        runtime.watchIdle = current.subscribe(retireWhenIdle);
      }
      const state = current.getSnapshot();
      if (state.saving || Object.keys(state.pending).length) return;
      runtime.watchIdle?.();
      runtime.watchIdle = undefined;
      runtime.rearm = undefined;
      runtime.retirement = setTimeout(() => {
        if (runtime.references) return;
        for (const [key, value] of widgets) if (value === runtime) widgets.delete(key);
        runtime.queryClient.clear();
      }, 30_000);
    };
    runtime.rearm = retireWhenIdle;
    runtime.watchIdle = watched.subscribe(retireWhenIdle);
    retireWhenIdle();
  };
}
