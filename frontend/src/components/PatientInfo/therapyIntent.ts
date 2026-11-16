/** Reader-facing wording for a therapy line's stored values (#2613).
 *
 * Therapy intents arrive from the ETL as uppercase tokens
 * (`DEFINITIVE_LOCAL`, `METASTATIC_DISEASE_CONTROL`, …); a clinician must not
 * read a database value. The table is the reviewed wording; a token it does
 * not know is still humanised rather than printed raw, so a new code cannot
 * leak a second time.
 */
const INTENT_LABELS: Record<string, string> = {
  DEFINITIVE_LOCAL: 'Definitive local',
  ADJUVANT: 'Adjuvant',
  NEOADJUVANT: 'Neoadjuvant',
  INDUCTION: 'Induction',
  METASTATIC_DISEASE_CONTROL: 'Metastatic disease control',
  CONSOLIDATION: 'Consolidation',
  MAINTENANCE: 'Maintenance',
  SALVAGE: 'Salvage',
  PALLIATIVE: 'Palliative',
  PALLIATIVE_SYMPTOM: 'Palliative or symptom control',
  CURATIVE: 'Curative',
};

/** A token the table does not know: `SOME_NEW_CODE` → "Some new code". */
function humaniseToken(token: string): string {
  const words = token.toLowerCase().replace(/[_-]+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : token;
}

/** Whether a stored value is a code rather than an already-written phrase. */
function isToken(value: string): boolean {
  return /^[A-Z0-9_-]+$/.test(value);
}

/** The reviewed wording for a therapy intent, or null when it is absent. */
export function intentLabel(intent?: string | null): string | null {
  const token = intent?.trim();
  if (!token) return null;
  return INTENT_LABELS[token.toUpperCase()] ?? (isToken(token) ? humaniseToken(token) : token);
}

/** The same rule for the line's other stored values (outcome, reason). */
export function valueLabel(value?: string | null): string | null {
  const text = value?.trim();
  if (!text) return null;
  return isToken(text) ? humaniseToken(text) : text;
}
