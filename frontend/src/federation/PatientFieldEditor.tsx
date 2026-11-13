import { useContext, useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Pencil } from 'lucide-react';
import Field from '@/components/PatientInfo/Field';
import { PatientFormRenewContext, usePatientFormSnapshot } from './PatientFormContext';
import type { FormFieldDescriptor, PatientFormStore } from './patientFormStore';

interface Props {
  store: PatientFormStore;
  name: string;
  readOnly?: boolean;
  infoHref?: string;
  ancestors?: string[];
  compact?: boolean;
  label?: string;
  /** The caller owns a reviewed label that already carries this field's unit.
   * Do not append the descriptor unit beside it: one unit, one source (#5704). */
  labelIncludesUnit?: boolean;
  /** The tab's own choices for a select. They apply only when the descriptor
   * carries no options of its own; curated descriptor options always win, and
   * a date or boolean is never turned into a list of strings. */
  options?: string[];
  /** The control the caller's tab declared for this field (ClinicalField's
   * `type`). A tab that names `select`/`multiselect` alongside its own choices
   * is describing a closed answer set — ECOG 0-5, Karnofsky 0-100, protein
   * expressions — and the model-derived control (`IntegerField` → number)
   * would otherwise win and accept values the scale does not have. An `email`
   * declaration likewise keeps the input's type. Booleans, dates and curated
   * options keep the descriptor's control. */
  controlType?: 'text' | 'number' | 'select' | 'multiselect' | 'boolean' | 'date' | 'email';
}

/** Today in the browser's zone, not UTC: a result date is a local calendar day. */
function todayIso() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

interface DependencyGraph {
  /** Some reachable field is writable on PatientRecord. */
  editable: boolean;
  /** The graph revisits a field, so no edit path exists but must be explained. */
  cyclic: boolean;
}

/**
 * What a computed field's declared inputs can actually reach.
 *
 * A computed value is edited only through its sources, so the pencil is an
 * honest affordance only when drilling in reaches a field that accepts a
 * patient-record write. When the graph bottoms out in fields the record does
 * not own — No Active Infection → active_infection_status → OMOP condition
 * facts — the dialog would contain no control at all (#2244).
 *
 * A cycle is reported rather than hidden: the dialog is where the explanation
 * lives. `ancestors` doubles as the path guard, so shared inputs are walked
 * once per path without looping.
 */
function inspectInputs(
  store: PatientFormStore,
  fields: Record<string, FormFieldDescriptor>,
  names: string[],
  ancestors: Set<string>,
): DependencyGraph {
  const graph: DependencyGraph = { editable: false, cyclic: false };
  for (const name of names) {
    const canonical = store.resolve(name);
    if (!canonical) continue;
    if (ancestors.has(canonical)) {
      graph.cyclic = true;
      continue;
    }
    const field = fields[canonical];
    if (!field) continue;
    const direct = field.writable === true && field.target === 'patient_record';
    // Same precedence as the row: a field that declares inputs is edited
    // through them, and an alias is edited through the field it mirrors.
    const next = field.inputs ?? (!direct && field.canonical ? [field.canonical] : []);
    // A field with a withheld input renders read-only instead of a dialog
    // (#5600), so that branch cannot reach an editor. The row then falls
    // through to the direct control, which is why `direct` is checked second.
    const opensDialog = next.length > 0 && !next.some((input) => {
      const child = store.resolve(input);
      return !child || !fields[child];
    });
    if (opensDialog) {
      const child = inspectInputs(store, fields, next, new Set(ancestors).add(canonical));
      graph.editable ||= child.editable;
      graph.cyclic ||= child.cyclic;
      if (child.editable) return graph;
      // A dialog with no edit path does not open; the row falls through to its
      // direct control. A cycle is the exception, since the dialog explains it.
      if (child.cyclic) continue;
    }
    if (direct) {
      graph.editable = true;
      return graph;
    }
  }
  return graph;
}

