import { useMemo } from 'react';
import { VocabSource } from '@/hooks/useVocabulary';
import { VocabularyTooltip } from '../UI/VocabularyTooltip';
import FieldMappingLink from './FieldMappingLink';
import SelectControl from './controls/SelectControl';
import BooleanControl from './controls/BooleanControl';
import MultiSelectControl from './controls/MultiSelectControl';
import DateControl from './controls/DateControl';
import TextNumberControl from './controls/TextNumberControl';
import { optionsToObjects, type OptionInput } from './utils';

interface FieldProps {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'select' | 'multiselect' | 'boolean' | 'email';
  value: unknown;
  options?: OptionInput[];
  onChange: (name: string, value: unknown) => void;
  disabled?: boolean;
  readOnly?: boolean;
  triState?: boolean;
  /** Reviewed wording for a boolean's two answers (#5601). */
  answerLabels?: { true?: string; false?: string };
  vocabSource?: VocabSource | null;
  fullWidth?: boolean;
  precision?: 'day' | 'month' | 'year' | null;
  /** Accessible name for the control when the visible label is suppressed
   *  (compact Trial Details mounts) or not programmatically associated. */
  ariaLabel?: string;
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
  triState,
  answerLabels,
  vocabSource,
  precision,
  ariaLabel,
}: FieldProps) {
  // An empty string is not a name: fall through and emit no attribute rather
  // than aria-label="" which some readers announce as a nameless control.
  const accessibleName = (ariaLabel?.trim() || label?.trim() || '') || undefined;
  const isDisabled = disabled || readOnly;
  const optionObjects = useMemo(() => optionsToObjects(options), [options]);

  const selectedValues = useMemo<string[]>(() => {
    const v = value;
    if (Array.isArray(v)) return v.map((x) => String(x)).filter(Boolean);
    if (typeof v === 'string') {
      return v.split(',').map((s) => s.trim()).filter(Boolean);
    }
    return [];
  }, [value]);

  const selectedLabels = useMemo(() => {
    return selectedValues.map((value) => optionObjects.find((option) => String(option.value) === value)?.label ?? value);
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
            ariaLabel={accessibleName}
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
            ariaLabel={accessibleName}
            options={optionObjects}
            selectedValues={selectedValues}
            display={msDisplay}
            disabled={isDisabled}
            onChange={(nextValues) => {
              onChange(
                name,
                isStringBacked ? (nextValues as string[]).join(',') : nextValues
              );
            }}
          />
        );

      case 'boolean':
        return (
          <BooleanControl
            value={value as boolean | null | undefined}
            ariaLabel={accessibleName}
            disabled={isDisabled}
            triState={triState}
            trueLabel={answerLabels?.true}
            falseLabel={answerLabels?.false}
            onChange={(v) => onChange(name, v)}
          />
        );

      case 'date':
        return (
          <DateControl
            value={formatDateForInput(value as string)}
            ariaLabel={accessibleName}
            disabled={isDisabled}
            onChange={(v) => onChange(name, v)}
            precision={precision}
          />
        );

      default:
        return (
          <TextNumberControl
            type={type}
            ariaLabel={accessibleName}
            value={value === 0 ? 0 : ((value ?? '') as string | number | null)}
            disabled={isDisabled}
            onChange={(v) => onChange(name, v)}
          />
        );
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, value, optionObjects, isDisabled, name, selectedValues, msDisplay, triState,
      answerLabels?.true, answerLabels?.false, precision, accessibleName]);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <label className="text-sm font-medium text-portal-text-primary">
          {label}
          {readOnly && <span className="ml-1 text-xs font-normal text-portal-text-secondary">(computed)</span>}
        </label>
        <FieldMappingLink name={name} label={label} />
        {vocabSource && <VocabularyTooltip name={vocabSource.name} url={vocabSource.url} />}
      </div>
      {control}
    </div>
  );
}
