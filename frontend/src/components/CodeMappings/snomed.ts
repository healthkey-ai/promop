/**
 * SNOMED destinations, as Suggest and the SNOMED evidence panel see them: a
 * candidate is judged by hierarchy, defining attributes and subsumption
 * against the source's reference concept (what the source means at least).
 */
import type { AxisExcludedCandidate } from "./loincAxes";

export type SnomedVerdict = "match" | "general" | "specific" | "conflict" | "unknown";
export type SnomedRelation = "exact" | "general" | "specific";
export type SnomedLaterality = "Left" | "Right" | "Right and left";

export interface SnomedAttribute {
  relationship_id: string;
  label: string;
  values: string[];
  verdict?: SnomedVerdict;
  primitive_gap?: true;
}

export interface SnomedCandidateInfo {
  hierarchy: string;
  semantic_tag: string;
  status: "Defined" | "Primitive" | null;
  attributes: SnomedAttribute[];
  laterality: SnomedLaterality | null;
  relation: SnomedRelation | null;
  levels: number | null;
  /** Exact because it restores the side the source names. */
  lateralised?: true;
  /** The source names this side, but the concept cannot carry it. */
  laterality_not_captured?: string;
}

export interface SnomedReference {
  concept_id: number;
  concept_name: string;
  concept_code: string;
  hierarchy?: string;
}

export interface SourceSnomed {
  references: SnomedReference[];
  reference_basis: "curator" | "source_code" | "maps_to" | null;
  hierarchy: { value: string; basis: string; evidence?: string } | null;
  laterality: { value: string; basis: string; evidence?: string } | null;
  attributes: { relationship_id: string; label: string; values: string[] }[];
}

/** Curator settings: key absent = inferred, null = do not constrain / no side. */
export interface SnomedSettings {
  reference_concept_id?: number;
  hierarchy?: string | null;
  laterality?: SnomedLaterality | null;
}

export interface SnomedAttributesPayload {
  mapping_id: number;
  applies: boolean;
  snomed_attributes: SnomedSettings;
  source: SourceSnomed | null;
  hierarchies: Record<string, string>;
  lateralities: SnomedLaterality[];
}

export const REFERENCE_BASIS_LABELS: Record<string, string> = {
  curator: "Chosen by curator",
  source_code: "Source code",
  maps_to: "Maps to",
};

export const SNOMED_BASIS_LABELS: Record<string, string> = {
  curator: "Set by curator",
  text: "Named in the source text",
  source_code: "From source code",
  maps_to: "From Maps to target",
  reference: "From reference concept",
};

export const basisLabel = (basis?: string | null): string =>
  basis ? SNOMED_BASIS_LABELS[basis] ?? REFERENCE_BASIS_LABELS[basis] ?? basis : "";

export const SNOMED_HELP =
  "The source means at least the reference concept. More general candidates are allowed and ranked lower; more specific ones need curator evidence.";

export function sameSnomedSettings(a: SnomedSettings, b: SnomedSettings): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof SnomedSettings>;
  return [...keys].every(key => (key in a) === (key in b) && a[key] === b[key]);
}

/** An excluded entry judged on SNOMED terms (LOINC entries carry none of these). */
export const isSnomedExcluded = (candidate: AxisExcludedCandidate): boolean =>
  candidate.snomed != null || candidate.exclusion_reason != null || candidate.snomed_conflicts != null;
