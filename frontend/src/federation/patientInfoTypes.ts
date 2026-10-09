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
  /** Render the component's own "Health Profile" heading. Hosts that supply a
   *  page title of their own (see ht-one's clinical routes) pass false, so the
   *  screen does not show two. */
  showHeading?: boolean;
  onPatientUpdated?: (data: unknown) => void;
}

export interface PatientInfoData {
  patient_info: Record<string, unknown>;
  user: { id: number; email: string; name: string } | null;
  patient_name: string;
}
