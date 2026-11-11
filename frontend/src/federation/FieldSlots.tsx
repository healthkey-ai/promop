import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';

import PatientFieldEditor from './PatientFieldEditor';
import { PatientFormRenewContext } from './PatientFormContext';
import {
  renewPatientFormForClient, takeRetiredDraftCount, type patientFormRuntime,
} from './patientFormRuntime';
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
  const [store, setStore] = useState(() => runtime.store);
  const [generation, setGeneration] = useState(0);
  const renew = useCallback(() => {
    setStore(renewPatientFormForClient(runtime.client, apiBase, '', store));
    setGeneration((count) => count + 1);
    // The old store's unsent drafts will never be sent. Tell the host, which
    // has a slot for exactly this; there is no other surface on this card.
    if (takeRetiredDraftCount(runtime.client)) onSaveError?.();
  }, [runtime, apiBase, store, onSaveError]);
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
