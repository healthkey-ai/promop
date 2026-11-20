import { createContext, useContext } from 'react';

/**
 * Which fields of the record shown are proposals, not approved data, and how
 * sure the proposal is (a percentage). The server lays each proposal over a
 * field the approved record leaves empty and reports it in `provisional_fields`
 * (#2659); the page provides it here so a field can say so beside its label.
 */
export interface ProvisionalValue {
  /** Field name to confidence percentage. */
  marks: Record<string, number>;
  /** The patient's own page: wording must not read as a diagnosis. */
  patientMode: boolean;
}

const NONE: ProvisionalValue = { marks: {}, patientMode: false };

export const ProvisionalContext = createContext<ProvisionalValue>(NONE);

export function useProvisionalMark(name: string): { confidence: number; patientMode: boolean } | null {
  const { marks, patientMode } = useContext(ProvisionalContext);
  const confidence = marks[name];
  return typeof confidence === 'number' ? { confidence, patientMode } : null;
}
