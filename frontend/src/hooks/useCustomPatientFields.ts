import { useEffect, useState } from "react";
import { clinicalClient, clinicalUrl } from "@/api/clinicalTransport";
import type { FieldScoped } from "@/types/fieldScope";

/**
 * A field added through Field Concept Mapping (#721), as
 * `GET /v1/custom-patient-fields/` lists it. `scope` and `diseases` say
 * whose record the field is part of (#2331).
 */
export interface CustomPatientField extends FieldScoped {
  id: number;
  field_name: string;
  display_name: string;
  tab: string;
  field_type: "text" | "number" | "date" | "boolean";
  mode: "editable" | "computed";
  unit: string;
}

/**
 * The added fields' definitions, read once by the patient editor that shows
 * them and handed to each tab. Empty until they load, and when they cannot
 * be read: the tabs' own fields do not depend on them, and a host served by
 * an instance without the endpoint simply shows none.
 */
export function useCustomPatientFields(): CustomPatientField[] {
  const [fields, setFields] = useState<CustomPatientField[]>([]);
  useEffect(() => {
    let current = true;
    (async () => {
      let loaded: CustomPatientField[] = [];
      try {
        const response = await clinicalClient().get(clinicalUrl("/v1/custom-patient-fields/"));
        // Only an array is usable here; a paginated envelope or anything else
        // (an HTML shell from a mis-targeted request) must not reach setFields.
        const body = response.data;
        loaded = Array.isArray(body) ? body : Array.isArray(body?.results) ? body.results : [];
      } catch {
        // Shown as no added fields.
      }
      if (current) setFields(loaded);
    })();
    return () => { current = false; };
  }, []);
  return fields;
}
