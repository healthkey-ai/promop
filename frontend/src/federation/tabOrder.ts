/**
 * Tab navigation for the federated patient form (#5610).
 *
 * The tab strip and the `?tab=` deep link both read this one list: a second
 * literal would drift on the next reorder and leave the deep link pointing at
 * a panel the label no longer names. Genomics sits right after Labs and right
 * before Behavior, as in the native `PatientDetail` view.
 */
export function federatedTabKeys(showDiseaseTab: boolean, federated?: boolean): string[] {
  return ['general', ...(showDiseaseTab ? ['disease'] : []), 'treatment', 'labs', 'genomics',
    'behavior', ...(federated ? [] : ['wearable']), 'history'];
}

/** Legacy `?tab=` spellings that still open the named tab. */
export const TAB_ALIASES: Record<string, string> = {
  blood: 'labs',
  breast_cancer: 'disease',
  multiple_myeloma: 'disease',
  follicular_lymphoma: 'disease',
  chronic_lymphocytic_leukemia: 'disease',
  mantle_cell_lymphoma: 'disease',
};

/** Index of the requested `?tab=` value in the strip, or -1 when it names no tab. */
export function tabIndexFor(requested: string | null, showDiseaseTab: boolean, federated?: boolean): number {
  if (!requested) return -1;
  return federatedTabKeys(showDiseaseTab, federated).indexOf(TAB_ALIASES[requested] ?? requested);
}
