import { createContext, useContext } from "react";
import type { AxiosInstance } from "axios";

export interface PatientInfoContextValue {
  apiClient: AxiosInstance;
  apiBasePath: string;
}

export const PatientInfoContext = createContext<PatientInfoContextValue | null>(null);

export function usePatientInfoContext(): PatientInfoContextValue {
  const ctx = useContext(PatientInfoContext);
  if (!ctx) {
    throw new Error("usePatientInfoContext must be used inside <PatientInfoProvider>");
  }
  return ctx;
}
