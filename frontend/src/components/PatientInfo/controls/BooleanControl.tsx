import SelectControl from "./SelectControl";

export default function BooleanControl({
  value,
  disabled,
  onChange,
  triState = false,
}: {
  value: boolean | null | undefined;
  disabled?: boolean;
  onChange: (v: boolean | null) => void;
  triState?: boolean;
}) {
  const strValue = value === true
    ? "true"
    : value === false
      ? "false"
      : triState
        ? "unknown"
        : "";

  return (
    <SelectControl
      value={strValue}
      disabled={disabled}
      placeholder="Select…"
      treatEmptyOptionAsUnknown={false}
      allowClear={!triState}
      clearLabel="— None —"
      options={[
        { value: "true", label: "Yes" },
        { value: "false", label: "No" },
        ...(triState ? [{ value: "unknown", label: "Unknown" }] : []),
      ]}
      onChange={(v) => {
        if (v === null || v === "" || v === "unknown") return onChange(null);
        onChange(String(v) === "true");
      }}
    />
  );
}
