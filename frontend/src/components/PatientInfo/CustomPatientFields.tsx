import { useId } from "react";
import FieldMappingLink from "./FieldMappingLink";
import type { CustomPatientField } from "@/hooks/useCustomPatientFields";
import { fieldDiseases } from "@/utils/fieldScope";

function displayValue(value: unknown, field: CustomPatientField) {
  if (value === undefined || value === null || value === "") return "Not recorded";
  if (field.field_type === "boolean") return value ? "Yes" : "No";
  return String(value) + (field.unit && typeof value === "number" ? ` ${field.unit}` : "");
}

/**
 * The fields added through Field Concept Mapping that belong on a patient
 * tab (#721), after the tab's own fields: those of `fields` placed on one of
 * `tabs` that are part of this record, which is the base record plus the
 * extension of each Disease Category in `diseases`.
 *
 * Read-only. A value is derived from the patient's OMOP facts for the
 * field's concept, or computed from its formula; nothing here writes one.
 * Renders nothing when the tab has no such field.
 */
export function CustomPatientFields({ fields, tabs, diseases, formData }: {
  /** Every added field, from `useCustomPatientFields`. */
  fields: CustomPatientField[];
  tabs: string[];
  diseases: string[];
  formData: Record<string, unknown>;
}) {
  const headingId = useId();
  const visible = fields.filter((field) => {
    if (!tabs.includes(field.tab)) return false;
    const scoped = fieldDiseases(field);
    return scoped.length === 0 || scoped.some((code) => diseases.includes(code));
  });
  if (visible.length === 0) return null;

  const values = (formData.custom_fields as Record<string, unknown> | undefined) || {};
  return (
    <section className="mt-8 border-t border-border pt-6" data-testid="custom-patient-fields" aria-labelledby={headingId}>
      <h3 id={headingId} className="mb-4 text-sm font-semibold">Additional fields</h3>
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
