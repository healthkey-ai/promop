export function prettyLabel(key: string) {
  return key
    .replace(/([A-Z])/g, ' $1')
    .replace(/_/g, ' ')
    .replace(/^./, (s) => s.toUpperCase());
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

  const positiveWord = text.includes('POSITIVE');
  const negativeWord = text.includes('NEGATIVE');
  if (positiveWord && negativeWord) return null;
  if (positiveWord) return 1;
  if (negativeWord) return -1;

  const positiveSign = text.endsWith('+') || /\+\s/.test(text);
  const negativeSign = text.endsWith('-') || /-\s/.test(text);
  if (positiveSign && negativeSign) return null;
  if (positiveSign) return 1;
  if (negativeSign) return -1;
  return null;
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
