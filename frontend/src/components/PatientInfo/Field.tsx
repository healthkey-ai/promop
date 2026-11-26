import { useMemo } from 'react';
import { VocabSource } from '@/hooks/useVocabulary';
import { VocabularyTooltip } from '../UI/VocabularyTooltip';
import FieldMappingLink from './FieldMappingLink';
import ProvisionalBadge, { AcceptedBadge } from './ProvisionalBadge';
import SelectControl from './controls/SelectControl';
import BooleanControl from './controls/BooleanControl';
import MultiSelectControl from './controls/MultiSelectControl';
import DateControl from './controls/DateControl';
import TextNumberControl from './controls/TextNumberControl';
import { stringsToOptions, type OptionInput } from './utils';

interface FieldProps {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'select' | 'multiselect' | 'boolean' | 'email';
  value: unknown;
  options?: OptionInput[];
  onChange: (name: string, value: unknown) => void;
  disabled?: boolean;
  readOnly?: boolean;
  vocabSource?: VocabSource | null;
  fullWidth?: boolean;
  precision?: 'day' | 'month' | 'year' | null;
  /** Say "Accepted" beside the label when the field holds a value that is not a
   *  proposal. Opt-in: the Wearables tab, where every value is one or the other. */
  showAccepted?: boolean;
  /** The name the field's proposal mark has in `provisional_fields`, when it is not `name`:
   *  a created field's is `custom_fields.<name>` (#2857). */
  markName?: string;
}

/** 0 and false are values; null, undefined, '' and an empty list are none. */
function hasValue(value: unknown): boolean {
  return value !== null && value !== undefined && value !== '' && !(Array.isArray(value) && value.length === 0);
}

export default function Field({
  label,
  name,
  type,
  value,
  options = [],
  onChange,
  disabled,
  readOnly,
  vocabSource,
  precision,
  showAccepted,
  markName,
}: FieldProps) {
  const isDisabled = disabled || readOnly;
  const optionObjects = useMemo(() => stringsToOptions(options), [options]);

  const selectedValues = useMemo<string[]>(() => {
    const v = value;
    if (Array.isArray(v)) return v.map((x) => String(x)).filter(Boolean);
    if (typeof v === 'string') {
      return v.split(',').map((s) => s.trim()).filter(Boolean);
    }
    return [];
  }, [value]);

  const selectedLabels = useMemo(() => {
    const set = new Set(selectedValues);
    return optionObjects.filter((o) => set.has(String(o.value))).map((o) => o.label);
  }, [optionObjects, selectedValues]);

  const msDisplay = useMemo(() => {
    if (selectedLabels.length === 0) return 'Select...';
    if (selectedLabels.length === 1) return selectedLabels[0];
    if (selectedLabels.length <= 3) return selectedLabels.join(', ');
    return `${selectedLabels[0]}, ${selectedLabels[1]} +${selectedLabels.length - 2} more`;
  }, [selectedLabels]);

  // Derived per-render so it never goes stale when value changes from null → array
  const isStringBacked = value == null || typeof value === 'string';

  const formatDateForInput = (dateString: string) => {
    if (!dateString) return '';
    try {
      return new Date(dateString).toISOString().split('T')[0];
    } catch {
      return '';
    }
  };

  const control = useMemo(() => {
    switch (type) {
      case 'select':
        return (
          <SelectControl
            value={value != null && value !== '' ? String(value) : ''}
            options={optionObjects}
            disabled={isDisabled}
            placeholder="Select…"
            allowClear={true}
            clearLabel="— None —"
            treatEmptyOptionAsUnknown={false}
            onChange={(v) => onChange(name, v ?? '')}
          />
        );

      case 'multiselect':
        return (
          <MultiSelectControl
            options={optionObjects}
            selectedValues={selectedValues}
            display={msDisplay}
            disabled={isDisabled}
            onChange={(nextValues) => {
              onChange(
                name,
                isStringBacked ? (nextValues as string[]).join(', ') : nextValues
              );
            }}
          />
        );

      case 'boolean':
        return (
          <BooleanControl
            value={value as boolean | null | undefined}
            disabled={isDisabled}
            onChange={(v) => onChange(name, v)}
          />
        );

      case 'date':
        return (
          <DateControl
            value={formatDateForInput(value as string)}
            disabled={isDisabled}
            onChange={(v) => onChange(name, v)}
            precision={precision}
          />
        );

      default:
        return (
          <TextNumberControl
            type={type}
            value={value === 0 ? 0 : ((value ?? '') as string | number | null)}
            disabled={isDisabled}
            onChange={(v) => onChange(name, v)}
          />
        );
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, value, optionObjects, isDisabled, name, selectedValues, msDisplay, precision]);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <label className="text-sm font-medium text-portal-text-primary">
          {label}
          {readOnly && <span className="ml-1 text-xs font-normal text-portal-text-secondary">(computed)</span>}
        </label>
        <ProvisionalBadge name={markName ?? name} fromDevice={showAccepted} label={label} />
        {showAccepted && hasValue(value) && <AcceptedBadge name={markName ?? name} />}
        <FieldMappingLink name={name} label={label} />
        {vocabSource && <VocabularyTooltip name={vocabSource.name} url={vocabSource.url} />}
      </div>
      {control}
    </div>
  );
}
