import type { FieldDescriptor } from '@/hooks/useWritableFields';

import { FLIPI_FACTORS, selectedFlipiFactors } from './flipiFactors';

export default function FlipiAssessment({ value, recordedScore, descriptor, onChange }: {
  value: unknown; recordedScore?: unknown; descriptor?: FieldDescriptor;
  onChange: (field: string, value: unknown) => void;
}) {
  const selected = selectedFlipiFactors(value);
  const score = selected?.length;
  const risk = score == null ? null : score <= 1 ? 'Low' : score === 2 ? 'Intermediate' : 'High';
  const write = (keys: string[] | null) => onChange('flipi_score_options', keys == null ? null : keys.join(','));
  return (
    <fieldset className="space-y-3 rounded-md border p-4" disabled={!descriptor?.writable}>
      <legend className="px-1 font-medium">FLIPI-1 assessment</legend>
      <p className="text-sm text-muted-foreground">Select the risk factors present at diagnosis. Each selected factor adds 1 point.</p>
      {FLIPI_FACTORS.map(([key, label]) => (
        <label key={key} className="flex items-center gap-3 text-sm">
          <input type="checkbox" checked={selected?.includes(key) ?? false}
            onChange={event => write(FLIPI_FACTORS.map(([id]) => id).filter(id => id === key ? event.target.checked : selected?.includes(id)))} />
          <span>{label}</span><span className="ml-auto">{selected?.includes(key) ? '+1' : '+0'}</span>
        </label>
      ))}
      <div className="rounded bg-muted p-3" aria-live="polite">
        <p className="font-medium">FLIPI Score: {score == null ? (recordedScore == null ? 'Not assessed' : `${String(recordedScore)} / 5 — historical`) : `${score} / 5 — ${risk} risk`}</p>
        {score == null && recordedScore != null && (
          <p className="text-sm">Factor checklist unavailable; the historical score has not been recalculated.</p>
        )}
        {score != null && <p className="text-sm">{FLIPI_FACTORS.map(([key]) => selected?.includes(key) ? '1' : '0').join(' + ')} = {score}</p>}
        <p className="text-xs text-muted-foreground">0–1: Low · 2: Intermediate · 3–5: High. FLIPI describes prognosis; GELF assesses tumor burden.</p>
      </div>
      <div className="flex gap-4 text-sm">
        <button type="button" className="underline" onClick={() => write([])}>Record no risk factors</button>
        <button type="button" className="underline" onClick={() => write(null)}>Mark not assessed</button>
      </div>
    </fieldset>
  );
}
