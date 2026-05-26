import type { AxiosInstance } from "axios";
import type { QueryClient } from "@tanstack/react-query";
import type { LabsThemeTokens } from "./types";

export interface PatientInfoProps {
  apiClient: AxiosInstance;
  apiBasePath?: string;
  queryClient?: QueryClient;
  className?: string;
  theme?: Partial<LabsThemeTokens>;
  readOnly?: boolean;
  onPatientUpdated?: (data: unknown) => void;
}

export interface PatientInfoData {
  patient_info: Record<string, unknown>;
  user: { id: number; email: string; name: string } | null;
  patient_name: string;
}
