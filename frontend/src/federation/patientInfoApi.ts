import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import type { PatientInfoData } from "./patientInfoTypes";
import { marksOf, withoutMarks } from "@/components/PatientInfo/provisional";

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
    onSuccess: (result: Record<string, unknown>, variables: Record<string, unknown>) => {
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
      let hadMarks = false;
      queryClient.setQueryData(KEYS.me, (old: PatientInfoData | undefined) => {
        if (!old) return old;
        const pi = result.patient_info as Record<string, unknown> | undefined;
        const merged = pi ? { ...old.patient_info, ...pi } : old.patient_info;
        // The PATCH response carries no `provisional_fields`, so the merge would keep
        // the marks the record had. A field the save wrote is the patient's now (an
        // approval or a rejected proposal): its mark goes here, and the others that
        // may have gone with it (a unit, a calculation, the disease group) are
        // settled by the read below (#2671).
        hadMarks = Object.keys(marksOf(old.patient_info)).length > 0;
        const written = Object.keys(variables).filter((f) => f !== 'rejected_proposals' && f !== 'patient_name');
        return {
          ...old,
          patient_info: withoutMarks(merged, written),
          ...(typeof result.patient_name === 'string'
            ? { patient_name: result.patient_name }
            : {}),
        };
      });
      // Only a record that showed proposals can have marks to settle; one that did not
      // keeps the round-trip-free merge above.
      if (hadMarks) queryClient.invalidateQueries({ queryKey: KEYS.me });
    },
  });
}
