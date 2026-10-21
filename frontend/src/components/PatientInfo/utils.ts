export function prettyLabel(key: string) {
  return key
    .replace(/([A-Z])/g, ' $1')
    .replace(/_/g, ' ')
    .replace(/^./, (s) => s.toUpperCase());
}

export type DiseaseType = 'breast' | 'lymphoma' | 'mcl' | 'myeloma' | 'cll' | 'other';

// Mantle-cell must win over the broad lymphoma match, otherwise an MCL record
// is rendered with the follicular-lymphoma section (FLIPI/GELF).
export function diseaseTypeFor(value: unknown): DiseaseType {
  const d = String(value ?? '').toLowerCase();
  if (d.includes('breast')) return 'breast';
  if (d.includes('mantle') || d.includes('mcl')) return 'mcl';
  if (d.includes('lymphoma')) return 'lymphoma';
  if (d.includes('myeloma')) return 'myeloma';
  if (d.includes('cll') || d.includes('chronic lymphocytic')) return 'cll';
  return 'other';
}

export type Option = { value: unknown; label: string };

export function optionKey(v: unknown) {
  return String(v);
}

export type OptionInput = string | Option;

export function stringsToOptions(items: OptionInput[]): Option[] {
  return items.map((item) => (typeof item === 'string' ? { value: item, label: item } : item));
}

// The server derives HR/TNBC, but the widget also needs a local verdict while
// the user edits the receptor selects. Mirror the server's accepted spellings:
// words (`Positive`/`Negative`), receptor codes (`ER+`/`ER-`), and the offered
// expression-qualified forms (`ER+ with low expression`). Ambiguous values stay
// unknown so the UI never turns `ER+/-` into a false TNBC result.
export function receptorSign(value: unknown): -1 | 1 | null {
  const text = String(value ?? '').trim().toUpperCase();
  if (!text) return null;
  if (text.includes('/') || text.includes('EQUIVOCAL') || ['UNKNOWN', 'NOT ASSESSED'].includes(text)) {
    return null;
  }

  const signals = new Set<-1 | 1>();
  if (text.includes('POSITIVE')) signals.add(1);
  if (text.includes('NEGATIVE')) signals.add(-1);
  // Only a sign attached to a receptor token is a receptor result. A bare
  // assay score such as `IHC 2+` or `3+` must stay uninterpreted.
  for (const match of text.matchAll(/\b(?:ER|PR|HER2|HR)\s*([+-])/g)) {
    signals.add(match[1] === '+' ? 1 : -1);
  }
  if (signals.size !== 1) return null;
  return [...signals][0];
}

export function deriveTnbcStatus(er: unknown, pr: unknown, her2: unknown): boolean | null {
  const signals = [receptorSign(er), receptorSign(pr), receptorSign(her2)];
  if (signals.every((signal) => signal === -1)) return true;
  if (signals.some((signal) => signal === 1)) return false;
  return null;
}

// CB/PROMOP stores gender as the OMOP code (`M`/`F`/`U`) while the widget's select options are
// display labels. Normalise at the read boundary so the field renders instead of blank, and keep
// an unrecognised value verbatim rather than silently hiding it.
export function normalizeGender(value: unknown): unknown {
  if (value == null) return '';
  const raw = String(value).trim();
  switch (raw.toLowerCase()) {
    case 'm':
    case 'male':
      return 'Male';
    case 'f':
    case 'female':
      return 'Female';
    case 'o':
    case 'other':
      return 'Other';
    case 'u':
    case 'unknown':
      return 'Unknown';
    default:
      return raw;
  }
}

// PATCH the OMOP code, not the display label. PROMOP's resolver treats the
// literal `Other` as unknown (`U`), which would turn a user's choice into
// Unknown after refresh; `O` is the correct code.
export function genderCode(value: unknown): unknown {
  if (value == null) return '';
  const raw = String(value).trim();
  switch (raw.toLowerCase()) {
    case 'm':
    case 'male':
      return 'M';
    case 'f':
    case 'female':
      return 'F';
    case 'o':
    case 'other':
      return 'O';
    case 'u':
    case 'unknown':
      return 'U';
    default:
      return raw;
  }
}
