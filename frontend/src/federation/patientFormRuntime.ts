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
  client: AxiosInstance; queryClient: QueryClient; store: PatientFormStore;
  references: number; retirement?: ReturnType<typeof setTimeout>; watchIdle?: () => void;
}
const widgets = new Map<string, Runtime>();
const clients = new WeakMap<AxiosInstance, Map<string, PatientFormStore>>();

export function patientFormForClient(client: AxiosInstance, base = '', sessionKey = '') {
  let stores = clients.get(client);
  if (!stores) { stores = new Map(); clients.set(client, stores); }
  // The client object identifies the connection; `sessionKey` identifies the
  // identity inside it. A host whose axios instance survives a sign-in change
  // must pass a new key, or a pending edit from the old session would drain
  // through the new credentials.
  const key = `${base}\u0000${sessionKey}`;
  let store = stores.get(key);
  if (!store) { store = new PatientFormStore(client, base); stores.set(key, store); }
  return store;
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
    runtime = { client, queryClient: new QueryClient(), store: patientFormForClient(client, apiBase), references: 0 };
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
    const retireWhenIdle = () => {
      const state = runtime.store.getSnapshot();
      if (state.saving || Object.keys(state.pending).length) return;
      runtime.watchIdle?.();
      runtime.watchIdle = undefined;
      runtime.retirement = setTimeout(() => {
        if (runtime.references) return;
        for (const [key, value] of widgets) if (value === runtime) widgets.delete(key);
        runtime.queryClient.clear();
      }, 30_000);
    };
    runtime.watchIdle = runtime.store.subscribe(retireWhenIdle);
    retireWhenIdle();
  };
}
