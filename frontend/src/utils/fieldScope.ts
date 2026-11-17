import type { DiseaseCategoryOption, DiseaseSet, FieldScoped } from "@/types/fieldScope";

/** The category every unrecognised disease falls into. It has no extension. */
export const OTHER_DISEASE = "Other";

/** The disease filter's value for the base record; no category has this code. */
export const BASE_FILTER = "__base__";

/**
 * The fields that say which disease a record has. They decide its profile,
 * so the server gives them no scope (`disease_profiles._PROFILE_KEYS`).
 */
const PROFILE_KEYS = new Set(["disease", "disease_slug", "disease_slugs"]);

export const canHaveScope = (fieldName: string): boolean => !PROFILE_KEYS.has(fieldName);

export const fieldDiseases = (field: FieldScoped): string[] => field.diseases ?? [];

/**
 * Whether a field is in the view the disease filter names, by the rule the
 * server applies to `?disease=<code>&set=extension|profile`
 * (`disease_field_sets.disease_set`): a disease's extension is the fields
 * that name it, and its profile is those plus the base record. `filter` is
 * "" for every field, `BASE_FILTER`, or a Disease Category code exactly as
 * the fields carry it.
 */
export function inDiseaseView(field: FieldScoped, filter: string, set: DiseaseSet): boolean {
  if (!filter) return true;
  const diseases = fieldDiseases(field);
  if (filter === BASE_FILTER) return diseases.length === 0;
  if (diseases.includes(filter)) return true;
  return set === "profile" && diseases.length === 0;
}

/**
 * The filter value a `?disease=` names: a category's code for its code or
 * label in any case, `BASE_FILTER` for Other (whose record is the base
 * record), and `null` for a value that names no category offered. The server
 * resolves the same parameter this way (`disease_profiles.profile_codes`).
 */
export function resolveDiseaseFilter(value: string, options: DiseaseCategoryOption[]): string | null {
  const text = value.trim();
  if (!text || text === BASE_FILTER) return text;
  const wanted = text.toLowerCase();
  if (wanted === OTHER_DISEASE.toLowerCase()) return BASE_FILTER;
  const match = options.find(
    (option) => option.value.toLowerCase() === wanted || option.label.toLowerCase() === wanted,
  );
  return match ? match.value : null;
}

/**
 * The Disease Category of each disease the patient editor has a tab for, by
 * the editor's disease type, as `disease_profiles.SEED_DISEASE_FIELDS` names
 * them. A record of any other disease has the base record only.
 */
const DISEASE_TYPE_CATEGORY: Record<string, string> = {
  breast: "BC", lymphoma: "FL", myeloma: "MM", cll: "CLL", mcl: "MCL",
};

export const diseaseCategoryCode = (diseaseType: string): string =>
  DISEASE_TYPE_CATEGORY[diseaseType] ?? "";

const LAB_FIELD_TABS = ["labs", "blood"];

/**
 * The field tabs whose added fields a patient tab shows (#721). The Labs tab
 * is where the editor shows blood counts, so a field placed on the mapping
 * page's Blood tab renders there too.
 */
export const addedFieldTabs = (tab: string): string[] => (tab === "labs" ? LAB_FIELD_TABS : [tab]);
