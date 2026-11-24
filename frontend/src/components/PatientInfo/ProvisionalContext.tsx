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
  /** Present only for a professional who may confirm the record's proposals (#2870).
   *  The server decides which fields (`provisional_confirm` on the record read); the page
   *  only offers a control where it said yes. */
  confirm?: {
    /** The marked fields the server will confirm, from the record read. */
    confirmable: ReadonlySet<string>;
    /** The field being confirmed now (or `'*'` for all), disabling every control meanwhile. */
    busy: string | null;
    /** The last refusal or skip for a field, shown beside its control. */
    errors: Readonly<Record<string, string>>;
    /** Confirm one field: the server saves its current proposal as approved data. */
    onConfirm: (name: string) => void;
    /** A shown control tells the page its field's label, for the "Confirm all" question. */
    registerLabel?: (name: string, label: string) => void;
  };
}

const NONE: ProvisionalValue = { marks: {}, patientMode: false };

export const ProvisionalContext = createContext<ProvisionalValue>(NONE);

export function useProvisionalMark(name: string): { confidence: number; patientMode: boolean } | null {
  const { marks, patientMode } = useContext(ProvisionalContext);
  const confidence = marks[name];
  return typeof confidence === 'number' ? { confidence, patientMode } : null;
}

/** The control state for confirming `name`'s proposal, or null where none is offered:
 *  the viewer is not a professional the server allowed, the page is the patient's own,
 *  or the server did not list the field. */
export function useConfirmControl(name: string) {
  const { marks, patientMode, confirm } = useContext(ProvisionalContext);
  if (!confirm || patientMode || typeof marks[name] !== 'number' || !confirm.confirmable.has(name)) return null;
  return {
    busy: confirm.busy,
    error: confirm.errors[name] ?? null,
    onConfirm: () => confirm.onConfirm(name),
    registerLabel: confirm.registerLabel,
  };
}
