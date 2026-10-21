import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import IndividualSuggestCandidates from "./IndividualSuggestCandidates";
import type { CandidateActivity } from "./SuggestCandidates";

const axes = { component: "Glucose", property: "MCnc", time_aspect: "Pt", system: "Ser/Plas", scale_type: "Qn", method_type: "" };
const mass = { concept_id: 11, concept_name: "Glucose [Mass/volume] in Serum or Plasma", concept_code: "2345-7", vocabulary_id: "LOINC",
  loinc_axes: axes, axis_match: { component: "match", property: "match", time_aspect: "match", system: "match", scale_type: "match", method_type: "unknown" } as const,
  axes_matched: 5, axes_compatible: 0 };
const urine = { concept_id: 14, concept_name: "Glucose [Mass/volume] in Urine", concept_code: "5792-7", vocabulary_id: "LOINC" };
const snomed = { concept_id: 15, concept_name: "Glucose measurement", concept_code: "36048009", vocabulary_id: "SNOMED", loinc_axes: null };
const activity: CandidateActivity[] = [
  { stage: "candidates", strategy: "lexical", candidates: [snomed, urine] },
  { stage: "candidates", strategy: "axes", candidates: [mass] },
  { stage: "ranked", suggested: mass, candidates: [mass, snomed],
    source_axes: { axes: { system: { value: "Ser/Plas", basis: "specimen", evidence: "specimen.type = Serum" } }, hints: { method_type: "maybe automated count" } },
    axis_excluded: [{ concept_id: 14, concept_name: urine.concept_name, concept_code: urine.concept_code, vocabulary_id: "LOINC",
      loinc_axes: { ...axes, system: "Urine" }, axis_match: { system: "conflict" }, axis_conflicts: ["system"], retrieval: "lexical" }] },
];

describe("IndividualSuggestCandidates LOINC axes", () => {
  it("orders by axes matched, shows chips, and discloses the conflicting candidates", () => {
    const onSelect = vi.fn();
    render(<IndividualSuggestCandidates activity={activity} running={false} selectedId="" onSelect={onSelect} />);
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Glucose [Mass/volume] in Serum or Plasma");
    expect(rows[0]).toHaveTextContent("5/6 axes");
    expect(within(rows[0]).getByLabelText("LOINC axes").querySelector('[data-axis="method_type"]')).toHaveTextContent("—");
    // A non-LOINC candidate carries no axis chips.
    expect(within(rows[1]).queryByLabelText("LOINC axes")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Source LOINC axes")).toHaveTextContent("System Ser/Plas");
    expect(screen.getByLabelText("Source LOINC axes")).toHaveTextContent("Hints: Method maybe automated count");
    fireEvent.click(screen.getByRole("button", { name: "1 candidate hidden: conflict on System" }));
    const hidden = screen.getByRole("list", { name: "Candidates hidden by LOINC axes" });
    expect(hidden).toHaveTextContent("Glucose [Mass/volume] in Urine");
    expect(hidden.querySelector('[data-axis="system"]')).toHaveAttribute("data-verdict", "conflict");
    fireEvent.click(rows[0]);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ concept_id: 11 }));
  });
});
