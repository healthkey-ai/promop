import { createContext, useContext } from "react";
import { isMemberOf } from "@/utils/fieldScope";

/**
 * The disease record a tab is showing (#2332): the Disease Category whose
 * extension its fields belong to, and a name for it. Only the disease tab
 * provides one, so a field of another tab is never held to a disease.
 */
export interface DiseaseProfile {
  category: string;
  label: string;
}

export const DiseaseProfileContext = createContext<DiseaseProfile | null>(null);

/**
 * What a control inside the disease tab does with its field: `member` fields
 * render as they are; a field the record's disease does not own renders
 * read-only, marked, when it holds a value (a value stored before a disease
 * change, or an imported fact: never cleared, never hidden silently), and
 * not at all when it holds none.
 */
export function useFieldMembership(
  descriptor: { diseases?: string[] } | undefined,
  hasValue: boolean,
): { member: boolean; hidden: boolean; label: string | null } {
  const profile = useContext(DiseaseProfileContext);
  if (!profile || isMemberOf(descriptor, profile.category)) return { member: true, hidden: false, label: null };
  return { member: false, hidden: !hasValue, label: profile.label };
}
