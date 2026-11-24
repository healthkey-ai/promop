import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { answerState, errorText, type ValueMapping } from "./answerConcept";

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

const concept = { concept_id: 4014046, vocabulary_id: "SNOMED", concept_code: "103338009", concept_name: "In remission", domain_id: "Observation" };

const mapping = (over: Partial<ValueMapping> = {}): ValueMapping => ({
  id: 7, role: "value_as_concept", outcome: "mapped", status: "proposed", target: concept,
  vocabulary_id: "SNOMED", concept_code: "103338009", source_resolution: "resolved",
  target_state: null, rationale: "", vocabulary_release: "2026-09", proposed_by: null, reviewer: null,
  ...over,
});

const choice = (value_mappings?: ValueMapping[]) => ({
  id: 1, field_name: "disease_activity", display: "In remission", sort_order: 0, codes: [], value_mappings,
});

describe("answerState", () => {
  it("is unmapped with nothing decided, and ignores other roles", () => {
    expect(answerState(undefined).kind).toBe("unmapped");
    expect(answerState([mapping({ role: "main_concept", status: "approved" })]).kind).toBe("unmapped");
  });

  it("says a proposal is waiting, and when its concept is not loaded", () => {
    const s = answerState([mapping()]);
    expect(s.kind).toBe("proposed");
    expect(s.summary).toContain("waiting for approval");
    const missing = answerState([mapping({ target: null })]);
    expect(missing.warning).toContain("not loaded here");
  });

  it("tells an approved concept, a reviewed no-equivalent and an unusable concept apart", () => {
    expect(answerState([mapping({ status: "approved", target_state: "ok" })]).kind).toBe("approved");
    expect(answerState([mapping({ status: "approved", outcome: "no_equivalent", target: null })]).kind)
      .toBe("approved_no_equivalent");
    const retired = answerState([mapping({ status: "approved", target_state: "retired" })]);
    expect(retired.kind).toBe("approved_invalid");
    expect(retired.warning).toContain("retired");
  });

  it("keeps the decision in force beside a proposal waiting to replace it", () => {
    const s = answerState([mapping({ id: 1, status: "approved", target_state: "ok" }), mapping({ id: 2 })]);
    expect(s.kind).toBe("approved");
    expect(s.proposed?.id).toBe(2);
  });

  it("shows the server's reason and never a raw error", () => {
    expect(errorText({ response: { data: { detail: "Not standard." } } }, "x")).toBe("Not standard.");
    expect(errorText(new Error("Request failed with status code 500"), "Try again.")).toBe("Try again.");
  });
});

describe("answer concept panel in the choice editor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPost.mockResolvedValue({ data: {} });
    mockGet.mockImplementation((url: string) =>
      url.includes("/targets/")
        ? Promise.resolve({ data: { results: [concept], rules: { required_domain: null, excluded_domains: [], excluded_vocabularies: [] } } })
        : Promise.resolve({ data: [choice([])] }));
  });

  it("searches with the server's rules and proposes the chosen concept", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Map In remission to a concept" }));
    fireEvent.change(screen.getByLabelText("Concept search"), { target: { value: "remission" } });
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "Same meaning" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Propose In remission for In remission" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/field-choices/1/value-mappings/",
      { role: "value_as_concept", concept_id: 4014046, rationale: "Same meaning" },
    ));
    expect(mockGet).toHaveBeenCalledWith(
      "/v1/field-choices/1/value-mappings/targets/", { params: { q: "remission", role: "value_as_concept" } });
  });

  it("does not search for fewer than three characters", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Map In remission to a concept" }));
    fireEvent.change(screen.getByLabelText("Concept search"), { target: { value: "re" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 3 characters");
    expect(mockGet).not.toHaveBeenCalledWith(expect.stringContaining("/targets/"), expect.anything());
  });

  it("needs a reason to record that no standard concept exists", async () => {
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Record no equivalent for In remission" }));
    const send = screen.getByRole("button", { name: "Propose no equivalent" });
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "Local phrase" } });
    fireEvent.click(send);
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/field-choices/1/value-mappings/",
      { role: "value_as_concept", outcome: "no_equivalent", rationale: "Local phrase" },
    ));
  });

  it("shows the server's refusal and keeps the form", async () => {
    mockPost.mockRejectedValue({ response: { data: { detail: "SNOMED 1 is retired." } } });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Record no equivalent for In remission" }));
    fireEvent.change(screen.getByLabelText("Rationale"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Propose no equivalent" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("SNOMED 1 is retired.");
    expect(screen.getByLabelText("Rationale")).toBeInTheDocument();
  });

  it("offers approve and reject on a proposal only to approvers", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ data: [choice([mapping()])] }));
    const { unmount } = render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    expect(await screen.findByText(/waiting for approval/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve proposal for In remission" })).toBeNull();
    unmount();

    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve proposal for In remission" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/field-choices/1/value-mappings/7/approve/", {}));
  });

  it("asks before withdrawing an approval", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ data: [choice([mapping({ status: "approved", target_state: "ok" })])] }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: "Withdraw approval for In remission" }));
    expect(confirm).toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Withdraw approval for In remission" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/field-choices/1/value-mappings/7/reject/", {}));
  });

  it("keeps the list, and what was typed in another answer, when one decision is saved", async () => {
    const two = [choice([mapping()]), { ...choice([]), id: 2, display: "Relapse" }];
    mockGet.mockImplementation(() => Promise.resolve({ data: two }));
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: "Map Relapse to a concept" }));
    const box = screen.getAllByLabelText("Concept search")[0];
    fireEvent.change(box, { target: { value: "relap" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve proposal for In remission" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Loading...")).toBeNull();
    expect(screen.getByLabelText("Concept search")).toHaveValue("relap");
  });

  it("reads the list again when a decision is refused, so the buttons are not stale", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ data: [choice([mapping()])] }));
    mockPost.mockRejectedValue({ response: { data: { detail: "Only a proposal can be approved." } } });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve proposal for In remission" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only a proposal can be approved.");
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it("does not offer Approve on a proposal whose concept is not loaded", async () => {
    mockGet.mockImplementation(() => Promise.resolve({ data: [choice([mapping({ target: null })])] }));
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} canApprove />);
    expect(await screen.findByRole("button", { name: "Approve proposal for In remission" })).toBeDisabled();
  });

  it("says when only the first concepts are shown", async () => {
    mockGet.mockImplementation((url: string) =>
      url.includes("/targets/")
        ? Promise.resolve({ data: { count: 60, results: [concept], rules: { required_domain: "Condition", excluded_domains: [], excluded_vocabularies: [] } } })
        : Promise.resolve({ data: [choice([])] }));
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Map In remission to a concept" }));
    fireEvent.change(screen.getByLabelText("Concept search"), { target: { value: "remission" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/Showing the first 1 of 60/)).toBeInTheDocument();
    expect(screen.getByText(/in the Condition domain/)).toBeInTheDocument();
  });

  it("leaves Escape in a text box to the box", async () => {
    const onClose = vi.fn();
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={onClose} />);
    fireEvent.click(await screen.findByRole("button", { name: "Map In remission to a concept" }));
    fireEvent.keyDown(screen.getByLabelText("Concept search"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
