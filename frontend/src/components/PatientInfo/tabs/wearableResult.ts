/** What an upload or a sync reports about the readings it stored that are not accepted yet (#2835). */
export interface PendingSamples {
  /** Of the readings this upload stored, those a proposed mapping gave a concept. Sent only above zero. */
  provisional_samples?: number | null;
  /** Of the readings this upload stored, those still waiting for a mapping. Sent only above zero. */
  unresolved_samples?: number | null;
}

const positive = (count: number | null | undefined): number =>
  (typeof count === 'number' && count > 0 ? count : 0);

/**
 * What an upload or a sync adds to its result when some of what it stored is
 * not accepted yet. The two counts are separate readings, never one inside the
 * other, so each is said as the server gives it. Worded for the person who
 * uploaded, who is the patient: no "mapping", no "code". Empty when there are
 * neither.
 */
export function pendingClause({ provisional_samples, unresolved_samples }: PendingSamples): string {
  const provisional = positive(provisional_samples);
  const waiting = positive(unresolved_samples);
  const parts: string[] = [];
  if (provisional > 0) {
    parts.push(`${provisional} ${provisional === 1 ? 'is' : 'are'} provisional: stored, and not yet confirmed.`);
  }
  if (waiting > 0) {
    parts.push(`${waiting} ${waiting === 1 ? 'is' : 'are'} stored and waiting to be matched to a standard code.`);
  }
  return parts.join(' ');
}
