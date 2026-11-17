import { useMemo } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/select";

type Option = { value: unknown; label: string; code?: string | null };

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
  ariaLabel,
  onChange,
}: {
  value: unknown;
  options: Option[];
  disabled?: boolean;
  placeholder?: string;
  allowClear?: boolean;
  clearLabel?: string;
  treatEmptyOptionAsUnknown?: boolean;
  ariaLabel?: string;
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

  // A vocabulary may carry the code a record stores beside the display it
  // shows (`{value: 'Male', code: 'M'}` from the descriptor's FieldChoice rows).
  // The record holds the code, so matching only `value` made the stored answer
  // look like an extra, unnamed option beside the real one — the Gender menu
  // listed both `M` and `Male`. A stored code selects its own option, and a
  // selection still writes the option's `value`.
  const codeToKey = useMemo(() => {
    const m = new Map<string, string>();
    for (const o of normalized) {
      const code = o.code ? keyOf(o.code) : '';
      if (code) m.set(code, o.__key);
    }
    return m;
  }, [normalized]);
  const currentKey = useMemo(() => {
    const k = keyOf(value);
    if (!k && treatEmptyOptionAsUnknown) return UNKNOWN;
    if (k && !keyToValue.has(k) && codeToKey.has(k)) return codeToKey.get(k)!;
    return k;
  }, [value, treatEmptyOptionAsUnknown, keyToValue, codeToKey]);

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
      <SelectTrigger aria-label={ariaLabel}>
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
