import type { FieldDescriptor } from '@/hooks/useWritableFields';

const FACTORS = [
  ['large_mass', 'A nodal or extranodal mass > 7 cm'],
  ['multiple_large_nodes', 'At least 3 nodal areas, each > 3 cm'],
  ['b_symptoms', 'Lymphoma-related B symptoms'],
  ['compression', 'Organ compression or threatened organ function'],
  ['splenomegaly', 'Symptomatic splenomegaly'],
  ['effusion', 'Lymphoma-related pleural effusion or ascites'],
  ['circulating_cells', 'Circulating lymphoma cells > 5 × 10⁹/L'],
  ['cytopenia', 'Disease-related neutrophils < 1 × 10⁹/L or platelets < 100 × 10⁹/L'],
] as const;
export default function GelfAssessment({ value, descriptor, onChange }: {
  value: unknown; descriptor?: FieldDescriptor; onChange: (field: string, value: unknown) => void;
}) {
  const selected = value == null ? null : String(value).split(',').filter(Boolean);
  const write = (next: string[] | null) => onChange('gelf_criteria_options', next == null ? null : next.join(','));
  return <fieldset className="space-y-3 rounded-md border p-4" disabled={!descriptor?.writable}>
    <legend className="px-1 font-medium">GELF high tumor burden</legend>
    <p className="text-sm text-muted-foreground">Any selected criterion meets this GELF definition. Nodal count or marrow involvement alone is insufficient.</p>
    {FACTORS.map(([key, label]) => <label className="flex items-center gap-3 text-sm" key={key}>
      <input type="checkbox" checked={selected?.includes(key) ?? false} onChange={event => write(FACTORS.map(([id]) => id).filter(id => id === key ? event.target.checked : selected?.includes(id)))} />
      {label}
    </label>)}
    <p aria-live="polite" className="font-medium">GELF Criteria: {selected == null ? 'Not assessed' : selected.length ? 'Met' : 'Not Met'}</p>
    <div className="flex gap-4 text-sm">
      <button type="button" className="underline" onClick={() => write([])}>Record no GELF criteria</button>
      <button type="button" className="underline" onClick={() => write(null)}>Mark not assessed</button>
    </div>
  </fieldset>;
}
