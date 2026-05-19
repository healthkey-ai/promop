import React, { useMemo } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../shadcn/select";

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

  return (
    <Select
      value={currentKey}
      onValueChange={(k) => {
        if (!k) return onChange(null);
        if (allowClear && k === CLEAR) return onChange(null);
        if (k === UNKNOWN) return onChange("");
        onChange(keyToValue.get(k) ?? null);
      }}
      disabled={disabled}
    >
      <SelectTrigger>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {allowClear ? <SelectItem value={CLEAR}>{clearLabel}</SelectItem> : null}
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
