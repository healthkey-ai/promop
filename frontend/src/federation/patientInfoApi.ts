import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import type { PatientInfoData } from "./patientInfoTypes";
import type { ConditionsAnswer } from '@/components/PatientInfo/PreexistingConditionsAnswer';

const KEYS = {
  me: ["patient-info", "me"] as const,
};

/** A dedicated answer write must also advance the shared GET/PATCH cache. */
export function useCommitConditionsAnswer() {
  const queryClient = useQueryClient();
  return async (answer: ConditionsAnswer) => {
    await queryClient.cancelQueries({ queryKey: KEYS.me });
    queryClient.setQueryData(KEYS.me, (old: PatientInfoData | undefined) => old ? {
      ...old, patient_info: { ...old.patient_info, ...answer },
    } : old);
  };
}

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
    onSuccess: (result: Record<string, unknown>, payload: Record<string, unknown>) => {
      // Update the cache with the PATCH response fields rather than invalidating.
      // Invalidating would trigger a refetch that returns the DB state — which can
      // differ from the user's selection when OMOP post-save signals run between
      // the serializer.save() and the GET response (e.g. disease gets cleared by
      // refresh_patient_info and not restored correctly).  Merging the PATCH result
      // directly avoids a round-trip and keeps editedInfo in sync with the cache.
      //
      // The /me/ PATCH response wraps the record under `patient_info`, exactly
      // like GET.  Unwrap before merging so the field values land at the right
      // nesting level — spreading the wrapper itself would bury them under a
      // spurious `patient_info.patient_info` key and leave the top-level values
      // stale, which is what caused the input to "clear" after save (#1083).
      queryClient.setQueryData(KEYS.me, (old: PatientInfoData | undefined) => {
        if (!old) return old;
        const pi = result.patient_info as Record<string, unknown> | undefined;
        const merged = pi ? { ...old.patient_info, ...pi } : old.patient_info;
        // Answers have their own write action. A late unrelated autosave result
        // must not resurrect the state from before that explicit write.
        for (const field of ['no_pre_existing_conditions', 'preexisting_conditions'] as const) {
          if (!(field in payload) && field in old.patient_info) merged[field] = old.patient_info[field];
        }
        return {
          ...old,
          patient_info: merged,
          ...(typeof result.patient_name === 'string'
            ? { patient_name: result.patient_name }
            : {}),
        };
      });
    },
  });
}
