/**
 * A professional confirming a patient's provisional values (#2870).
 *
 * The server decides what can be confirmed: the record read lists it
 * (`provisional_confirm.confirmable`) and `POST /v1/patient-records/<id>/confirm-provisional/`
 * saves each field's *current proposal* as approved data, taking the value from its own overlay.
 * The page sends what it showed (`expected`) so a stale page cannot confirm a value it never saw.
 */
import api from '@/api/axios';
import { marksOf } from './provisional';

export interface ConfirmSkip { field: string; reason: string; detail?: string }
export interface ConfirmResult { person_id?: number; confirmed: string[]; skipped: ConfirmSkip[]; remaining: number }

/** The marked fields the server listed as confirmable, among those still marked (a field the
 *  user has just edited has lost its mark, and is theirs). Empty for anyone the server did not
 *  offer the confirmation to: a patient, an analyst, a record read that carries no such list. */
export function confirmableFields(info: Record<string, unknown> | null | undefined): string[] {
  const block = info?.provisional_confirm as { confirmable?: unknown } | undefined;
  if (!block || !Array.isArray(block.confirmable)) return [];
  const marks = marksOf(info);
  return (block.confirmable as string[]).filter((field) => field in marks).sort();
}

/** What a skip means to the person who pressed the button. */
export function skipText(item: ConfirmSkip): string {
  switch (item.reason) {
    case 'changed': return 'The proposed value changed since this page was loaded. Review the new value and confirm again.';
    case 'not_seen': return 'This proposal arrived after the page was loaded. Review it and confirm it on its own.';
    case 'invalid': return item.detail ? `The proposed value was not accepted: ${item.detail}` : 'The proposed value was not accepted.';
    case 'not_saved': return 'The value could not be saved.';
    case 'already_approved': return 'This field already holds an approved value.';
    case 'rejected': return 'The proposal was rejected earlier and is not shown.';
    case 'not_provisional': return 'This field no longer shows a proposal.';
    case 'disabled': return 'Provisional data is switched off.';
    default: return 'This value cannot be confirmed here.';
  }
}

/** Skips that mean the person should look again, as opposed to a value that clears with the
 *  field it follows (a calculation, a unit, a list). */
export const ACTIONABLE_SKIPS: ReadonlySet<string> = new Set([
  'changed', 'not_seen', 'invalid', 'not_saved', 'already_approved', 'rejected', 'not_provisional',
]);

export async function confirmProvisional(
  personId: string | number,
  body: { fields: string[] } | { all: true },
  expected: Record<string, unknown>,
): Promise<ConfirmResult> {
  const { data } = await api.post(`/v1/patient-records/${personId}/confirm-provisional/`, { ...body, expected });
  return data as ConfirmResult;
}
