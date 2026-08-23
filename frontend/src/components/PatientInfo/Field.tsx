import { useMemo } from 'react';
import { usePatientInfoContext } from '@/federation/PatientInfoContext';
import { VocabSource } from '@/hooks/useVocabulary';
import { VocabularyTooltip } from '../UI/VocabularyTooltip';
import SelectControl from './controls/SelectControl';
import BooleanControl from './controls/BooleanControl';
import MultiSelectControl from './controls/MultiSelectControl';
import DateControl from './controls/DateControl';
import TextNumberControl from './controls/TextNumberControl';
import { stringsToOptions } from './utils';

interface FieldProps {
  label: string;
  name: string;
  type: 'text' | 'number' | 'date' | 'select' | 'multiselect' | 'boolean' | 'email';
  value: unknown;
  options?: string[];
  onChange: (name: string, value: unknown) => void;
  disabled?: boolean;
  readOnly?: boolean;
  vocabSource?: VocabSource | null;
  fullWidth?: boolean;
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
}: FieldProps) {
  // A field the server has no write path for (email/computed/unmapped/…) is shown read-only
  // rather than editable, so a user never edits a value that silently would not save. Null
  // writableFields (descriptor unavailable) leaves everything editable — never lock the form.
  const { writableFields } = usePatientInfoContext();
  const notWritable = writableFields != null && !writableFields.has(name);
  const isDisabled = disabled || readOnly || notWritable;
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
  }, [type, value, optionObjects, isDisabled, name, selectedValues, msDisplay]);

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <label className="text-sm font-medium text-portal-text-primary">
          {label}
          {readOnly && <span className="ml-1 text-xs font-normal text-portal-text-secondary">(computed)</span>}
          {!readOnly && notWritable && <span className="ml-1 text-xs font-normal text-portal-text-secondary">(read-only)</span>}
        </label>
        {vocabSource && <VocabularyTooltip name={vocabSource.name} url={vocabSource.url} />}
      </div>
      {control}
    </div>
  );
}
