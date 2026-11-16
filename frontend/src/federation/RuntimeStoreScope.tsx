import { Fragment, useCallback, useEffect, useMemo, type ReactNode } from 'react';

import { usePatientFormRenewals } from './editorSession';
import { PatientFormContext, PatientFormRenewContext } from './PatientFormContext';
import {
  patientFormStoreId, renewPatientFormForClient, type patientFormRuntime,
} from './patientFormRuntime';
import type { PatientFormStore } from './patientFormStore';

/**
 * The widget runtime's current store, for a standalone mount (therapy lines,
 * genomics). Reading `runtime.store` once at mount left the editor on a store
 * that a renewal elsewhere on the runtime (a `mountFields` card, the full
 * form) had retired. This follows the connection's renewal generation, loads
 * each store it lands on, and offers `renew` to the editor's own Retry; a new
 * store remounts the editor, whose local state belongs to the old patient.
 */
export default function RuntimeStoreScope({ runtime, apiBase, children }: {
  runtime: ReturnType<typeof patientFormRuntime>;
  /** The mount's normalized base, the same one the runtime keys its store by. */
  apiBase: string;
  children: (store: PatientFormStore) => ReactNode;
}) {
  const generation = usePatientFormRenewals(runtime.client, apiBase, '');
  const store = useMemo(() => {
    void generation; // a renewal replaced the cache entry; read it again
    return runtime.store;
  }, [runtime, generation]);
  useEffect(() => { void store.load(); }, [store]);
  const renew = useCallback(() => {
    renewPatientFormForClient(runtime.client, apiBase, '', store);
  }, [runtime, apiBase, store]);
  return <PatientFormContext.Provider value={store}>
    <PatientFormRenewContext.Provider value={renew}>
      <Fragment key={patientFormStoreId(store)}>{children(store)}</Fragment>
    </PatientFormRenewContext.Provider>
  </PatientFormContext.Provider>;
}
