import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import { FieldChoiceEditor } from "./FieldChoiceEditor";
import { AnswerDetailsPanel } from "./AnswerDetailsPanel";
import { AnswerHistory } from "./AnswerHistory";
import { actionText, newestFirst, refusalText, revisionConcept, type HistoryEntry } from "./answerConcept";

const mockGet = vi.fn();
const mockPatch = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const refused = (data: unknown) => ({ response: { data } });

const entry = (over: Partial<HistoryEntry> = {}): HistoryEntry => ({
  mapping_id: 7, revision: 1, action: "proposed", status: "proposed", outcome: "mapped",
  role: "value_as_concept", vocabulary_id: "SNOMED", concept_code: "103338009", rationale: "",
  vocabulary_release: "2026-09", changed_by: "ada", created_at: "2026-11-20T10:00:00Z",
  ...over,
});

describe("helpers", () => {
  it("reads a serializer's field errors and a detail, never the raw error", () => {
    expect(refusalText(refused({ display: ["Already a choice."] }), "x")).toBe("Already a choice.");
    expect(refusalText(refused({ detail: "No." }), "x")).toBe("No.");
    expect(refusalText(refused({ display: ["A."], aliases: ["B."] }), "x")).toBe("A. B.");
    expect(refusalText(refused("<html>boom</html>"), "fallback")).toBe("fallback");
    expect(refusalText(new Error("network"), "fallback")).toBe("fallback");
  });

  it("words history entries and puts the newest first", () => {
    expect(actionText("approved")).toBe("Approved");
    expect(actionText("odd")).toBe("odd");
    expect(revisionConcept(entry())).toBe("SNOMED 103338009");
    expect(revisionConcept(entry({ outcome: "no_equivalent" }))).toBe("no standard concept");
    const order = newestFirst([
      entry({ revision: 1, created_at: "2026-11-01T00:00:00Z" }),
      entry({ revision: 2, created_at: "2026-11-02T00:00:00Z" }),
    ]);
    expect(order.map((e) => e.revision)).toEqual([2, 1]);
  });
});

describe("AnswerDetailsPanel", () => {
  const onChanged = vi.fn();
  const panel = (over: Partial<Parameters<typeof AnswerDetailsPanel>[0]> = {}) =>
    render(<AnswerDetailsPanel choiceId={4} label="In remission" code="in_remission"
      aliases={["Remission"]} onChanged={onChanged} {...over} />);

  beforeEach(() => {
    vi.clearAllMocks();
    mockPatch.mockResolvedValue({ data: {} });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows the stable code and the other spellings", () => {
    panel();
    expect(screen.getByText("in_remission")).toBeInTheDocument();
    expect(screen.getByText(/Also written: Remission/)).toBeInTheDocument();
  });

  it("sends only what changed, and tells the curator the old label is kept", async () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit label and spellings of In remission/ }));
    expect(screen.getByText(/keeps the old one as another spelling/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "  In   full remission " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { display: "In full remission" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("sends the spellings one per line, without blanks or repeats", async () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit label and spellings/ }));
    fireEvent.change(screen.getByLabelText("Other spellings"), {
      target: { value: "Remission\n\n  CR \nCR\nComplete  response" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-choices/4/", { aliases: ["Remission", "CR", "Complete response"] }));
  });

  it("saves nothing when nothing changed", () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit label and spellings/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(mockPatch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Label")).not.toBeInTheDocument();
  });

  it("refuses an empty label without asking the server", () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit label and spellings/ }));
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it("shows the server's refusal and stays open", async () => {
    mockPatch.mockRejectedValue(refused({ display: ["'Complete' is already a choice of disease_activity in this context."] }));
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit label and spellings/ }));
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Complete" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already a choice");
    expect(screen.getByLabelText("Label")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("retires only after a confirmation, and restores without one", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const { unmount } = panel();
    fireEvent.click(screen.getByRole("button", { name: "Retire In remission" }));
    expect(mockPatch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retire In remission" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { retired: true }));
    expect(confirm).toHaveBeenCalledTimes(2);
    unmount();

    mockPatch.mockClear();
    panel({ retired: true });
    expect(screen.getByText("Retired")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore In remission" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { retired: false }));
    expect(confirm).toHaveBeenCalledTimes(2);
  });
});

