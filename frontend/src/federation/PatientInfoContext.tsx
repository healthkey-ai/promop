import { createContext, useContext } from "react";
import type { AxiosInstance } from "axios";
import type { FieldDescriptorMap } from "./patientInfoApi";

export interface PatientInfoContextValue {
  apiClient: AxiosInstance;
  apiBasePath: string;
  // Fields the server will persist. null = unknown (loading/unavailable) → do not gate editability.
  writableFields: Set<string> | null;
  // Full per-field descriptor (kind/reason), for the "(computed) + reason" display of non-editable
  // fields. null while loading / unavailable → render without reason hints.
  descriptor: FieldDescriptorMap | null;
}

export const PatientInfoContext = createContext<PatientInfoContextValue | null>(null);

export function usePatientInfoContext(): PatientInfoContextValue {
  const ctx = useContext(PatientInfoContext);
  if (!ctx) {
    throw new Error("usePatientInfoContext must be used inside <PatientInfoProvider>");
  }
  return ctx;
}
