import { useState } from 'react';
import Field from './Field';
import { today } from '@/api/clinicalFacts';
import type { VocabSource } from '@/hooks/useVocabulary';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

interface Props {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'multiselect' | 'email';
  value: unknown;
  /** Choices for a select. The descriptor's own `options` win when it has them:
   *  those are the curated set the server resolves a concept from, so a local
   *  list could offer a value the write would then fail to code. */
  options?: string[];
  vocabSource?: VocabSource | null;
  /** Mark a control the API has no field for at all — distinct from one it
   *  refuses to write. See #646. */
  unknownField?: boolean;
  descriptor?: FieldDescriptor;
  onChange: (name: string, value: unknown) => void;
  /** Event date for this edit, shared across the tab. */
  date?: string;
  onDateChange?: (date: string) => void;
  /** Print the reason under the field. Off for a tab where every field shares
   *  one reason — repeating the same paragraph beside 25 boxes buries it. The
   *  tab states it once instead. */
  showReason?: boolean;
}

/**
 * A PatientRecord field rendered according to what the server says can be done
 * with it.
 *
 * The projection is derived and owns no writable clinical column, so a box is
 * typeable only when the server can name the OMOP fact behind it. Anything else
 * renders read-only *with the reason* — a field that is computed from height and
 * weight, or that mirrors another column, is not "broken", and saying so is the
 * difference between a UI that looks unfinished and one that explains itself.
 *
 * An absent descriptor means read-only. Failing closed matters: offering an edit
 * the server will refuse is worse than showing a value that cannot yet be changed.
 */
export default function ClinicalField({
  label,
  name,
  type,
  value,
  descriptor,
  onChange,
  options,
  vocabSource,
  unknownField,
  date,
  onDateChange,
  showReason = true,
}: Props) {
  const writable = !!descriptor?.writable;
  // A tab that shares one date across its fields passes it in — a blood panel is
  // drawn once. A tab whose results come from different specimens, as a pathology
  // report's do, can leave it out and get a date per field rather than one date
  // pretending to cover all of them.
  const [ownDate, setOwnDate] = useState(today());
  // `absent` is stronger than "not writable": the server has no such field.
  const absent = !!unknownField;
  const curated = descriptor?.options?.map((o) => o.value);
  const choices = curated ?? options;
  // `multiple` is the descriptor saying several answers are stored at once,
  // comma-joined — SCT history is "autologous SCT,tandem SCT". Rendering that as
  // a single select would silently discard every answer but one.
  const control = choices?.length
    ? (descriptor?.multiple ? 'multiselect' : 'select')
    : type;

  if (!writable) {
    const reason =
      descriptor?.reason
      ?? (absent
        // Not a PatientRecord column at all: the API neither returns it nor
        // accepts it, so the box could never show a value and never save one.
        // Saying "derived" here would be wrong in the other direction (#646).
        ? 'Not stored on the patient record yet.'
        : 'Derived from OMOP data; not editable here.');
    return (
      <div>
        <Field
          label={label}
          name={name}
          type={control}
          value={value}
          options={choices}
          onChange={onChange}
          readOnly
        />
        {showReason && (
          <p className="mt-1 text-xs text-muted-foreground" data-testid={`reason-${name}`}>
            {reason}
          </p>
        )}
      </div>
    );
  }

  return (
    <div>
      <Field
        label={label}
        name={name}
        type={control}
        value={value}
        options={choices}
        vocabSource={vocabSource}
        onChange={onChange}
      />
      {(onDateChange || descriptor?.target === 'measurement') && (
        <div className="mt-1 flex items-center gap-2">
          <label
            htmlFor={`${name}-date`}
            className="text-xs text-muted-foreground whitespace-nowrap"
          >
            Result date
          </label>
          <input
            id={`${name}-date`}
            type="date"
            value={date ?? ownDate}
            onChange={(e) => (onDateChange ?? setOwnDate)(e.target.value)}
            className="rounded-md border border-input px-2 py-1 text-xs"
          />
        </div>
      )}
    </div>
  );
}
