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

export function stringsToOptions(strings: string[]): Option[] {
  return strings.map((s) => ({ value: s, label: s }));
}
