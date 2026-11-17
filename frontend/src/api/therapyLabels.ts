/** Reader-facing wording for a therapy line's stored values (#2613).
 *
 * Therapy intents arrive from the ETL as uppercase tokens
 * (`DEFINITIVE_LOCAL`, `METASTATIC_DISEASE_CONTROL`, …); a clinician must not
 * read a database value. The table is the reviewed wording; an underscore-joined
 * code it does not know is humanised, and anything else is shown unchanged so
 * clinical shorthand (`CR`, `VGPR`, `COVID-19`) is never rewritten.
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

/** A code-like token: uppercase words joined by underscores (`SOME_NEW_CODE`).
 *
 * Nothing else is rewritten. A short all-caps response (`CR`, `VGPR`, `PD`) is
 * the product's clinical shorthand, not a token to title-case — "Cr" reads like
 * creatinine — and a hyphenated phrase (`COVID-19`, `G-CSF`) must keep it.
 */
function isCodeToken(value: string): boolean {
  return /^[A-Z0-9]+(_[A-Z0-9]+)+$/.test(value);
}

/** `SOME_NEW_CODE` → "Some new code". */
function humaniseToken(token: string): string {
  const words = token.toLowerCase().replace(/_+/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : token;
}

/** The reviewed wording for a therapy intent, or null when it is absent. */
export function intentLabel(intent?: string | null): string | null {
  const token = intent?.trim();
  if (!token) return null;
  return INTENT_LABELS[token.toUpperCase()]
    ?? (isCodeToken(token) ? humaniseToken(token) : token);
}

/** The same rule for the line's other stored values (outcome, reason). */
export function valueLabel(value?: string | null): string | null {
  const text = value?.trim();
  if (!text) return null;
  return isCodeToken(text) ? humaniseToken(text) : text;
}
