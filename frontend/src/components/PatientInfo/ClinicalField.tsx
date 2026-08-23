import Field from './Field';
import type { VocabSource } from '@/hooks/useVocabulary';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

interface Props {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'email';
  value: unknown;
  /** Choices for a select. The descriptor's own `options` win when it has them:
   *  those are the curated set the server resolves a concept from, so a local
   *  list could offer a value the write would then fail to code. */
  options?: string[];
  vocabSource?: VocabSource | null;
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
  date,
  onDateChange,
  showReason = true,
}: Props) {
  const writable = !!descriptor?.writable;
  const curated = descriptor?.options?.map((o) => o.value);
  const choices = curated ?? options;
  const control = choices?.length ? 'select' : type;

  if (!writable) {
    const reason =
      descriptor?.reason ??
      'Derived from OMOP data; not editable here.';
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
      {onDateChange && (
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
            value={date ?? ''}
            onChange={(e) => onDateChange(e.target.value)}
            className="rounded-md border border-input px-2 py-1 text-xs"
          />
        </div>
      )}
    </div>
  );
}
