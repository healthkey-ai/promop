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

/** An option may arrive as a bare display string or with a stored value that
 *  differs from its label (a host catalog stores codes, patients read titles). */
export type OptionInput = string | { value: string; label?: string };

export function optionsToObjects(options: OptionInput[]): Option[] {
  return options.map((option) => typeof option === 'string'
    ? { value: option, label: option }
    : { value: option.value, label: option.label ?? option.value });
}
