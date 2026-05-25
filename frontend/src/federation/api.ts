import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import type { LabResultCard, PaginatedResponse, LabValuesResponse } from "./types";

const KEYS = {
  summary: (params: Record<string, unknown>) => ["labs", "summary", params] as const,
  values: (conceptCode: string, params: Record<string, unknown>) =>
    ["labs", "values", conceptCode, params] as const,
};

export interface LabSummaryParams {
  page?: number;
  pageSize?: number;
}

export function useLabResultsSummary(
  params?: LabSummaryParams,
  apiClient?: AxiosInstance,
  apiBasePath = "",
) {
  return useQuery({
    queryKey: KEYS.summary({ page: params?.page, pageSize: params?.pageSize }),
    queryFn: async () => {
      const resp = await apiClient!.get<PaginatedResponse<LabResultCard>>(
        `${apiBasePath}/lab-results/summary/`,
        {
          params: {
            page: params?.page ?? 1,
            page_size: params?.pageSize ?? 50,
          },
        },
      );
      return resp.data;
    },
    enabled: !!apiClient,
  });
}

export interface LabValuesParams {
  conceptCode: string;
  page?: number;
  pageSize?: number;
}

export function useLabValues(
  params: LabValuesParams,
  apiClient?: AxiosInstance,
  apiBasePath = "",
) {
  return useQuery({
    queryKey: KEYS.values(params.conceptCode, { page: params.page, pageSize: params.pageSize }),
    queryFn: async () => {
      const resp = await apiClient!.get<LabValuesResponse>(
        `${apiBasePath}/lab-results/values/`,
        {
          params: {
            concept_code: params.conceptCode,
            page: params.page ?? 1,
            page_size: params.pageSize ?? 50,
          },
        },
      );
      return resp.data;
    },
    enabled: !!apiClient && !!params.conceptCode,
  });
}

export interface UpdateMeasurementInput {
  measurementId: number;
  value?: number | null;
  value_string?: string | null;
  measured_at?: string;
  range_low?: number | null;
  range_high?: number | null;
}

export function useUpdateMeasurement(apiClient?: AxiosInstance, apiBasePath = "") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ measurementId, ...data }: UpdateMeasurementInput) => {
      await apiClient!.patch(`${apiBasePath}/lab-results/measurements/${measurementId}/`, data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["labs"] });
    },
  });
}

export function useDeleteMeasurement(apiClient?: AxiosInstance, apiBasePath = "") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (measurementId: number) => {
      await apiClient!.delete(`${apiBasePath}/lab-results/measurements/${measurementId}/`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["labs"] });
    },
  });
}
