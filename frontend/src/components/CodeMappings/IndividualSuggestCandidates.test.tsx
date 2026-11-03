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

describe("IndividualSuggestCandidates LOINC preference for labs", () => {
  it("says LOINC is preferred and why, even with no axis settled", () => {
    render(<IndividualSuggestCandidates running={false} selectedId="" onSelect={vi.fn()} activity={[
      { stage: "ranked", suggested: mass, candidates: [mass, snomed],
        source_axes: { axes: {}, hints: {}, expected_vocabulary: {
          vocabulary_id: "LOINC", reason: "The sending system filed it as a laboratory result" } } },
    ]} />);
    expect(screen.getByTestId("expected-vocabulary")).toHaveTextContent(
      "LOINC preferred: The sending system filed it as a laboratory result");
  });
});

describe("IndividualSuggestCandidates for one axis variant (#2036)", () => {
  it("names the variant, offers Use on every row, Use winner on the winner, and marks the current destination", () => {
    const onSelect = vi.fn();
    render(<IndividualSuggestCandidates activity={activity} running={false} selectedId="15" onSelect={onSelect} variant="%" />);
    expect(screen.getByRole("heading", { name: "Suggestion candidates for the % variant" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Use a candidate to make it this variant's destination, then approve it under LOINC axes to route its units.");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByRole("button", { name: `Use ${mass.concept_name}` })).toHaveTextContent("Use winner");
    expect(within(rows[1]).getByRole("button", { name: `Use ${snomed.concept_name}` })).toHaveTextContent("Use");
    expect(rows[1]).toHaveTextContent("Current destination");
    fireEvent.click(within(rows[0]).getByRole("button", { name: `Use ${mass.concept_name}` }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ concept_id: 11 }));
  });

  it("keeps the mapping wording and a plain Use button when no variant is named", () => {
    render(<IndividualSuggestCandidates activity={activity} running={false} selectedId="" onSelect={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Suggestion candidates" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Choose a candidate to fill the destination, then Save your mapping.");
    expect(screen.getByRole("button", { name: `Use ${mass.concept_name}` })).toHaveTextContent("Use winner");
  });
});

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

const fracture = { concept_id: 21, concept_name: "Fracture of right femur", concept_code: "1000001", vocabulary_id: "SNOMED",
  snomed: { hierarchy: "Disorder", semantic_tag: "disorder", status: "Defined" as const, laterality: "Right" as const,
    relation: "exact" as const, levels: 0, lateralised: true as const,
    attributes: [{ relationship_id: "Has finding site", label: "Finding site", values: ["Right femur"], verdict: "match" as const }] } };
const femurFx = { concept_id: 22, concept_name: "Fracture of femur", concept_code: "71620000", vocabulary_id: "SNOMED",
  snomed: { hierarchy: "Disorder", semantic_tag: "disorder", status: "Primitive" as const, laterality: null, relation: "general" as const, levels: 1,
    laterality_not_captured: "Right",
    attributes: [
      { relationship_id: "Has finding site", label: "Finding site", values: ["Femur"], verdict: "general" as const },
      { relationship_id: "Has asso morph", label: "Morphology", values: [], verdict: "unknown" as const, primitive_gap: true as const },
    ] } };
const neckFx = { concept_id: 23, concept_name: "Fracture of neck of right femur", concept_code: "1000002", vocabulary_id: "SNOMED" };
const leftFx = { concept_id: 24, concept_name: "Fracture of left femur", concept_code: "1000003", vocabulary_id: "SNOMED" };
const snomedActivity: CandidateActivity[] = [
  { stage: "candidates", strategy: "hierarchy", candidates: [femurFx, fracture, neckFx, leftFx] },
  { stage: "ranked", suggested: fracture, candidates: [fracture, femurFx],
    source_snomed: { references: [{ concept_id: 22, concept_name: "Fracture of femur", concept_code: "71620000", hierarchy: "Disorder" }],
      reference_basis: "maps_to", hierarchy: { value: "Disorder", basis: "reference" }, laterality: { value: "Right", basis: "text" }, attributes: [] },
    axis_excluded: [
      { ...leftFx, exclusion_reason: "conflict", snomed_conflicts: ["Laterality"] },
      { ...neckFx, exclusion_reason: "more_specific", snomed_conflicts: [],
        snomed: { hierarchy: "Disorder", semantic_tag: "disorder", status: "Defined", laterality: "Right", relation: "specific", levels: 1, attributes: [] } },
    ] },
];

describe("IndividualSuggestCandidates SNOMED", () => {
  it("shows tag, relation, attribute chips, laterality warning and the split disclosure with Use anyway", () => {
    const onSelect = vi.fn();
    render(<IndividualSuggestCandidates activity={snomedActivity} running={false} selectedId="" onSelect={onSelect} />);
    expect(screen.getByLabelText("Source SNOMED meaning")).toHaveTextContent("Source means at least: Fracture of femur (maps to) · Disorder · Right");
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Fracture of right femur");
    expect(rows[0]).toHaveTextContent("H");
    const exact = within(rows[0]).getByLabelText("SNOMED evidence");
    expect(exact).toHaveTextContent("(disorder)");
    expect(exact.querySelector('[data-relation="exact"]')).toHaveTextContent("exact");
    expect(exact).toHaveTextContent("lateralised");
    expect(exact.querySelector('[data-attribute="Has finding site"]')).toHaveAttribute("data-verdict", "match");
    const general = within(rows[1]).getByLabelText("SNOMED evidence");
    expect(general.querySelector('[data-relation="general"]')).toHaveTextContent("↑ 1 more general");
    expect(general).toHaveTextContent("primitive");
    expect(general.querySelector('[data-attribute="Has finding site"]')).toHaveAttribute("data-verdict", "general");
    expect(general.querySelector('[data-attribute="Has asso morph"]')).toHaveAttribute("title", "Primitive concept: attribute not defined, not a mismatch");
    expect(within(rows[1]).getByRole("note")).toHaveTextContent("Source says Right; this concept has no side — record laterality separately.");
    // SNOMED exclusions do not use the LOINC wording.
    expect(screen.queryByRole("button", { name: /candidates? hidden: conflict on/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "1 contradict the source: Laterality" }));
    const contradict = screen.getByRole("list", { name: "SNOMED candidates that contradict the source" });
    expect(contradict).toHaveTextContent("Fracture of left femur");
    expect(contradict).toHaveTextContent("conflict on Laterality");
    expect(within(contradict).queryByRole("button", { name: /anyway/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "1 more specific than the source supports — need curator evidence" }));
    const specific = screen.getByRole("list", { name: "SNOMED candidates more specific than the source" });
    expect(specific.querySelector('[data-relation="specific"]')).toHaveTextContent("more specific");
    fireEvent.click(within(specific).getByRole("button", { name: "Use Fracture of neck of right femur anyway" }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ concept_id: 23 }));
  });
});
