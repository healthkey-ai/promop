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
