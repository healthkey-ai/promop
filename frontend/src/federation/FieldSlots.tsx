import { useCallback, useEffect, useMemo } from 'react';
import { createPortal } from 'react-dom';

import PatientFieldEditor from './PatientFieldEditor';
import { usePatientFormRenewals } from './editorSession';
import { PatientFormRenewContext } from './PatientFormContext';
import { renewPatientFormForClient, type patientFormRuntime } from './patientFormRuntime';
import type { PatientFormResult } from './patientFormStore';
import { hasNewSaveError } from './saveErrors';

interface FieldSlotsOptions {
  apiBase?: string;
  readOnly?: boolean;
  infoHref?: string;
  onPatientUpdated?: (result?: PatientFormResult) => unknown;
  onSaveError?: () => unknown;
}

/** The slots of one `mountFields` card, on the connection's current store.
 * When that store finds the client answering for another patient, Retry
 * renews it (`renewPatientFormForClient`) and the slots remount on the fresh
 * store; the old one keeps its own patient and sends nothing more. */
export default function FieldSlots({ runtime, opts, slots }: {
  runtime: ReturnType<typeof patientFormRuntime>; opts: FieldSlotsOptions; slots: HTMLElement[];
}) {
  const apiBase = (opts.apiBase ?? '').replace(/\/+$/, '');
  const { onPatientUpdated, onSaveError } = opts;
  // The connection's renewal generation, shared with every other surface on
  // this client: a renewal from a co-mounted form moves these slots too.
  const generation = usePatientFormRenewals(runtime.client, apiBase, '');
  const store = useMemo(() => {
    void generation; // a renewal replaced the cache entry; read it again
    return runtime.store;
  }, [runtime, generation]);
  const renew = useCallback(() => {
    // retire() publishes the "Not sent" errors before this render swaps the
    // store, so the subscription below tells the host once. Do not consume the
    // runtime count here: a later provider mount owns the visible notice.
    renewPatientFormForClient(runtime.client, apiBase, '', store);
  }, [runtime, apiBase, store]);
  useEffect(() => { void store.load(); }, [store]);
  useEffect(() => onPatientUpdated ? store.onUpdated(onPatientUpdated) : undefined,
    [store, onPatientUpdated]);
  useEffect(() => {
    let lastErrors = store.getSnapshot().errors;
    return store.subscribe(() => {
      const errors = store.getSnapshot().errors;
      if (hasNewSaveError(lastErrors, errors)) onSaveError?.();
      lastErrors = errors;
    });
  }, [store, onSaveError]);
  return <PatientFormRenewContext.Provider value={renew}>{slots.map((slot, index) => createPortal(
    <PatientFieldEditor store={store} name={slot.dataset.promopField!}
      label={slot.dataset.promopLabel}
      readOnly={opts.readOnly ?? true} infoHref={opts.infoHref} compact />,
    slot, `${index}:${generation}`,
  ))}</PatientFormRenewContext.Provider>;
}