describe("AnswerHistory", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGet.mockImplementation(async (url: string) => {
      if (url.endsWith("/value-mappings/")) {
        return { data: [
          { id: 7, role: "value_as_concept" },
          { id: 9, role: "main_concept" },
          { id: 8, role: "value_as_concept" },
        ] };
      }
      if (url.endsWith("/7/revisions/")) {
        return { data: [
          { ...entry(), mapping_id: undefined },
          { ...entry({ revision: 2, action: "rejected", status: "rejected", changed_by: "bo", created_at: "2026-11-21T10:00:00Z", rationale: "Wrong site" }), mapping_id: undefined },
        ] };
      }
      if (url.endsWith("/8/revisions/")) {
        return { data: [{ ...entry({ outcome: "no_equivalent", revision: 1, action: "proposed", created_at: "2026-11-22T10:00:00Z" }) }] };
      }
      throw new Error(`unexpected ${url}`);
    });
  });

  it("loads only when asked, for the concept role, and lists newest first", async () => {
    render(<AnswerHistory choiceId={4} choiceLabel="In remission" />);
    expect(mockGet).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Show the history of In remission/ }));
    const region = await screen.findByRole("region", { name: /History of In remission/ });
    expect(mockGet).toHaveBeenCalledWith("/v1/field-choices/4/value-mappings/", { params: { history: 1 } });
    expect(mockGet).not.toHaveBeenCalledWith("/v1/field-choices/4/value-mappings/9/revisions/");
    const rows = Array.from(region.querySelectorAll("li")).map((li) => li.textContent);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("no standard concept");
    expect(rows[1]).toContain("Rejected");
    expect(rows[1]).toContain("Wrong site");
    expect(rows[1]).toContain("by bo");
    expect(rows[2]).toContain("Proposed SNOMED 103338009 by ada");
  });

  it("hides again, and says when nothing was ever proposed", async () => {
    mockGet.mockImplementation(async () => ({ data: [] }));
    render(<AnswerHistory choiceId={4} choiceLabel="In remission" />);
    fireEvent.click(screen.getByRole("button", { name: /Show the history/ }));
    expect(await screen.findByText(/Nothing has been proposed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.getByRole("button", { name: /Show the history/ })).toBeInTheDocument();
  });

  it("shows the server's reason when it cannot load", async () => {
    mockGet.mockRejectedValue(refused({ detail: "Mapping access required." }));
    render(<AnswerHistory choiceId={4} choiceLabel="In remission" />);
    fireEvent.click(screen.getByRole("button", { name: /Show the history/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Mapping access required.");
  });
});

describe("FieldChoiceEditor with answer details", () => {
  const choices = (over: Record<string, unknown> = {}) => [{
    id: 1, field_name: "disease_activity", display: "In remission", sort_order: 0, codes: [],
    code: "in_remission", aliases: ["Remission"], retired: false, value_mappings: [], ...over,
  }];

  beforeEach(() => {
    vi.clearAllMocks();
    mockPatch.mockResolvedValue({ data: {} });
    mockDelete.mockResolvedValue({ data: {} });
  });

  it("shows details and history for a reader of the mapping pages, and refreshes after a save", async () => {
    mockGet.mockResolvedValue({ data: choices() });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    expect(await screen.findByText("in_remission")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Show the history of In remission/ })).toBeInTheDocument();
    mockGet.mockResolvedValue({ data: choices({ retired: true }) });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Retire In remission" }));
    expect(await screen.findByText("Retired")).toBeInTheDocument();
  });

  it("offers no history to someone who cannot read mappings", async () => {
    mockGet.mockResolvedValue({ data: choices({ value_mappings: undefined }) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    await screen.findByText("in_remission");
    expect(screen.queryByRole("button", { name: /Show the history/ })).not.toBeInTheDocument();
  });

  it("says why a delete was refused and points to retiring", async () => {
    mockGet.mockResolvedValue({ data: choices() });
    mockDelete.mockRejectedValue(refused({ detail: "This answer has an approved concept mapping. Only org admins and staff can delete it; retire it to stop offering it." }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Delete choice In remission/ }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("retire it instead"));
    expect(await screen.findByText(/retire it to stop offering it/)).toBeInTheDocument();
  });
});
