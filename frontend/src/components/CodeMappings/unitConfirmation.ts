export interface UnitConsistencyWarning {
  source_code?: string;
  warning_count?: number;
  destination_concept_name?: string;
  property?: string;
  expected_units?: string[];
  observed_units?: { unit: string; count: number; compatible?: boolean }[];
  observed_unit_count_kind?: "mappings";
}

export function unitConfirmation(error: unknown): UnitConsistencyWarning | null {
  const data = (error as { response?: { data?: { code?: string; unit_consistency?: UnitConsistencyWarning } } })
    ?.response?.data;
  return data?.code === "unit_mismatch_confirmation_required"
    ? data.unit_consistency ?? null
    : null;
}

export function confirmUnitOverride(warning: UnitConsistencyWarning): boolean {
  const incompatible = (warning.observed_units || [])
    .filter((row) => row.compatible !== true)
    .map((row) => {
      const noun = warning.observed_unit_count_kind === "mappings" ? "mapping" : "result";
      return `${row.unit} (${row.count.toLocaleString()} ${noun}${row.count === 1 ? "" : "s"})`;
    })
    .join(", ");
  const expected = warning.expected_units?.join(", ") || "no example/canonical units recorded";
  const affected = warning.warning_count
    ? `${warning.warning_count.toLocaleString()} mapping${warning.warning_count === 1 ? "" : "s"} in this group have`
    : `${warning.source_code || "This source code"} has`;
  return window.confirm(
    `${affected} stored units that may not fit ${warning.destination_concept_name || "the selected LOINC destination"}.\n\n`
    + `Observed: ${incompatible || "unknown"}\nExpected/compatible reference: ${expected}\n`
    + `Property: ${warning.property || "not quantitative or unknown"}\n\nApprove anyway?`,
  );
}
