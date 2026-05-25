import { useLabsContext } from "./LabsContext";
import {
  useLabResultsSummary as _useLabResultsSummary,
  useLabValues as _useLabValues,
  useUpdateMeasurement as _useUpdateMeasurement,
  useDeleteMeasurement as _useDeleteMeasurement,
  type LabSummaryParams,
  type LabValuesParams,
} from "./api";

export function useLabResultsSummary(params?: LabSummaryParams) {
  const { apiClient, apiBasePath } = useLabsContext();
  return _useLabResultsSummary(params, apiClient, apiBasePath);
}

export function useLabValues(params: LabValuesParams) {
  const { apiClient, apiBasePath } = useLabsContext();
  return _useLabValues(params, apiClient, apiBasePath);
}

export function useUpdateMeasurement() {
  const { apiClient, apiBasePath } = useLabsContext();
  return _useUpdateMeasurement(apiClient, apiBasePath);
}

export function useDeleteMeasurement() {
  const { apiClient, apiBasePath } = useLabsContext();
  return _useDeleteMeasurement(apiClient, apiBasePath);
}
