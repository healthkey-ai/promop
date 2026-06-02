import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import type { PatientInfoData } from "./patientInfoTypes";

const KEYS = {
  me: ["patient-info", "me"] as const,
};

export function usePatientInfoMe(apiClient?: AxiosInstance, apiBasePath = "") {
  return useQuery({
    queryKey: KEYS.me,
    queryFn: async () => {
      const resp = await apiClient!.get<PatientInfoData>(
        `${apiBasePath}/patient-info/me/`,
      );
      return resp.data;
    },
    enabled: !!apiClient,
  });
}

export function usePatchPatientInfo(apiClient?: AxiosInstance, apiBasePath = "") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (data: Record<string, unknown>) => {
      const resp = await apiClient!.patch(
        `${apiBasePath}/patient-info/me/`,
        data,
      );
      return resp.data;
    },
    onSuccess: (result: Record<string, unknown>) => {
      // Update the cache with the PATCH response fields rather than invalidating.
      // Invalidating would trigger a refetch that returns the DB state — which can
      // differ from the user's selection when OMOP post-save signals run between
      // the serializer.save() and the GET response (e.g. disease gets cleared by
      // refresh_patient_info and not restored correctly).  Merging the PATCH result
      // directly avoids a round-trip and keeps editedInfo in sync with the cache.
      queryClient.setQueryData(KEYS.me, (old: PatientInfoData | undefined) => {
        if (!old) return old;
        const { previous_values: _pv, ...fields } = result;
        return { ...old, patient_info: { ...old.patient_info, ...fields } };
      });
    },
  });
}
