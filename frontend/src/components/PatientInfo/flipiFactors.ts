export const FLIPI_FACTORS = [
  ['age', 'Age > 60 years'],
  ['stage', 'Ann Arbor stage III or IV'],
  ['hemoglobin', 'Hemoglobin < 12 g/dL'],
  ['nodalAreas', 'More than 4 involved nodal areas'],
  ['ldh', 'LDH above the laboratory upper limit of normal'],
] as const;
const legacy: Record<string, string> = {
  'Age ≥ 60': 'age', 'Stage III/IV': 'stage', 'Hgb < 12 g/dL': 'hemoglobin',
  'Nodal areas > 4': 'nodalAreas', 'Elevated LDH': 'ldh',
};

export function selectedFlipiFactors(value: unknown): string[] | null {
  if (value == null) return null;
  const items = Array.isArray(value) ? value : String(value).split(/[,;]/);
  const selected = new Set(items.map(item => legacy[String(item).trim()] ?? String(item).trim()));
  const allowed = new Set<string>(FLIPI_FACTORS.flatMap(([key, label]) => [key, label]));
  if ([...selected].some(key => key && !allowed.has(key))) return null;
  return FLIPI_FACTORS.filter(([key, label]) => selected.has(key) || selected.has(label)).map(([key]) => key);
}

