/** What an upload or a sync reports about samples that are not accepted rows yet (#2835). */
export interface PendingSamples {
  /** Rows a proposed mapping gave a concept. Sent only above zero. */
  provisional_samples?: number | null;
  /**
   * Readings stored with no approved mapping to answer for them. Sent only
   * above zero, and it includes the ones a proposed mapping then made
   * provisional, so what is still waiting is the remainder.
   */
  unresolved_samples?: number | null;
}

const positive = (count: number | null | undefined): number =>
  (typeof count === 'number' && count > 0 ? count : 0);

/**
 * What an upload or a sync adds to its result when some of what it stored is
 * not accepted yet: first the samples that are provisional until their mapping
 * is approved, then the ones stored and still waiting for a mapping. Empty
 * when there are neither.
 */
export function pendingClause({ provisional_samples, unresolved_samples }: PendingSamples): string {
  const provisional = positive(provisional_samples);
  const waiting = Math.max(positive(unresolved_samples) - provisional, 0);
  const parts: string[] = [];
  if (provisional > 0) {
    parts.push(`${provisional} provisional until ${provisional === 1 ? 'its' : 'their'} mapping is approved.`);
  }
  if (waiting > 0) parts.push(`${waiting} stored and waiting for a mapping.`);
  return parts.join(' ');
}
