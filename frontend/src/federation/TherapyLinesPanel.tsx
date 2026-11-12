import { useContext, useId, useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';

import { diseaseToDiseaseCode } from '@/api/therapyContext';
import {
  retractTherapyLine,
  setTherapyHistoryCompleteness,
  type EditableTherapyLine,
  type TherapyConnection,
} from '@/api/therapyLines';
import TherapyLineDialog from '@/components/PatientInfo/TherapyLineDialog';
import { PatientFormRenewContext, usePatientFormSnapshot } from './PatientFormContext';
import type { PatientFormStore } from './patientFormStore';

interface Props {
  store: PatientFormStore;
  readOnly?: boolean;
  /** Called after a mutation lands, so the host can reload the record and
   *  invalidate whatever query rendered the row. `store.load()` alone does not:
   *  it publishes a snapshot but never reaches the store's `onUpdated` set. */
  onChanged?: () => void;
  /** Which connection the line endpoints talk through. Omitted, the panel uses
   *  the module-level clinical transport the standalone app owns. A federated
   *  host passes its own client so two mounted editors cannot cross. */
  connection?: TherapyConnection;
}

/** The therapy-line editor, mounted on its own by a host that only has a modal.
 *
 * Same editor as the Treatment tab (list + `TherapyLineDialog` + completeness),
 * so a host can offer it inside a trial-details dialog without embedding the
 * whole patient form. Values come from the shared patient-form store, which is
 * why authoring one line updates the host's row on the next `load()`.
 */
export default function TherapyLinesPanel({ store, readOnly = false, onChanged, connection }: Props) {
  const state = usePatientFormSnapshot(store);
  const renew = useContext(PatientFormRenewContext);
  const [dialog, setDialog] = useState<{ mode: 'add' } | { mode: 'edit'; line: EditableTherapyLine } | null>(null);
  const [busy, setBusy] = useState(false);
  const completeId = useId();
  const [error, setError] = useState('');

  const personId = state.personId ?? null;
  const lines = Array.isArray(state.values.lines_of_therapy)
    ? (state.values.lines_of_therapy as EditableTherapyLine[])
    : [];
  // The authoring line counts the lines the record actually carries, not the
  // derived `therapy_lines_count`: an imported patient can have a non-zero
  // count with no line rows, and counting the aggregate would open "Record a
  // line" at 2 and load second-line regimens for the first recorded line
  // (#1969 follow-up). The planned-regimen catalog still uses the aggregate;
  // that lives in `TreatmentTab`.
  const lastRecordedLine = lines.reduce(
    (max, line) => Math.max(max, Number(line.line) || 0), 0);
  const nextLine = lastRecordedLine + 1;
  const diseaseCode = diseaseToDiseaseCode(state.values.disease);
  const complete = state.values.therapy_history_complete === true;
  const editable = !readOnly && state.writable && personId !== null;

  if (state.loading) return <p role="status" className="text-sm text-muted-foreground">Loading therapy lines…</p>;
  if (state.error) {
    return <div role="alert" className="text-sm text-red-600">
      {state.error} <button type="button" className="underline" onClick={() => {
        // The client now speaks for another patient: reloading this store
        // only meets the identity guard again. Start a fresh one instead.
        if (state.identityChanged && renew) renew(); else void store.load();
      }}>Retry</button>
    </div>;
  }

  const retract = async (line: EditableTherapyLine) => {
    if (typeof line.episode_id !== 'number') return;
    setBusy(true); setError('');
    store.beginBypassWrite();
    try {
      await retractTherapyLine(line.episode_id, connection);
      // A read already in flight started before this write; sharing it would
      // publish the pre-write record (#2433).
      await store.load(true);
      onChanged?.();
    } catch {
      setError('Could not remove that line. Try again.');
    } finally {
      store.endBypassWrite();
      setBusy(false);
    }
  };

  const setComplete = async (next: boolean) => {
    if (personId === null) return;
    setBusy(true); setError('');
    store.beginBypassWrite();
    try {
      await setTherapyHistoryCompleteness(personId, next, connection);
      await store.load(true);
      onChanged?.();
    } catch {
      setError('Could not update whether this history is complete.');
    } finally {
      store.endBypassWrite();
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
        // Named by the visible sentence, not a differently worded aria-label
        // (WCAG 2.5.3); the hint is a description, not part of the name.
        aria-labelledby={`${completeId}-label`}
        aria-describedby={complete ? undefined : `${completeId}-hint`}
        className="mt-0.5"
      />
      <span>
        <span id={`${completeId}-label`}>This is the complete treatment history.</span>
        {!complete && <span id={`${completeId}-hint`} className="block text-xs text-muted-foreground">
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
      connection={connection}
      onClose={() => setDialog(null)}
      onAuthored={() => {
        setDialog(null);
        // The store reload and the host callback are part of the same bypass
        // write: hold the listeners until both finish. The reload supersedes
        // any read that started before the write (#2433).
        store.beginBypassWrite();
        void store.load(true)
          .then(() => onChanged?.())
          .finally(() => store.endBypassWrite());
      }}
    />}
  </div>;
}
