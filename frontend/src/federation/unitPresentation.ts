/** How a unit is shown to a reader (#2610). */

/** A stored UCUM code is not reader-facing wording (#2610). */
const UNIT_DISPLAY: Record<string, string> = {
  'mm[Hg]': 'mmHg',
  '/min': 'bpm',
  'bpm': 'bpm',
  '10*3/uL': '10³/µL',
  '10*6/uL': '10⁶/µL',
  '10*9/L': '10⁹/L',
  '10*12/L': '10¹²/L',
  'mL/min/1.73m2': 'mL/min/1.73m²',
  'mm/h': 'mm/hr',
};

/** Long spellings a reviewed label may use for a symbol unit. */
const UNIT_WORDS: Record<string, string[]> = {
  h: ['h', 'hr', 'hrs', 'hour', 'hours'],
  s: ['s', 'sec', 'secs', 'second', 'seconds'],
  mo: ['mo', 'month', 'months'],
};

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** The unit to show, or null when there is nothing a reader should see. */
export function displayUnitLabel(unit?: string | null): string | null {
  const raw = unit?.trim();
  if (!raw || /^\{.*\}$/.test(raw)) return null;
  return UNIT_DISPLAY[raw] ?? raw;
}

/** Whether the row's label already names its unit, so the unit is said once.
 *
 * A tab's reviewed label ("Systolic Blood Pressure (mmHg)", "Resting Heart
 * Rate (bpm)") is the source of truth; comparing the descriptor's code as well
 * as its display spelling keeps the rule working for either (#2610).
 */
export function labelNamesUnit(label: string, unit?: string | null): boolean {
  const raw = unit?.trim();
  if (!raw) return false;
  const haystack = label.toLowerCase();
  const candidates = new Set<string>([
    raw.toLowerCase(),
    (UNIT_DISPLAY[raw] ?? '').toLowerCase(),
    ...(UNIT_WORDS[raw.toLowerCase()] ?? []),
  ]);
  for (const candidate of candidates) {
    if (!candidate) continue;
    if (candidate.length <= 2 || !/[a-z0-9]/.test(candidate)) {
      // A one-character or symbol unit needs a boundary: `h` must not match
      // "hours" as a substring, and `%` must be its own token.
      if (new RegExp(`(^|[^a-z0-9])${escapeRegExp(candidate)}([^a-z0-9]|$)`).test(haystack)) {
        return true;
      }
    } else if (haystack.includes(candidate)) {
      return true;
    }
  }
  return false;
}

