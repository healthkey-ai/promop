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

/** A choice: the text it stores, or the text it stores with another one shown. */
export type OptionInput = string | { value: string; label?: string };

export function stringsToOptions(strings: OptionInput[]): Option[] {
  return strings.map((s) => (typeof s === 'string'
    ? { value: s, label: s }
    : { value: s.value, label: s.label ?? s.value }));
}
