import Field from './Field';
import type { FieldDescriptor } from '@/hooks/useWritableFields';

interface Props {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'boolean';
  value: unknown;
  descriptor?: FieldDescriptor;
  onChange: (name: string, value: unknown) => void;
  /** Event date for this edit, shared across the tab. */
  date?: string;
  onDateChange?: (date: string) => void;
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
  date,
  onDateChange,
}: Props) {
  const writable = !!descriptor?.writable;

  if (!writable) {
    const reason =
      descriptor?.reason ??
      'Derived from OMOP data; not editable here.';
    return (
      <div>
        <Field
          label={label}
          name={name}
          type={type}
          value={value}
          onChange={onChange}
          readOnly
        />
        <p className="mt-1 text-xs text-muted-foreground" data-testid={`reason-${name}`}>
          {reason}
        </p>
      </div>
    );
  }

  return (
    <div>
      <Field label={label} name={name} type={type} value={value} onChange={onChange} />
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
