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
  const { apiClient } = useLabsContext();
  return _useLabResultsSummary(params, apiClient);
}

export function useLabValues(params: LabValuesParams) {
  const { apiClient } = useLabsContext();
  return _useLabValues(params, apiClient);
}

export function useUpdateMeasurement() {
  const { apiClient } = useLabsContext();
  return _useUpdateMeasurement(apiClient);
}

export function useDeleteMeasurement() {
  const { apiClient } = useLabsContext();
  return _useDeleteMeasurement(apiClient);
}
