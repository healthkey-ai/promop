import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { entryStatus, lookupCoverage, type ValueMapping } from "./answerConcept";

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

const concept = { concept_id: 9, vocabulary_id: "SNOMED", concept_code: "55561003", concept_name: "Active", domain_id: "Observation" };
const mapping = (over: Partial<ValueMapping> = {}): ValueMapping => ({
  id: 7, role: "value_as_concept", outcome: "mapped", status: "approved", target: concept,
  vocabulary_id: "SNOMED", concept_code: "55561003", source_resolution: "resolved", target_state: "ok",
  rationale: "", vocabulary_release: "2026-09", proposed_by: null, reviewer: null, ...over,
});

const source = { kind: "vocabulary", name: "her2-status", label: "Her2Status", enforced: false, source_name: "", source_url: "" };
let body: Record<string, unknown>;

beforeEach(() => {
  vi.clearAllMocks();
  body = {
    field_name: "her2_status", source,
    options: [
      { code: "pos", title: "Positive", choice_id: 5, value_mappings: [mapping()] },
      { code: "neg", title: "Negative", choice_id: null, value_mappings: [] },
      { code: "eq", title: "Equivocal", choice_id: 6, value_mappings: [mapping({ id: 8, status: "proposed" })] },
    ],
    orphaned: [{ choice_id: 9, title: "Positive (old)", value_mappings: [mapping({ id: 10 })] }],
  };
  mockGet.mockImplementation(async (url: string) =>
    ({ data: url.startsWith("/v1/field-choices/lookup/") ? body : [] }));
  mockPost.mockResolvedValue({ data: { choice_id: 12, title: "Negative" } });
});

const open = async (canApprove = true) => {
  render(<FieldChoiceEditor fieldName="her2_status" onClose={() => {}} canApprove={canApprove} />);
  const section = await screen.findByRole("region", { name: "Lookup options" });
  fireEvent.click(within(section).getByRole("button", { name: "Show entries" }));
  return section;
};

describe("helpers", () => {
  it("says where an entry's decision stands in a phrase", () => {
    expect(entryStatus(undefined)).toBe("No concept decided");
    expect(entryStatus([mapping()])).toBe("Approved: SNOMED 55561003 Active");
    expect(entryStatus([mapping({ outcome: "no_equivalent", target: null })])).toBe("Approved: no standard equivalent");
    expect(entryStatus([mapping({ status: "proposed" })])).toBe("Proposed: SNOMED 55561003 Active (proposed)");
  });

  it("counts decided, waiting and unusable entries", () => {
    expect(lookupCoverage([
      { value_mappings: [mapping()] },
      { value_mappings: [mapping({ status: "proposed" })] },
      { value_mappings: [mapping({ target_state: "retired" })] },
      { value_mappings: [] },
    ])).toEqual({ total: 4, decided: 1, proposed: 1, attention: 1, undecided: 1 });
  });
});

describe("a lookup's entries in the choice editor", () => {
  it("shows how many entries have a concept, and each entry's state", async () => {
    const section = await open();
    expect(within(section).getByRole("status")).toHaveTextContent("1 of 3 entries have a concept decided, 1 waiting for approval");
    const rows = within(section).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("Positive");
    expect(rows[0]).toHaveTextContent("Approved: SNOMED 55561003 Active");
    expect(rows[1]).toHaveTextContent("No concept decided");
    expect(rows[2]).toHaveTextContent("Proposed");
  });

  it("opens the panel of an entry that already has a decision without making anything", async () => {
    const section = await open();
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Positive" }));
    expect(await within(section).findByRole("group", { name: "Concept for Positive" })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("makes the row the first time a curator decides something for an entry, then offers the panel", async () => {
    const section = await open();
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Negative" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/field-choices/lookup/answer/", { field_name: "her2_status", code: "neg" }));
    expect(await within(section).findByRole("group", { name: "Concept for Negative" })).toBeInTheDocument();
  });

  it("proposes through the answer's own endpoint on the row it made", async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url.startsWith("/v1/field-choices/lookup/answer")) return { data: [] };
      if (url.startsWith("/v1/field-choices/lookup/")) return { data: body };
      if (url.includes("/targets/")) return { data: { count: 1, results: [concept], rules: { required_domain: null, excluded_domains: [], excluded_vocabularies: [] } } };
      return { data: [] };
    });
    const section = await open();
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Negative" }));
    const panel = await within(section).findByRole("group", { name: "Concept for Negative" });
    fireEvent.click(within(panel).getByRole("button", { name: "Map Negative to a concept" }));
    fireEvent.change(within(panel).getByLabelText("Concept search"), { target: { value: "active" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Search" }));
    fireEvent.click(await within(panel).findByRole("button", { name: "Propose Active for Negative" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/field-choices/12/value-mappings/", expect.objectContaining({ concept_id: 9 })));
  });

  it("says when an entry could not be started, and shows no panel", async () => {
    mockPost.mockRejectedValue({ response: { data: { detail: "The lookup has no such entry." } } });
    const section = await open();
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Negative" }));
    expect(await within(section).findByRole("alert")).toHaveTextContent("The lookup has no such entry.");
    expect(within(section).queryByRole("group", { name: "Concept for Negative" })).not.toBeInTheDocument();
  });

  it("lists a decision about an entry the lookup no longer has", async () => {
    const section = await open();
    expect(within(section).getByText("No longer in the lookup")).toBeInTheDocument();
    expect(within(section).getByText(/Positive \(old\): Approved/)).toBeInTheDocument();
  });

  it("only an approver sees Approve on a proposal here", async () => {
    let section = await open(false);
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Equivocal" }));
    await within(section).findByRole("group", { name: "Concept for Equivocal" });
    expect(screen.queryByRole("button", { name: /Approve proposal for Equivocal/ })).not.toBeInTheDocument();
    document.body.innerHTML = "";
    section = await open(true);
    fireEvent.click(within(section).getByRole("button", { name: "Concept for Equivocal" }));
    expect(await screen.findByRole("button", { name: "Approve proposal for Equivocal" })).toBeInTheDocument();
  });
});
