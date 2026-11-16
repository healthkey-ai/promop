/**
 * Whose record a field is part of (#2331): the base record every patient has,
 * or the extension of the Disease Categories listed. Membership is the rule,
 * so `scope` is `disease` exactly when `diseases` is not empty.
 */
export type FieldScope = "base" | "disease";

/**
 * What every field mapping, field descriptor and custom field carries. Both
 * keys are optional because a federated host may still be served by an
 * instance older than #2331; a field without them reads as base.
 */
export interface FieldScoped {
  scope?: FieldScope;
  /** Disease Category codes, sorted; empty for a base field. */
  diseases?: string[];
}

/** One Disease Category from `/v1/disease-categories/`. */
export interface DiseaseCategoryOption {
  value: string;
  label: string;
}

/** The category every unrecognised disease falls into. It has no extension. */
export const OTHER_DISEASE = "Other";

export type DiseaseSet = "extension" | "profile";

/** The disease filter's value for the base record; no category has this code. */
export const BASE_FILTER = "__base__";

export const fieldDiseases = (field: FieldScoped): string[] => field.diseases ?? [];

/**
 * Whether a field is in the view the disease filter names, by the rule the
 * server applies to `?disease=<code>&set=extension|profile`
 * (`disease_field_sets.disease_set`): a disease's extension is the fields
 * that name it, and its profile is those plus the base record.
 */
export function inDiseaseView(field: FieldScoped, filter: string, set: DiseaseSet): boolean {
  if (!filter) return true;
  const diseases = fieldDiseases(field);
  if (filter === BASE_FILTER) return diseases.length === 0;
  if (diseases.includes(filter)) return true;
  return set === "profile" && diseases.length === 0;
}
