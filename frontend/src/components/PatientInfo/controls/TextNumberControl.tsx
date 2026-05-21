import { Input } from "@/components/shadcn/input";

export default function TextNumberControl({
  type,
  value,
  placeholder,
  disabled,
  onChange,
}: {
  type: string;
  value?: string | number | null;
  placeholder?: string;
  disabled?: boolean;
  onChange: (v: unknown) => void;
}) {
  const isNumber = type === "number" || type === "int" || type === "float";

  return (
    <Input
      disabled={disabled}
      type={isNumber ? "number" : type === "email" ? "email" : "text"}
      value={value ?? ""}
      placeholder={placeholder}
      onChange={(e) => {
        const v = e.target.value;
        if (!isNumber) return onChange(v);
        if (v === "") return onChange(null);
        onChange(type === "int" ? parseInt(v, 10) : Number(v));
      }}
    />
  );
}
