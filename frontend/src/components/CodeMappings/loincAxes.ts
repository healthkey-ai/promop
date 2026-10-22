/**
 * The six LOINC axes, as the code-to-LOINC Suggest and the LOINC axes panel
 * see them. Order and labels mirror omop_core/mapping/loinc_axes.py.
 */
import type { SnomedCandidateInfo } from "./snomed";

export const LOINC_AXES = ["component", "property", "time_aspect", "system", "scale_type", "method_type"] as const;
export type LoincAxis = typeof LOINC_AXES[number];

export const LOINC_AXIS_LABELS: Record<LoincAxis, string> = {
  component: "Component",
  property: "Property",
  time_aspect: "Time",
  system: "System",
  scale_type: "Scale",
  method_type: "Method",
};

export type AxisBasis = "curator" | "source_code" | "fhir_coding" | "unit" | "value_type" | "specimen";

export const AXIS_BASIS_LABELS: Record<string, string> = {
  curator: "Set by curator",
  source_code: "From source code",
  fhir_coding: "From FHIR coding",
  unit: "From unit",
  value_type: "From value type",
  specimen: "From specimen",
};

export type AxisVerdict = "match" | "compatible" | "conflict" | "unknown";

/** Curator overrides: a string settles the axis, null lifts an inferred one. */
export type LoincAxisOverrides = Partial<Record<LoincAxis, string | null>>;

export interface SourceAxes {
  axes: Partial<Record<LoincAxis, { value: string; basis: AxisBasis | string; evidence: string }>>;
  hints: Partial<Record<LoincAxis, string>>;
  /** Set when the source is a lab-style test: LOINC candidates go first. */
  expected_vocabulary?: { vocabulary_id: string; reason: string };
}

export interface AxisExcludedCandidate {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  vocabulary_id: string;
  loinc_axes?: Partial<Record<LoincAxis, string>> | null;
  axis_match?: Partial<Record<LoincAxis, AxisVerdict>>;
  axis_conflicts?: LoincAxis[];
  retrieval?: string | string[];
  /** SNOMED entries only: why it was held back. LOINC entries are conflicts. */
  exclusion_reason?: "conflict" | "more_specific";
  /** SNOMED entries only: what it contradicts, e.g. ["Hierarchy"], ["Lineage", "Finding site"]. */
  snomed_conflicts?: string[];
  snomed?: SnomedCandidateInfo | null;
}

export interface AxisVariant {
  id: number | null;
  axis_key: string;
  axis_values: Partial<Record<LoincAxis, string>> & Record<string, string>;
  units: string[];
  occurrence_count: number;
  status: "proposed" | "approved";
  target_concept: { concept_id: number; concept_name: string; concept_code: string; vocabulary_id: string } | null;
  reviewed_at: string | null;
  seen?: boolean;
}

export interface LoincAxesPayload {
  mapping_id: number;
  applies: boolean;
  loinc_axes: LoincAxisOverrides;
  source_axes: SourceAxes;
  axis_labels?: Record<string, string>;
  axis_variant_count: number;
  variants: AxisVariant[];
  variant_notes: string[];
  repoint?: { rows_updated: number; persons_marked_stale: number; rows_collapsed: number };
  /** The variant's status after an axis-variants POST. */
  variant_status?: "proposed" | "approved";
}

export function axisLabel(axis: string, labels?: Record<string, string>): string {
  return labels?.[axis] ?? LOINC_AXIS_LABELS[axis as LoincAxis] ?? axis;
}

/** "Property SCnc · Scale Qn", or "Unit %" for keys the backend adds. */
export function describeAxisValues(values: Record<string, string>, labels?: Record<string, string>): string {
  const ordered = [
    ...LOINC_AXES.filter(axis => axis in values),
    ...Object.keys(values).filter(key => !(LOINC_AXES as readonly string[]).includes(key)),
  ];
  return ordered.map(key => {
    const label = key === "unit" ? "Unit" : axisLabel(key, labels);
    return `${label} ${values[key] === "" ? "(none)" : values[key]}`;
  }).join(" · ") || "—";
}

export const axisVariantTitle = (n: number) =>
  `This source code's units imply ${n} different LOINC axis sets; it may need one destination per variant.`;

/**
 * Drop retrieval candidates the axes (or SNOMED) filter hid, and follow the ranked event's
 * order (axes matched first) once one has arrived. Retrieval events stream
 * before the filter runs, so they still carry the conflicting candidates.
 */
export function applyAxisOrder<T extends { concept_id: number }>(
  merged: T[],
  ranked?: { candidates?: { concept_id: number }[]; axis_excluded?: { concept_id: number }[] },
): T[] {
  const hidden = new Set((ranked?.axis_excluded ?? []).map(c => c.concept_id));
  const kept = hidden.size ? merged.filter(c => !hidden.has(c.concept_id)) : merged;
  // SNOMED candidates are ranked too: exact, then same side, then nearest ancestor.
  if (!ranked?.axis_excluded && !(ranked?.candidates ?? []).some(c => "loinc_axes" in c || "snomed" in c)) return kept;
  const order = new Map((ranked?.candidates ?? []).map((c, index) => [c.concept_id, index]));
  return kept.map((c, index) => ({ c, index }))
    .sort((a, b) => (order.get(a.c.concept_id) ?? order.size + a.index) - (order.get(b.c.concept_id) ?? order.size + b.index))
    .map(({ c }) => c);
}