/** CB's computed → source-input dialog, with recursive drill-in. Medical
 * metadata and values come only from PROMOP, never the host's trial uoptions. */
export default function PatientFieldEditor({ store, name, readOnly, infoHref, ancestors = [], compact, label: callerLabel, labelIncludesUnit = false, options: tabOptions, controlType }: Props) {
  const state = usePatientFormSnapshot(store);
  const renew = useContext(PatientFormRenewContext);
  const [open, setOpen] = useState(false);
  const [resultDateOverride, setResultDateOverride] = useState<string | null>(null);
  const [valueDraft, setValueDraft] = useState<{ set: boolean; value: unknown }>(
    { set: false, value: undefined });
  // A source pair's unit is chosen beside the value (#5700). `null` follows the
  // stored/pending unit until the reader picks one.
  const [unitDraft, setUnitDraft] = useState<string | null>(null);
  // A draft chosen while the value was empty must not survive a pair that
  // arrives from elsewhere (another mounted editor, a refresh, or a rollback
  // after a rejected clear). If the pair later clears again, a surviving draft
  // would quietly reapply an obsolete unit to the next typed number. Computing
  // this before the early returns keeps the hook order stable.
  // A queued edit, else a failed draft the reader has not retried yet.
  const hasDraft = (key: string) => key in state.pending || key in state.failed;
  const draftOf = (key: string) => key in state.pending ? state.pending[key] : state.failed[key];
  const initialCanonical = store.resolve(name);
  const initialPending = initialCanonical && hasDraft(initialCanonical)
    ? draftOf(initialCanonical) : undefined;
  const initialValue = initialCanonical
    ? (hasDraft(initialCanonical)
      ? (initialPending && typeof initialPending === 'object'
        && !Array.isArray(initialPending) && 'value' in initialPending
        ? (initialPending as { value: unknown }).value : initialPending)
      : state.values[initialCanonical])
    : undefined;
  // A dated row keeps its chosen unit as a draft until "Save result"; the
  // stale-draft reset only applies to rows that autosave.
  const initialDated = Boolean(
    initialCanonical && store.getSnapshot().fields[initialCanonical]?.dated);
  useEffect(() => {
    if (!initialDated && unitDraft && initialValue != null && initialValue !== '') {
      setUnitDraft(null);
    }
  }, [initialDated, unitDraft, initialValue]);
  if (state.loading) return <span role="status">Loading patient value…</span>;
  // The client now speaks for another patient: Retry starts a fresh store
  // through the host's renewal when there is one; reloading this store would
  // only meet the same identity guard again.
  if (state.error) return <div role="alert">{state.error} <button onClick={() => {
    if (state.identityChanged && renew) renew(); else void store.load();
  }}>Retry</button></div>;
  const canonical = store.resolve(name);
  const field = canonical ? state.fields[canonical] : undefined;
  if (!canonical || !field) return <div className="text-xs text-muted-foreground">
    {callerLabel && <div className="text-sm font-medium">{callerLabel}</div>}
    No reviewed PROMOP field binding. {infoHref && <a href={infoHref}>Open Your Info</a>}
  </div>;
  if (ancestors.includes(canonical)) return <p role="alert">Circular field dependency; editing is unavailable.</p>;
  // A field's caller owns its patient-facing wording. PROMOP descriptors provide
  // a generated label as a fallback; when a tab supplies a reviewed label, don't
  // let that generated value replace it (for example "Hiv status" vs "HIV Positive").
  const label = (callerLabel ?? '').trim() || field.label || canonical;
  // A negated descriptor asks the trial's question over a record that stores the
  // positive answer ("No HIV" over `hiv_status`). The edit lands on the positive
  // column with the value inverted; writing the negated name itself would store
  // the opposite answer (#5663).
  const negatedSource = field.kind === 'negated' && typeof field.canonical === 'string'
    ? store.resolve(field.canonical) : undefined;
  if (negatedSource) {
    const source = state.fields[negatedSource];
    const queued = hasDraft(negatedSource) ? draftOf(negatedSource) : undefined;
    const raw = hasDraft(negatedSource)
      ? (queued && typeof queued === 'object' && !Array.isArray(queued) && 'value' in queued
        ? (queued as { value: unknown }).value : queued)
      : state.values[negatedSource];
    const inverted = raw === true ? false : raw === false ? true : raw;
    const canWrite = !readOnly && state.writable && source?.writable === true
      && source.target === 'patient_record';
    const answer = raw === true ? 'No' : raw === false ? 'Yes' : 'Unknown';
    // A host that withholds the positive column says why on that field; keep
    // that reason next to the inversion, or the row explains the mapping but
    // not why it cannot be answered here.
    const blocked = source && source.writable !== true ? source.reason : undefined;
    const readOnlyReason = [blocked, field.reason].filter(Boolean).join(' ')
      || 'Inverted view of the positive status.';
    return <div className="space-y-1" data-field={canonical}>
      {canWrite ? <Field label={compact ? '' : label} ariaLabel={label}
          name={canonical} type="boolean"
          value={inverted} triState={source.tri_state === true}
          onChange={(_name, next) => store.enqueue(negatedSource,
            next === true ? false : next === false ? true : next)} />
        : <><div>{!compact && <span className="text-sm font-medium">{label}: </span>}{answer}</div>
          <p className="text-xs text-muted-foreground">{readOnlyReason}
            {' '}{infoHref && <a href={infoHref}>Open Your Info</a>}</p></>}
      {negatedSource in state.pending && <span role="status" className="text-xs">Saving…</span>}
      {state.errors[negatedSource] && <p role="alert" className="text-xs text-red-600">{state.errors[negatedSource]}
        {negatedSource in state.failed && canWrite && <>{' '}<button type="button" className="underline"
          onClick={() => store.retryFailed([negatedSource])}>Retry</button></>}</p>}
    </div>;
  }
  const pending = hasDraft(canonical) ? draftOf(canonical) : undefined;
  // A dated edit is queued as {value, date}; the row still edits the value.
  const pendingValue = pending && typeof pending === 'object' && !Array.isArray(pending) && 'value' in pending
    ? (pending as { value: unknown }).value : pending;
  const value = hasDraft(canonical) ? pendingValue : state.values[canonical];
  const dobPrecision = draftOf('date_of_birth_precision') ?? state.values.date_of_birth_precision;
  const precision = canonical === 'date_of_birth'
    && (dobPrecision === 'day' || dobPrecision === 'month' || dobPrecision === 'year')
    ? dobPrecision : undefined;
  // An alias descriptor points at the field it mirrors. A field the server says
  // is directly writable is edited in place: drilling into its own canonical
  // would show a pencil for a plain select (#5605 Refractory Status).
  const directWrite = field.writable === true && field.target === 'patient_record';
  const inputs = field.inputs ?? (!directWrite && field.canonical ? [field.canonical] : []);
  // A dependency the host withholds has no descriptor of its own. Opening the
  // dialog anyway would promise an edit surface made only of "No reviewed
  // PROMOP field binding" rows (#5600); a partial dialog would misrepresent the
  // missing sources as optional. Treat the whole computed value as read-only.
  const unavailableInputs = inputs.filter((input) => {
    const child = store.resolve(input);
    return !child || !state.fields[child];
  });
  const inputsUnavailable = unavailableInputs.length > 0;
  // Dependencies that are present but not editable are the same dead end as
  // withheld ones: the dialog would show values and no control (#2244). A
  // cycle still gets the dialog, because that is where the cycle is explained.
  const inputGraph = inspectInputs(store, state.fields, inputs, new Set([canonical]));
  const editPath = inputGraph.editable || inputGraph.cyclic;
  const writable = !readOnly && state.writable && field.writable && field.target === 'patient_record';
  // The unit beside a source value (#5700). The descriptor names the companion
  // entry by `qualifies`; the pair travels together so the server normalises the
  // canonical and the client never converts or guesses a factor.
  const unitFieldName = Object.keys(state.fields).find(
    (key) => state.fields[key].qualifies === canonical
      && (state.fields[key].unit_options?.length ?? 0) > 1);
  const unitOptions = field.unit_options
    ?? (unitFieldName ? state.fields[unitFieldName].unit_options : undefined) ?? [];
  // A dated fact is edited with its clinical date. The latest recorded date is
  // the correction target; a new entry defaults to today, and an older choice
  // says what happens to the current value.
  const dated = writable && field.dated === true;
  const pendingUnit = unitFieldName && hasDraft(unitFieldName)
    ? draftOf(unitFieldName) : undefined;
  const storedUnit = String(pendingUnit
    ?? (unitFieldName ? state.values[unitFieldName] : undefined) ?? '');
  // A draft chosen while the value was empty must not outlive a pair that
  // arrived from elsewhere (another mounted editor, or a rollback after a
  // rejected clear). Once the shared value is non-empty, the saved/pending pair
  // is the authority; the draft is ignored and cleared by the next queued edit.
  const authoritativeUnit = storedUnit || unitOptions[0]?.value || '';
  const chosenUnit = dated
    ? (unitDraft ?? authoritativeUnit)
    : (value == null || value === '')
      ? (unitDraft ?? authoritativeUnit) : authoritativeUnit;
  const showUnitPicker = writable && Boolean(unitFieldName) && unitOptions.length > 1;
  const pairError = state.errors[canonical]
    ?? (unitFieldName ? state.errors[unitFieldName] : undefined);
  const pairPending = canonical in state.pending
    || Boolean(unitFieldName && unitFieldName in state.pending);
  const pairFailed = canonical in state.failed
    || Boolean(unitFieldName && unitFieldName in state.failed);
  // A field the record would accept, shown in a read-only mount or to a caller
  // without write access, is not derived: it is only locked here. It keeps the
  // descriptor's own explanation if it has one and otherwise claims nothing.
  const lockedByView = directWrite && (readOnly || !state.writable);
  // A dated input commits only through its own "Save result" pair, so the
  // dialog must not promise that every edit saves itself.
  // The gate reason used to be reachable inside the dialog. Rows that lose the
  // dialog must keep it, or the clinical restriction disappears with the pencil.
  const reason = inputsUnavailable || (inputs.length > 0 && !editPath)
    ? ['This value is derived from data that is not editable in this view.',
      field.compatibility_reason].filter(Boolean).join(' ')
    : field.compatibility_reason ?? field.reason
      ?? (lockedByView ? undefined : 'Derived value; edit the source inputs.');
  // A compatibility gate withholds the RESULT, not the clinical facts it is
  // computed from. Editing an input never writes the verdict; the result remains
  // read-only and is recalculated by PROMOP.
  const gated = Boolean(field.compatibility_reason);
  const pendingDate = pending && typeof pending === 'object' && !Array.isArray(pending)
      && 'date' in pending ? (pending as { date?: unknown }).date : undefined;
  // A value the reader already edited may not be the newest fact on the
  // record: forcing the newest date as the correction target would file the
  // correction on the wrong clinical day. Such a row asks for the date
  // explicitly instead of guessing.
  const needsExplicitDate = field.user_edited === true;
  const resultDate = resultDateOverride
    ?? (typeof pendingDate === 'string' ? pendingDate : undefined)
    ?? (needsExplicitDate ? '' : field.latest_date ?? todayIso());
  // The correction target comes from the server. If the post-save refresh
  // failed, `latest_date` is the pre-save date and saving against it would
  // record the result on the wrong clinical day, so the pair waits for a
  // successful refresh (#5608).
  const datesStale = state.datesStale === true;
  // A dated value and its date are one edit. Autosaving the value alone would
  // attach it to the previous result's date before the user has chosen one, so
  // the pair waits for an explicit commit.
  const datedValue = dated && valueDraft.set ? valueDraft.value : value;
  const datedDirty = dated && (valueDraft.set || resultDateOverride !== null
    || unitDraft !== null);
  const commitDated = () => {
    if (datesStale || !resultDate) return;
    const datedEdit = { value: datedValue, date: resultDate };
    if (showUnitPicker && unitFieldName) {
      // The number, its date and the unit are one result. Saving the value
      // alone would keep the stored unit and silently change the scale.
      if (store.enqueuePair(canonical, datedEdit, unitFieldName, chosenUnit)) {
        setUnitDraft(null);
      }
    } else {
      store.enqueue(canonical, datedEdit);
    }
    setValueDraft({ set: false, value: undefined });
    setResultDateOverride(null);
  };
  const valueControl = field.value_kind === 'number' ? 'number'
    : field.value_kind === 'boolean' ? 'boolean' : field.value_kind === 'date' ? 'date' : 'text';
  // A tab's declared select with its own answers is a closed scale. It beats
  // the descriptor's model-derived control (`IntegerField` → number); booleans
  // keep their yes/no control because their stored value is a boolean, not the
  // tab's answer string, dates keep the date input, and a plain number stays a
  // number box.
  const tabSelect = (controlType === 'select' || controlType === 'multiselect')
    && Boolean(tabOptions?.length)
    && field.value_kind !== 'boolean' && field.value_kind !== 'date'
    && field.control !== 'boolean' && field.control !== 'date';
  const ownControl = tabSelect ? controlType
    : (controlType === 'email' && valueControl === 'text') ? 'email'
    : field.control ?? valueControl;
  const choices = field.options?.length ? field.options
    : tabOptions?.length && (field.multiple || ownControl === 'text'
        || ownControl === 'select' || ownControl === 'multiselect')
      ? tabOptions : [];
  // A declared multiselect stays a list even when the descriptor does not mark
  // the column `multiple` (protein expressions are comma-joined strings).
  const control = (field.multiple || ownControl === 'multiselect') ? 'multiselect'
    : choices.length ? 'select' : ownControl;
  // A numeric scale answered from a picker still writes a number: the column is
  // numeric and the editor's contract is type-preserving.
  const numericAnswers = control === 'select' && field.value_kind === 'number';
  // A read-only list reads as its answers, by label, not as serialised JSON:
  // ["autologous SCT", "tandem SCT"] and "autologous SCT,tandem SCT" both
  // show "autologous SCT, tandem SCT".
  const optionLabel = (item: unknown) => {
    const choice = (choices as Array<string | { value: unknown; label?: string }>).find((option) =>
      String(typeof option === 'string' ? option : option.value) === String(item));
    return choice && typeof choice === 'object' && choice.label ? choice.label : String(item);
  };
  // Only a list of plain answers is listed; a list of records (objects) keeps
  // the serialised form rather than printing "[object Object]".
  const primitive = (item: unknown) => item == null
    || ['string', 'number', 'boolean'].includes(typeof item);
  const listed = Array.isArray(value) && value.every(primitive) ? value
    : field.multiple && typeof value === 'string' ? value.split(',') : undefined;
  // A null entry is absence, not the word "null": `String(null)` is truthy
  // and a read-only list rendered it beside the real answers.
  const items = listed?.filter((item) => item != null)
    .map((item) => String(item).trim()).filter(Boolean);
  // A reviewed answer pair answers the row's own question: a read-only "No"
  // beside "HIV Positive" is a different sentence than "No HIV" (#5601).
  const formatted = value == null || value === '' || (items && !items.length) ? 'Unknown'
    : items ? items.map(optionLabel).join(', ')
    : precision === 'year' ? String(value).slice(0, 4)
    : precision === 'month' ? String(value).slice(0, 7)
    : typeof value === 'boolean'
      ? (value ? field.answer_labels?.true ?? 'Yes' : field.answer_labels?.false ?? 'No')
    : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const onChangeValue = (name: string, next: unknown) => {
    if (numericAnswers) {
      // Clearing a numeric picker writes the same null the number box writes;
      // an empty string would reach an integer column. An answer that is not a
      // number is passed through unchanged: the server refuses it visibly, and
      // turning it into null would erase the stored value instead.
      if (next == null || next === '') next = null;
      else {
        const parsed = Number(next);
        if (Number.isFinite(parsed)) next = parsed;
      }
    }
    if (dated) { setValueDraft({ set: true, value: next }); return; }
    if (showUnitPicker && unitFieldName) {
      // Once queued, the pending pair (then the server value) owns the choice.
      // Keeping a local draft would let a rejected unit survive its rollback and
      // contaminate the next number edit.
      if (store.enqueuePair(canonical, next, unitFieldName, chosenUnit)) setUnitDraft(null);
      return;
    }
    store.enqueue(name, next);
  };
  const controlNode = <Field label={compact ? '' : label} ariaLabel={label}
    name={canonical} type={control}
    value={dated ? datedValue : value} options={choices}
    triState={field.tri_state === true} answerLabels={field.answer_labels}
    precision={precision}
    onChange={onChangeValue} />;
  return <div className="space-y-1" data-field={canonical}>
    {inputs.length && !inputsUnavailable && editPath ? <>
      {!compact && <div className="text-sm font-medium">{label}</div>}
      <div className="flex items-center gap-2"><span>{formatted}</span>
        <Dialog.Root open={open} onOpenChange={setOpen}>
          {/* A pencil, not an "Edit inputs" label: the row already shows the
              value, and the label widened the column enough to wrap "Your Value"
              on narrow screens. The accessible name stays explicit. */}
          <Dialog.Trigger asChild><button type="button"
            className="shrink-0 rounded p-1 text-muted-foreground hover:text-foreground"
            aria-label={`Edit ${label} inputs`} title={`Edit ${label} inputs`}>
            <Pencil className="h-4 w-4" strokeWidth={1.67} />
          </button></Dialog.Trigger>
          {/* Centred by auto margins, not translate utilities: a Tailwind 3
              host sets `transform` for those while ours sets `translate`, and
              both moves apply (see ui-labs/dialog.tsx). */}
          <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-[60] bg-black/40" />
            <Dialog.Content className="promop-root fixed inset-0 z-[61] m-auto h-fit max-h-[85vh] w-[min(32rem,calc(100%-2rem))] space-y-4 overflow-y-auto rounded-xl bg-background p-6 shadow-xl">
              <Dialog.Title className="font-semibold">{label}</Dialog.Title>
              <Dialog.Description className="text-sm text-muted-foreground">{readOnly || !state.writable
                ? 'These inputs are read-only in this view.'
                : !inputGraph.editable
                  ? 'These inputs have no edit path in this view; the dependency is shown for reference.'
                  : gated
                    ? 'The result stays read-only. Editing these inputs records the source facts it is computed from; it does not set the result itself.'
                    : 'Computed from the inputs below. This result is recalculated from them.'}</Dialog.Description>
              {field.compatibility_reason && <p role="alert">{field.compatibility_reason}</p>}
              {inputs.map((input) => <PatientFieldEditor key={input} store={store} name={input}
                readOnly={readOnly} infoHref={infoHref}
                ancestors={[...ancestors, canonical]} />)}
              {infoHref && <a className="block underline" href={infoHref}>Open Your Info</a>}
              <Dialog.Close asChild><button type="button" className="rounded border px-3 py-1">Close</button></Dialog.Close>
            </Dialog.Content></Dialog.Portal>
        </Dialog.Root>
      </div>
    </> : writable ? <>
      {showUnitPicker ? <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">{controlNode}</div>
        <label className="flex shrink-0 flex-col gap-1 text-xs text-muted-foreground">
          <span className="sr-only">Unit for {label}</span>
          <select aria-label={`Unit for ${label}`} value={chosenUnit}
            onChange={(event) => {
              const nextUnit = event.target.value;
              setUnitDraft(nextUnit);
              // A dated row commits only through "Save result": queueing the
              // pair here would attach the current value to today's date.
              if (dated) return;
              // A unit change with a value is a real edit of the same pair, not
              // a formatting change; a unit alone cannot be written.
              if (value != null && value !== '' && unitFieldName
                  && store.enqueuePair(canonical, value, unitFieldName, nextUnit)) {
                // A rejected save restores the server pair; the selector must
                // follow it rather than keep the rejected unit.
                setUnitDraft(null);
              }
            }}
            className="rounded-md border border-input px-2 py-1 text-sm">
            {unitOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label ?? option.value}</option>
            ))}
          </select>
        </label>
      </div> : controlNode}
      {dated && <div className="mt-1 flex flex-wrap items-center gap-2">
        <label htmlFor={`${canonical}-result-date`} className="text-xs text-muted-foreground whitespace-nowrap">
          Result date
        </label>
        <input id={`${canonical}-result-date`} type="date" value={resultDate} max={todayIso()}
          onChange={(event) => setResultDateOverride(event.target.value)}
          className="rounded-md border border-input px-2 py-1 text-xs" />
        <button type="button" disabled={!datedDirty || datesStale || !resultDate} onClick={commitDated}
          className="rounded-md border border-input px-2 py-1 text-xs disabled:opacity-50">
          Save result
        </button>
      </div>}
      {dated && datesStale && <p role="alert" className="text-xs text-red-600">
        The latest result date could not be refreshed, so the correction target may be out of date.
        {' '}<button type="button" className="underline" onClick={() => void store.reloadDates()}>Retry</button>
      </p>}
      {dated && field.latest_date && resultDate && resultDate < field.latest_date
        // A dated row without its own projection is a source pair, retained
        // until an explicit canonical event; see _SOURCE_PAIR_FIELDS.
        && <p role="status" className="text-xs text-muted-foreground">
          {field.projection
            // PatientRecord-first: the shared write sets the record to this
            // value now, and only the next rebuild from the dated facts puts
            // the newer result back (tests/test_dated_patient_form_writes.py).
            ? 'Older than the latest recorded result. It is saved with this date and may show as the current value until the record is next rebuilt from your results; the newer result then becomes current again.'
            : 'Older than the latest recorded result; your answer stays the current value and is recorded with this result date.'}
        </p>}
    </>
      : <><div>{!compact && <span className="text-sm font-medium">{label}: </span>}{formatted}</div>
        {(reason || infoHref || field.retryable) && <p className="text-xs text-muted-foreground">{reason} {infoHref && <a href={infoHref}>Open Your Info</a>} {field.retryable &&
          <button type="button" className="underline" onClick={() => void store.load()}>Retry</button>}</p>}</>}
    {field.options_note && <p role="status" className="text-xs text-muted-foreground">{field.options_note}</p>}
    {!showUnitPicker && !labelIncludesUnit && (field.unit ?? field.projection?.unit)
      && <span className="text-xs text-muted-foreground">{field.unit ?? field.projection?.unit}</span>}
    {writable && field.projection?.omop_table === 'measurement' && !dated
      && <p className="text-xs text-muted-foreground">This edit is recorded with today’s date.</p>}
    {pairPending && <span role="status" className="text-xs">Saving…</span>}
    {pairError && <p role="alert" className="text-xs text-red-600">{pairError}
      {pairFailed && writable && <>{' '}<button type="button" className="underline"
        onClick={() => store.retryFailed([canonical])}>Retry</button></>}</p>}
  </div>;
}
