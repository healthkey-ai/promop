import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AxiosInstance } from "axios";
import type { PatientInfoData } from "./patientInfoTypes";

// Keys are scoped by apiBasePath so a shared QueryClient mounting widgets against different APIs
// does not reuse the first instance's cached record/descriptor.
const meKey = (base: string) => ["patient-info", "me", base] as const;
const descriptorKey = (base: string) => ["patient-info", "descriptor", base] as const;

export function usePatientInfoMe(apiClient?: AxiosInstance, apiBasePath = "") {
  return useQuery({
    queryKey: meKey(apiBasePath),
    queryFn: async () => {
      const resp = await apiClient!.get<PatientInfoData>(
        `${apiBasePath}/patient-info/me/`,
      );
      return resp.data;
    },
    enabled: !!apiClient,
  });
}

// One per-field descriptor entry from /patient-info/descriptor/. `reason` explains why a
// non-editable field cannot be written here (computed / unmapped / set-on-Person / deferred), so the
// editor can show it inline as "(computed) + reason" instead of a bare read-only box.
export interface FieldDescriptorEntry {
  kind?: string;
  writable?: boolean;
  reason?: string;
  value_kind?: string;
}
export type FieldDescriptorMap = Record<string, FieldDescriptorEntry>;

interface DescriptorResponse {
  editable_fields?: string[];
  descriptor?: FieldDescriptorMap;
  write_enabled?: boolean;
}

// Single cached fetch of the descriptor; useWritableFields and usePatientDescriptor both read it.
function useDescriptorData(apiClient?: AxiosInstance, apiBasePath = "") {
  return useQuery({
    queryKey: descriptorKey(apiBasePath),
    queryFn: async () => {
      const resp = await apiClient!.get<DescriptorResponse>(
        `${apiBasePath}/patient-info/descriptor/`,
      );
      return resp.data;
    },
    enabled: !!apiClient,
    staleTime: Infinity,
  });
}

// The set of projection fields the server will actually persist (`editable_fields`), so the editor
// can keep fields with no write path (email, computed, unmapped, …) non-editable — a user never
// edits a field that silently would not save. This is the CB integration's OWN writable set, which
// is narrower than the descriptor's `writable` flag (email is descriptor-writable but deferred
// here). Null while loading or on failure → the editor does not gate (falls back to letting the
// server 4xx), so a descriptor outage never locks the whole form.
export function useWritableFields(apiClient?: AxiosInstance, apiBasePath = ""): Set<string> | null {
  const { data } = useDescriptorData(apiClient, apiBasePath);
  return data?.editable_fields ? new Set(data.editable_fields) : null;
}

// The full per-field descriptor (kind / writable / reason), for the "(computed) + reason" display of
// non-editable fields. Null while loading / on failure → the editor renders without the reason hints.
export function usePatientDescriptor(apiClient?: AxiosInstance, apiBasePath = ""): FieldDescriptorMap | null {
  const { data } = useDescriptorData(apiClient, apiBasePath);
  return data?.descriptor ?? null;
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
      queryClient.setQueryData(meKey(apiBasePath), (old: PatientInfoData | undefined) => {
        if (!old) return old;
        // `patient_name` is a top-level envelope field (the User's display name), not a projection
        // field, and `write` is the save summary — pull both out of the merge so the name updates
        // where the widget reads it and `write` doesn't pollute patient_info.
        const { previous_values: _pv, patient_name, write: _w, ...fields } =
          result as Record<string, unknown>;
        return {
          ...old,
          patient_name: (patient_name as string | undefined) ?? old.patient_name,
          patient_info: { ...old.patient_info, ...fields },
        };
      });
    },
  });
}
