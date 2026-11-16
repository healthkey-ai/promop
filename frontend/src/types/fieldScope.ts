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

/** Which of a disease's two field sets: its own fields, or its full record. */
export type DiseaseSet = "extension" | "profile";
