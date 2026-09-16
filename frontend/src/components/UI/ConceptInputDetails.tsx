interface Props {
  domain_id: string;
  measurement_type?: "qualitative" | "quantitative";
  suggested_unit?: string;
}

/** Input cues are only meaningful for concepts in the Measurement domain. */
export default function ConceptInputDetails({ domain_id, measurement_type, suggested_unit }: Props) {
  let label = domain_id || "Domain unavailable";
  if (domain_id === "Measurement") {
    label = measurement_type === "quantitative"
      ? `Quantitative${suggested_unit ? ` · Unit: ${suggested_unit}` : ""}`
      : measurement_type === "qualitative" ? "Qualitative" : "Measurement · Input type unavailable";
  }
  return <span className="mt-1 block text-slate-500">{label}</span>;
}
