import { useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';

import { diseaseToDiseaseCode } from '@/api/therapyContext';
import {
  retractTherapyLine,
  setTherapyHistoryCompleteness,
  type EditableTherapyLine,
} from '@/api/therapyLines';
import TherapyLineDialog from '@/components/PatientInfo/TherapyLineDialog';
import { usePatientFormSnapshot } from './PatientFormContext';
import type { PatientFormStore } from './patientFormStore';

interface Props {
  store: PatientFormStore;
  readOnly?: boolean;
}

/** The therapy-line editor, mounted on its own by a host that only has a modal.
 *
 * Same editor as the Treatment tab (list + `TherapyLineDialog` + completeness),
 * so a host can offer it inside a trial-details dialog without embedding the
 * whole patient form. Values come from the shared patient-form store, which is
 * why authoring one line updates the host's row on the next `load()`.
 */
export default function TherapyLinesPanel({ store, readOnly = false }: Props) {
  const state = usePatientFormSnapshot(store);
  const [dialog, setDialog] = useState<{ mode: 'add' } | { mode: 'edit'; line: EditableTherapyLine } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const personId = state.personId ?? null;
  const lines = Array.isArray(state.values.lines_of_therapy)
    ? (state.values.lines_of_therapy as EditableTherapyLine[])
    : [];
  const rawCount = String(state.values.therapy_lines_count ?? '');
  const count = rawCount === '3+' ? 3 : (parseInt(rawCount) || 0);
  const nextLine = Math.max(count, ...lines.map((line) => line.line)) + 1;
  const diseaseCode = diseaseToDiseaseCode(state.values.disease);
  const complete = state.values.therapy_history_complete === true;
  const editable = !readOnly && state.writable && personId !== null;

  if (state.loading) return <p role="status" className="text-sm text-muted-foreground">Loading therapy lines…</p>;
  if (state.error) {
    return <div role="alert" className="text-sm text-red-600">
      {state.error} <button type="button" className="underline" onClick={() => void store.load()}>Retry</button>
    </div>;
  }

  const retract = async (line: EditableTherapyLine) => {
    if (typeof line.episode_id !== 'number') return;
    setBusy(true); setError('');
    try {
      await retractTherapyLine(line.episode_id);
      await store.load();
    } catch {
      setError('Could not remove that line. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const setComplete = async (next: boolean) => {
    if (personId === null) return;
    setBusy(true); setError('');
    try {
      await setTherapyHistoryCompleteness(personId, next);
      await store.load();
    } catch {
      setError('Could not update whether this history is complete.');
    } finally {
      setBusy(false);
    }
  };

  return <div className="space-y-3" data-therapy-lines>
    {editable && <button
      type="button"
      onClick={() => setDialog({ mode: 'add' })}
      className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted"
    >
      <Plus size={14} />
      Add therapy line
    </button>}

    {lines.length > 0 ? <ul className="divide-y divide-border rounded-md border border-border">
      {lines.map((line) => <li key={`${line.episode_id ?? 'no-episode'}-${line.line}`}
        className="flex items-center gap-3 px-3 py-2 text-sm">
        <div className="min-w-0 flex-1">
          <p className="font-medium">
            Line {line.line}:{' '}
            {line.composition_known === false
              ? (line.regimen && line.regimen !== 'Unknown' ? line.regimen : 'Drugs unknown')
              : (line.regimen || 'Unnamed regimen')}
            {line.intent && <span className="ml-2 font-normal text-muted-foreground">{line.intent}</span>}
            {line.outcome && <span className="ml-2 inline-block rounded bg-muted px-1.5 py-0.5 text-xs font-normal">{line.outcome}</span>}
          </p>
          <p className="truncate text-xs text-muted-foreground">
            {line.start_date || 'No start date'}
            {line.end_date ? ` to ${line.end_date}` : ' to present'}
            {line.discontinuation_reason && <span className="ml-2">Reason: {line.discontinuation_reason}</span>}
          </p>
        </div>
        {editable && <button
          type="button"
          disabled={typeof line.episode_id !== 'number'}
          onClick={() => setDialog({ mode: 'edit', line })}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Pencil size={13} />
          Edit
        </button>}
        {editable && <button
          type="button"
          disabled={typeof line.episode_id !== 'number' || busy}
          onClick={() => void retract(line)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-red-700 hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Trash2 size={13} />
          Remove
        </button>}
      </li>)}
    </ul> : <p className="text-sm text-muted-foreground">No therapy lines recorded.</p>}

    <label className="flex items-start gap-2 rounded border border-border px-3 py-2 text-sm">
      <input
        type="checkbox"
        checked={complete}
        disabled={!editable || busy}
        onChange={(event) => void setComplete(event.target.checked)}
        aria-label="Treatment history is complete"
        className="mt-0.5"
      />
      <span>
        This is the complete treatment history.
        {!complete && <span className="block text-xs text-muted-foreground">
          Until confirmed, the number of prior lines counts as a minimum.
        </span>}
      </span>
    </label>

    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

    {dialog && personId !== null && <TherapyLineDialog
      personId={personId}
      defaultLineNumber={nextLine}
      line={dialog.mode === 'edit' ? dialog.line : undefined}
      diseaseCode={diseaseCode}
      onClose={() => setDialog(null)}
      onAuthored={() => { setDialog(null); void store.load(); }}
    />}
  </div>;
}
