import React from "react";
import { Input } from "../../shadcn/input";

export default function DateControl({
  value,
  disabled,
  onChange,
}: {
  value?: string | null;
  disabled?: boolean;
  onChange: (v: unknown) => void;
}) {
  return (
    <Input
      disabled={disabled}
      type="date"
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    />
  );
}
