interface Props {
  domain_id: string;
  measurement_type?: "qualitative" | "quantitative";
  suggested_unit?: string;
  example_units?: string[];
}

/** Input cues are only meaningful for concepts in the Measurement domain. */
export default function ConceptInputDetails({ domain_id, measurement_type, suggested_unit, example_units }: Props) {
  let label = domain_id || "Domain unavailable";
  if (domain_id === "Measurement") {
    label = measurement_type === "quantitative"
      ? `Quantitative${suggested_unit ? ` · Suggested unit: ${suggested_unit}` : ""}`
      : measurement_type === "qualitative" ? "Qualitative" : "Measurement · Input type unavailable";
  }
  if (domain_id === "Measurement" && example_units?.length) label += ` · Example units: ${example_units.join(", ")}`;
  return <span className="mt-1 block text-slate-500">{label}</span>;
}
