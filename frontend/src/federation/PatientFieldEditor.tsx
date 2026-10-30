import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Pencil } from 'lucide-react';
import Field from '@/components/PatientInfo/Field';
import { usePatientFormSnapshot } from './PatientFormContext';
import type { PatientFormStore } from './patientFormStore';

interface Props {
  store: PatientFormStore;
  name: string;
  readOnly?: boolean;
  infoHref?: string;
  ancestors?: string[];
  compact?: boolean;
  label?: string;
}

/** CB's computed → source-input dialog, with recursive drill-in. Medical
 * metadata and values come only from PROMOP, never the host's trial uoptions. */
export default function PatientFieldEditor({ store, name, readOnly, infoHref, ancestors = [], compact, label: fallbackLabel }: Props) {
  const state = usePatientFormSnapshot(store);
  const [open, setOpen] = useState(false);
  if (state.loading) return <span role="status">Loading patient value…</span>;
  if (state.error) return <div role="alert">{state.error} <button onClick={() => void store.load()}>Retry</button></div>;
  const canonical = store.resolve(name);
  const field = canonical ? state.fields[canonical] : undefined;
  if (!canonical || !field) return <div className="text-xs text-muted-foreground">
    {fallbackLabel && <div className="text-sm font-medium">{fallbackLabel}</div>}
    No reviewed PROMOP field binding. {infoHref && <a href={infoHref}>Open Your Info</a>}
  </div>;
  if (ancestors.includes(canonical)) return <p role="alert">Circular field dependency; editing is unavailable.</p>;
  const value = canonical in state.pending ? state.pending[canonical] : state.values[canonical];
  const dobPrecision = state.pending.date_of_birth_precision ?? state.values.date_of_birth_precision;
  const precision = canonical === 'date_of_birth'
    && (dobPrecision === 'day' || dobPrecision === 'month' || dobPrecision === 'year')
    ? dobPrecision : undefined;
  const label = field.label ?? canonical;
  const inputs = field.inputs ?? (field.canonical ? [field.canonical] : []);
  const writable = !readOnly && state.writable && field.writable && field.target === 'patient_record';
  const reason = field.compatibility_reason ?? field.reason ?? 'Derived value; edit the source inputs.';
  // A compatibility gate withholds the RESULT, not the clinical facts it is
  // computed from. Editing an input never writes the verdict; the result remains
  // read-only and is recalculated by PROMOP.
  const gated = Boolean(field.compatibility_reason);
  const formatted = value == null || value === '' ? 'Unknown'
    : precision === 'year' ? String(value).slice(0, 4)
    : precision === 'month' ? String(value).slice(0, 7)
    : typeof value === 'boolean' ? (value ? 'Yes' : 'No')
    : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const control = field.multiple ? 'multiselect' : field.options?.length
    ? 'select'
    : field.control ?? (field.value_kind === 'number' ? 'number'
      : field.value_kind === 'boolean' ? 'boolean' : field.value_kind === 'date' ? 'date' : 'text');
  return <div className="space-y-1" data-field={canonical}>
    {inputs.length ? <>
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
          <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-[60] bg-black/40" />
            <Dialog.Content className="promop-root fixed left-1/2 top-1/2 z-[61] max-h-[85vh] w-[min(32rem,calc(100%-2rem))] -translate-x-1/2 -translate-y-1/2 space-y-4 overflow-y-auto rounded-xl bg-background p-6 shadow-xl">
              <Dialog.Title className="font-semibold">{label}</Dialog.Title>
              <Dialog.Description className="text-sm text-muted-foreground">{readOnly
                ? 'These inputs are read-only in this view.'
                : gated
                  ? 'The result stays read-only. Editing these inputs records the source facts it is computed from; it does not set the result itself.'
                  : 'Computed from the inputs below. Save their values to update this result.'}</Dialog.Description>
              {field.compatibility_reason && <p role="alert">{field.compatibility_reason}</p>}
              {inputs.map((input) => <PatientFieldEditor key={input} store={store} name={input}
                readOnly={readOnly} infoHref={infoHref}
                ancestors={[...ancestors, canonical]} />)}
              {infoHref && <a className="block underline" href={infoHref}>Open Your Info</a>}
              <Dialog.Close asChild><button type="button" className="rounded border px-3 py-1">Close</button></Dialog.Close>
            </Dialog.Content></Dialog.Portal>
        </Dialog.Root>
      </div>
    </> : writable ? <Field label={compact ? '' : label} name={canonical} type={control}
      value={value} options={field.options?.map((option) => option.value)}
      triState={field.tri_state === true} precision={precision} onChange={store.enqueue} />
      : <><div>{!compact && <span className="text-sm font-medium">{label}: </span>}{formatted}</div>
        <p className="text-xs text-muted-foreground">{reason} {infoHref && <a href={infoHref}>Open Your Info</a>}</p></>}
    {(field.unit ?? field.projection?.unit) && <span className="text-xs text-muted-foreground">{field.unit ?? field.projection?.unit}</span>}
    {writable && field.projection?.omop_table === 'measurement'
      && <p className="text-xs text-muted-foreground">This edit is recorded with today’s date.</p>}
    {canonical in state.pending && <span role="status" className="text-xs">Saving…</span>}
    {state.errors[canonical] && <p role="alert" className="text-xs text-red-600">{state.errors[canonical]}</p>}
  </div>;
}
