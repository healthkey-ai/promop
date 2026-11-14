import { createContext, useContext, useSyncExternalStore } from 'react';
import type { PatientFormStore } from './patientFormStore';

export const PatientFormContext = createContext<PatientFormStore | null>(null);
export const PatientFormReadOnlyContext = createContext(false);
/** Start a fresh store when the linked patient changed under the same client. */
export const PatientFormRenewContext = createContext<(() => void) | null>(null);
export const usePatientFormStore = () => useContext(PatientFormContext);
/** The form is shown without edits: a read-only mount or a caller without write
 *  access. Controls that write must honour it; controls that only navigate or
 *  display must not, so it is a per-control decision rather than a blanket
 *  `<fieldset disabled>`. */
export const usePatientFormReadOnly = () => useContext(PatientFormReadOnlyContext);
export function usePatientFormSnapshot(store: PatientFormStore) {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
}
