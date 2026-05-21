import type { AxiosInstance } from "axios";
import type { QueryClient } from "@tanstack/react-query";

export interface LabsThemeTokens {
  colorPrimary: string;
  colorSuccess: string;
  colorWarning: string;
  colorDanger: string;
  colorMuted: string;
  fontFamily: string;
  borderRadius: string;
}

export interface LabsBaseProps {
  apiClient: AxiosInstance;
  apiBasePath?: string;
  queryClient?: QueryClient;
  className?: string;
  theme?: Partial<LabsThemeTokens>;
}

export interface LabResultsProps extends LabsBaseProps {
  selectedTest?: string;
  onNavigateToDetail?: (conceptCode: string) => void;
  onBack?: () => void;
  onResultDeleted?: (measurementId: number) => void;
}

export type LabValueStatus = "in_range" | "below" | "above" | "unknown";

export interface LabResultValue {
  measurement_id: number;
  value: number | null;
  value_string: string | null;
  unit: string | null;
  status: LabValueStatus;
  measured_at: string;
  range_low: number | null;
  range_high: number | null;
  source: string | null;
  lab_name: string | null;
  report_filename: string | null;
}

export interface LabResultCard {
  concept_id: number;
  concept_code: string;
  concept_name: string;
  vocabulary_id: string;
  category: string;
  values: LabResultValue[];
}

export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
