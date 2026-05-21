import SelectControl from "./SelectControl";

export default function BooleanControl({
  value,
  disabled,
  onChange,
}: {
  value: boolean | null | undefined;
  disabled?: boolean;
  onChange: (v: boolean | null) => void;
}) {
  const strValue = value === true ? "true" : value === false ? "false" : "";

  return (
    <SelectControl
      value={strValue}
      disabled={disabled}
      placeholder="Select…"
      treatEmptyOptionAsUnknown={false}
      allowClear={true}
      clearLabel="— None —"
      options={[
        { value: "true", label: "Yes" },
        { value: "false", label: "No" },
      ]}
      onChange={(v) => {
        if (v === null || v === "") return onChange(null);
        onChange(String(v) === "true");
      }}
    />
  );
}
