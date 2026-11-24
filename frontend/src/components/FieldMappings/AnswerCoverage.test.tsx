import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { answerCoverage, coverageBucket, inCoverageView, type ValueMapping } from "./answerConcept";

const mockGet = vi.fn();
const mockPatch = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

const concept = { concept_id: 1, vocabulary_id: "SNOMED", concept_code: "1", concept_name: "Thing", domain_id: "Observation" };
const mapping = (over: Partial<ValueMapping> = {}): ValueMapping => ({
  id: 1, role: "value_as_concept", outcome: "mapped", status: "approved", target: concept,
  vocabulary_id: "SNOMED", concept_code: "1", source_resolution: "resolved", target_state: "ok",
  rationale: "", vocabulary_release: "2026-09", proposed_by: null, reviewer: null, ...over,
});
const answer = (id: number, display: string, value_mappings?: ValueMapping[], retired = false) => ({
  id, field_name: "disease_activity", display, sort_order: id, codes: [], value_mappings, retired,
});

const FIELD = [
  answer(1, "Alpha", [mapping({ id: 11 })]),
  answer(2, "Beta", [mapping({ id: 12, outcome: "no_equivalent", target: null })]),
  answer(3, "Gamma", [mapping({ id: 13, status: "proposed", target_state: null })]),
  answer(4, "Delta", [mapping({ id: 14, target_state: "retired" })]),
  answer(5, "Epsilon", []),
  answer(6, "Zeta", [mapping({ id: 16 })], true),
  // An approved concept that cannot be used, with its replacement waiting.
  answer(7, "Eta", [mapping({ id: 17, target_state: "unresolved" }), mapping({ id: 18, status: "proposed", target_state: null })]),
];

describe("answer coverage", () => {
  it("puts each answer in one bucket, and keeps retired ones out of the counts", () => {
    expect(FIELD.map(coverageBucket)).toEqual(["done", "done", "proposed", "attention", "unmapped", "retired", "attention"]);
    expect(answerCoverage(FIELD)).toEqual({
      offered: 6, mapped: 1, no_equivalent: 1, waiting: 2, attention: 2, unmapped: 1, retired: 1,
    });
  });

  it("finds a replacement proposal for an approved concept that cannot be used", () => {
    expect(FIELD.filter((c) => inCoverageView(c, "waiting")).map((c) => c.display)).toEqual(["Gamma", "Eta"]);
  });

  it("treats a role other than the answer's concept as no decision", () => {
    expect(coverageBucket({ value_mappings: [mapping({ role: "main_concept" })] })).toBe("unmapped");
  });
});

describe("FieldChoiceEditor coverage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockResolvedValue({ data: FIELD });
  });

  it("states the coverage of the answers, apart from the question", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("2 of 6 answers decided (1 with a concept, 1 with no standard equivalent)");
    expect(status).toHaveTextContent("2 with an approved concept that cannot be used");
    expect(status).toHaveTextContent("1 with nothing approved or proposed");
    expect(status).toHaveTextContent("2 with a proposal waiting for approval");
    expect(status).toHaveTextContent("Retired answers are not counted");
    expect(screen.getByText(/counts the answers only/)).toBeInTheDocument();
  });

  it("lists only the answers that need attention, and says when a view is empty", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    const chips = await screen.findByRole("group", { name: "Show answers" });
    fireEvent.click(within(chips).getByRole("button", { name: "Needs attention 2" }));
    expect(screen.getByText("Delta")).toBeInTheDocument();
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
    expect(within(chips).getByRole("button", { name: "Needs attention 2" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(chips).getByRole("button", { name: "Retired 1" }));
    expect(screen.getByText("Zeta")).toBeInTheDocument();
    fireEvent.click(within(chips).getByRole("button", { name: "All 7" }));
    expect(screen.getByText("Alpha")).toBeInTheDocument();
  });

  it("cannot reorder a filtered view", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    const chips = await screen.findByRole("group", { name: "Show answers" });
    fireEvent.click(within(chips).getByRole("button", { name: "Waiting for approval 2" }));
    for (const button of document.querySelectorAll("button svg.lucide-chevron-up, button svg.lucide-chevron-down")) {
      expect(button.closest("button")).toBeDisabled();
    }
  });

  it("keeps a row in view after a decision moves it out of the filter, until the filter changes", async () => {
    mockGet.mockResolvedValue({ data: FIELD });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove />);
    const chips = await screen.findByRole("group", { name: "Show answers" });
    fireEvent.click(within(chips).getByRole("button", { name: "Waiting for approval 2" }));
    expect(screen.queryByText("Epsilon")).not.toBeInTheDocument();
    // Gamma's proposal is approved elsewhere: the list is read again without it waiting.
    mockGet.mockResolvedValue({ data: FIELD.map((c) => c.id === 3 ? answer(3, "Gamma", [mapping({ id: 13 })]) : c) });
    fireEvent.click(screen.getByRole("button", { name: "Approve proposal for Gamma" }));
    await screen.findByText(/^Approved: /);
    expect(screen.getByText("Gamma")).toBeInTheDocument();
    expect(within(chips).getByRole("button", { name: "Waiting for approval 1" })).toBeInTheDocument();
    fireEvent.click(within(chips).getByRole("button", { name: "Waiting for approval 1" }));
    expect(screen.queryByText("Gamma")).not.toBeInTheDocument();
  });

  it("shows a new answer by returning to the whole list", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    const chips = await screen.findByRole("group", { name: "Show answers" });
    fireEvent.click(within(chips).getByRole("button", { name: "Retired 1" }));
    fireEvent.change(screen.getByPlaceholderText(/New choice display name/), { target: { value: "Theta" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Show answers" }))
      .getByRole("button", { name: /^All/ })).toHaveAttribute("aria-pressed", "true"));
  });

  it("shows no coverage to someone who cannot read the mapping pages", async () => {
    mockGet.mockResolvedValue({ data: FIELD.map((c) => ({ ...c, value_mappings: undefined })) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    await screen.findByText("Alpha");
    expect(screen.queryByRole("group", { name: "Show answers" })).not.toBeInTheDocument();
  });
});
