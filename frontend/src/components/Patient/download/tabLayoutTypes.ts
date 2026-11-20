/** One `<Section>` of a patient tab, as `scripts/tab-layout.mjs` reads it. */
export interface TabLayoutSection {
  title: string;
  /** The function the section is rendered in: DiseaseTab picks one per disease. */
  component: string;
  /** Rendered only under `!patientMode`. */
  staffOnly?: true;
  /** `[label, PatientRecord field]`, in the tab's order. */
  fields: [string, string][];
}

export type TabLayout = Record<string, TabLayoutSection[]>;
