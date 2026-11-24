import { useId } from "react";
import Field from "./Field";
import type { CustomPatientField } from "@/hooks/useCustomPatientFields";
import type { FieldDescriptors } from "@/hooks/useWritableFields";
import { fieldDiseases } from "@/utils/fieldScope";
import { addedMark } from "./provisional";

/**
 * What an input hands back, as the field's type stores it. A number box
 * yields text, and an emptied one clears the value.
 */
function entered(field: CustomPatientField, value: unknown): unknown {
  if (value === "" || value === null || value === undefined) return null;
  if (field.field_type === "number") {
    const number = Number(value);
    // Not a number yet ("-", "1e"): sent as typed, and refused by the server
    // with the reason, rather than silently becoming something else.
    return Number.isFinite(number) ? number : value;
  }
  return value;
}

/**
 * The fields added through Field Concept Mapping that belong on a patient
 * tab (#721), after the tab's own fields: those of `fields` placed on one of
 * `tabs` that are part of this record, which is the base record plus the
 * extension of each Disease Category in `diseases`.
 *
 * A field takes a value here when the server says this caller may write it
 * (#2669): `descriptors` is the writable-fields answer for this patient, and
 * a field it does not list as writable renders read-only with the reason,
 * as a column does. Without `onChange` every field is read-only. A computed
 * field shows its formula's result. Renders nothing when the tab has no
 * such field.
 */
export function CustomPatientFields({ fields, tabs, diseases, formData, descriptors, onChange }: {
  /** Every added field, from `useCustomPatientFields`. */
  fields: CustomPatientField[];
  tabs: string[];
  diseases: string[];
  formData: Record<string, unknown>;
  descriptors?: FieldDescriptors;
  /** An edit to one field's value; the editor saves it with the record. */
  onChange?: (fieldName: string, value: unknown) => void;
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
      <div className="grid gap-4 sm:grid-cols-2">
        {visible.map((field) => {
          const descriptor = descriptors?.[field.field_name];
          const computed = field.mode === "computed";
          const writable = !!onChange && !computed && !!descriptor?.writable;
          const reason = writable
            ? undefined
            : descriptor?.reason ?? (computed ? "Computed from its formula; not editable here." : undefined);
          return (
            <div key={field.id}>
              <Field
                label={field.unit ? `${field.display_name} (${field.unit})` : field.display_name}
                name={field.field_name}
                markName={addedMark(field.field_name)}
                type={field.field_type}
                value={values[field.field_name] ?? (field.field_type === "boolean" ? null : "")}
                onChange={(name, value) => onChange?.(name, entered(field, value))}
                disabled={!writable}
                readOnly={computed}
              />
              {reason && (
                <p className="mt-1 text-xs text-muted-foreground" data-testid={`reason-${field.field_name}`}>
                  {reason}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
