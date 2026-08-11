import { useMemo } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/select";

type Option = { value: unknown; label: string };

const CLEAR = "__clear__";
const UNKNOWN = "__unknown__";

const keyOf = (v: unknown) => {
  if (v === null || v === undefined) return "";
  return String(v).trim();
};

export default function SelectControl({
  value,
  options,
  disabled,
  placeholder = "Select…",
  allowClear = false,
  clearLabel = "— None —",
  treatEmptyOptionAsUnknown = true,
  onChange,
}: {
  value: unknown;
  options: Option[];
  disabled?: boolean;
  placeholder?: string;
  allowClear?: boolean;
  clearLabel?: string;
  treatEmptyOptionAsUnknown?: boolean;
  onChange: (v: unknown) => void;
}) {
  const normalized = useMemo(() => {
    return (options ?? []).map((o) => {
      const k = keyOf(o.value);
      if (!k && treatEmptyOptionAsUnknown) {
        return { ...o, __key: UNKNOWN, __original: o.value };
      }
      return { ...o, __key: k, __original: o.value };
    });
  }, [options, treatEmptyOptionAsUnknown]);

  const keyToValue = useMemo(() => {
    const m = new Map<string, unknown>();
    for (const o of normalized) {
      const k = o.__key;
      if (!k) continue;
      if (k === CLEAR) continue;
      m.set(k, o.__original);
    }
    return m;
  }, [normalized]);

  const currentKey = useMemo(() => {
    const k = keyOf(value);
    if (!k && treatEmptyOptionAsUnknown) return UNKNOWN;
    return k;
  }, [value, treatEmptyOptionAsUnknown]);

  // A Radix Select falls back to its placeholder when `value` matches no item,
  // so a stored value outside the vocabulary reads as "not set" even though the
  // record holds it. That is not hypothetical: PatientRecord stores what OMOP
  // derived ("T1", "POSITIVE", "AC-T") while these vocabularies list display
  // titles ("T1: Invasive Tumor ≤ 2 cm", "ER+", "Trastuzumab (Herceptin)"), so
  // TNM, receptor status and every therapy line rendered blank.
  //
  // Surface the value as its own option rather than hiding it. A control must
  // never show less than the record holds — a clinician reading a blank field
  // concludes the data is missing, which is a worse failure than an unpolished
  // label. Selecting a real option still replaces it.
  const orphanKey = useMemo(() => {
    if (!currentKey || currentKey === UNKNOWN || currentKey === CLEAR) return null;
    return keyToValue.has(currentKey) ? null : currentKey;
  }, [currentKey, keyToValue]);

  return (
    <Select
      value={currentKey}
      onValueChange={(k) => {
        if (!k) return onChange(null);
        if (allowClear && k === CLEAR) return onChange(null);
        if (k === UNKNOWN) return onChange("");
        // Re-selecting the orphan is a no-op, not a clear: it is absent from
        // keyToValue, so the lookup below would fall through to null and wipe
        // the very value we are surfacing.
        if (k === orphanKey) return onChange(k);
        onChange(keyToValue.get(k) ?? null);
      }}
      disabled={disabled}
    >
      <SelectTrigger>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {allowClear ? <SelectItem value={CLEAR}>{clearLabel}</SelectItem> : null}
        {orphanKey ? (
          <SelectItem key={orphanKey} value={orphanKey}>
            {orphanKey}
          </SelectItem>
        ) : null}
        {normalized
          .filter((o) => o.__key && o.__key !== CLEAR)
          .map((o) => (
            <SelectItem key={o.__key} value={o.__key}>
              {o.label}
            </SelectItem>
          ))}
      </SelectContent>
    </Select>
  );
}
