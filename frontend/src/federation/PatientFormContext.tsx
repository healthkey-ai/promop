import { createContext, useContext, useSyncExternalStore } from 'react';
import type { PatientFormStore } from './patientFormStore';

export const PatientFormContext = createContext<PatientFormStore | null>(null);
export const PatientFormReadOnlyContext = createContext(false);
export const usePatientFormStore = () => useContext(PatientFormContext);
export function usePatientFormSnapshot(store: PatientFormStore) {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
}
