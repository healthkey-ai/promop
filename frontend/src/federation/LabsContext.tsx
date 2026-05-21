import { createContext, useContext } from "react";
import type { AxiosInstance } from "axios";

interface LabsContextValue {
  apiClient: AxiosInstance;
  apiBasePath: string;
}

const LabsContext = createContext<LabsContextValue | null>(null);

export function useLabsContext(): LabsContextValue {
  const ctx = useContext(LabsContext);
  if (!ctx) {
    throw new Error("useLabsContext must be used inside <LabsProvider>");
  }
  return ctx;
}

export { LabsContext };
