import { useEffect, useState } from "react";
import FieldMappingLink from "./FieldMappingLink";
import { clinicalClient, clinicalUrl } from "@/api/clinicalTransport";
import type { FieldScoped } from "@/types/fieldScope";
import { fieldDiseases } from "@/utils/fieldScope";

// `scope` and `diseases` say whose record the field is part of (#2331).
interface CustomField extends FieldScoped {
  id: number;
  field_name: string;
  display_name: string;
  tab: string;
  field_type: "text" | "number" | "date" | "boolean";
  mode: "editable" | "computed";
  unit: string;
}

function displayValue(value: unknown, field: CustomField) {
  if (value === undefined || value === null || value === "") return "Not recorded";
  if (field.field_type === "boolean") return value ? "Yes" : "No";
  return String(value) + (field.unit && typeof value === "number" ? ` ${field.unit}` : "");
}

/**
 * The fields added through Field Concept Mapping that belong on a patient
 * tab (#721), after the tab's own fields: those placed on one of `tabs`
 * that are part of this record, which is the base record plus the extension
 * of each Disease Category in `diseases`.
 *
 * Read-only. A value is derived from the patient's OMOP facts for the
 * field's concept, or computed from its formula; nothing here writes one.
 * Renders nothing when the tab has no such field.
 */
export function CustomPatientFields({ tabs, diseases, formData }: {
  tabs: string[];
  diseases: string[];
  formData: Record<string, unknown>;
}) {
  const [fields, setFields] = useState<CustomField[]>([]);
  useEffect(() => {
    let current = true;
    (async () => {
      let loaded: CustomField[] = [];
      try {
        const response = await clinicalClient().get(clinicalUrl("/v1/custom-patient-fields/"));
        // Only an array is usable here; a paginated envelope or anything else
        // (an HTML shell from a mis-targeted request) must not reach setFields.
        const body = response.data;
        loaded = Array.isArray(body) ? body : Array.isArray(body?.results) ? body.results : [];
      } catch {
        // The tab's own fields do not depend on these.
      }
      if (current) setFields(loaded);
    })();
    return () => { current = false; };
  }, []);

  const visible = fields.filter((field) => {
    if (!tabs.includes(field.tab)) return false;
    const scoped = fieldDiseases(field);
    return scoped.length === 0 || scoped.some((code) => diseases.includes(code));
  });
  if (visible.length === 0) return null;

  const values = (formData.custom_fields as Record<string, unknown> | undefined) || {};
  return (
    <section className="border-t border-border pt-6" data-testid="custom-patient-fields" aria-label="Additional fields">
      <h3 className="mb-4 text-sm font-semibold">Additional fields</h3>
      <dl className="grid gap-4 sm:grid-cols-2">
        {visible.map((field) => (
          <div key={field.id}>
            <dt className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
              <span>{field.display_name}</span>
              <FieldMappingLink name={field.field_name} label={field.display_name} />
            </dt>
            <dd className="mt-1 min-h-9 rounded border bg-muted/30 px-3 py-2 text-sm">
              {displayValue(values[field.field_name], field)}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
