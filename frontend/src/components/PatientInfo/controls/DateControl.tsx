import { useState } from "react";
import { Input } from "@/components/shadcn/input";

export default function DateControl({
  value,
  disabled,
  onChange,
  precision,
}: {
  value?: string | null;
  disabled?: boolean;
  onChange: (v: unknown) => void;
  precision?: 'day' | 'month' | 'year' | null;
}) {
  const source = `${precision ?? 'day'}:${value ?? ''}`;
  const yearVal = value ? value.substring(0, 4) : '';
  const [yearDraft, setYearDraft] = useState({ source, text: yearVal });
  if (yearDraft.source !== source) {
    setYearDraft({ source, text: yearVal });
  }
  if (precision === 'year') {
    return (
      <Input
        disabled={disabled}
        type="number"
        min={1900}
        max={new Date().getFullYear()}
        placeholder="YYYY"
        value={yearDraft.text}
        onChange={(e) => {
          const raw = e.target.value;
          setYearDraft({ source, text: raw });
          if (!raw) { onChange(null); return; }
          // Keep incomplete typing locally; only complete valid years are dates.
          if (!/^\d{4}$/.test(raw)) return;
          const y = parseInt(raw, 10);
          if (isNaN(y) || y < 1900 || y > new Date().getFullYear()) return;
          onChange(`${y}-01-01`);
        }}
        onBlur={() => setYearDraft({ source, text: yearVal })}
      />
    );
  }
  if (precision === 'month') {
    const monthVal = value ? value.substring(0, 7) : '';
    return (
      <Input
        disabled={disabled}
        type="month"
        value={monthVal}
        onChange={(e) => {
          const m = e.target.value;
          onChange(m ? `${m}-01` : null);
        }}
      />
    );
  }
  return (
    <Input
      disabled={disabled}
      type="date"
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    />
  );
}
