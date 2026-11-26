import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
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
    expect(refusalText(refused({ aliases: { "0": ["Too long."] } }), "x")).toBe("Too long.");
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
    fireEvent.click(screen.getByRole("button", { name: /Edit details of In remission/ }));
    expect(screen.getByText(/keeps the old one as another spelling/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "  In   full remission " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { display: "In full remission" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("sends the spellings one per line, without blanks or repeats", async () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    fireEvent.change(screen.getByLabelText("Other spellings"), {
      target: { value: "Remission\n\n  CR \nCR\nComplete  response" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-choices/4/", { aliases: ["Remission", "CR", "Complete response"] }));
  });

  it("does not write back a spelling list the curator never edited", async () => {
    const { rerender } = panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    // Another curator adds a spelling while this form is open.
    rerender(<AnswerDetailsPanel choiceId={4} label="In remission" code="in_remission"
      aliases={["Remission", "CR"]} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "In full remission" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { display: "In full remission" }));
  });

  it("names its toggle for what it does, and cannot retire under an open form", () => {
    panel();
    const edit = screen.getByRole("button", { name: /Edit details of/ });
    expect(edit).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(edit);
    const cancel = screen.getByRole("button", { name: /Cancel editing In remission/ });
    expect(cancel).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Retire In remission" })).toBeDisabled();
  });

  describe("scope", () => {
    const options = [{ value: "MM", label: "Multiple Myeloma" }, { value: "FL", label: "Follicular Lymphoma" }];

    it("says who is offered the answer, and its context", () => {
      panel({ diseases: ["FL"], contextKey: "stage_ann_arbor" });
      expect(screen.getByText("Offered to: FL")).toBeInTheDocument();
      expect(screen.getByText("stage_ann_arbor")).toBeInTheDocument();
      expect(screen.queryByText("Offered to every patient")).not.toBeInTheDocument();
    });

    it("names the answer on its scope control", () => {
      panel({ diseaseOptions: options });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      expect(screen.getByText(/which patients are offered "In remission"/)).toBeInTheDocument();
    });

    it("offers no scope control without the categories", () => {
      panel();
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
      expect(screen.queryByText(/Every patient$/)).not.toBeInTheDocument();
    });

    it("narrows an answer to the ticked categories, and only then", async () => {
      panel({ diseaseOptions: options });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      fireEvent.click(screen.getByLabelText("Only some Disease Categories"));
      fireEvent.click(screen.getByLabelText(/^MM/));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { diseases: ["MM"] }));
    });

    it("will not save a narrowing that names no category", () => {
      panel({ diseaseOptions: options });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      fireEvent.click(screen.getByLabelText("Only some Disease Categories"));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getByRole("alert")).toHaveTextContent("Tick at least one Disease Category");
      expect(mockPatch).not.toHaveBeenCalled();
    });

    it("offers it to every patient again by sending no categories", async () => {
      panel({ diseaseOptions: options, diseases: ["FL", "MM"] });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      fireEvent.click(screen.getByLabelText("Every patient"));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { diseases: [] }));
    });

    it("sends no scope when it was not changed, and keeps a category the list lacks", async () => {
      panel({ diseaseOptions: options, diseases: ["GONE"] });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      expect(screen.getByLabelText(/^GONE/)).toBeChecked();
      fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Remission" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-choices/4/", { display: "Remission" }));
    });

    it("shows the server's refusal of a category", async () => {
      mockPatch.mockRejectedValue(refused({ diseases: ["Unknown Disease Category 'XX'."] }));
      panel({ diseaseOptions: options });
      fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
      fireEvent.click(screen.getByLabelText("Only some Disease Categories"));
      fireEvent.click(screen.getByLabelText(/^MM/));
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
      expect(await screen.findByText(/Unknown Disease Category/)).toBeInTheDocument();
    });
  });

  it("saves nothing when nothing changed", () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(mockPatch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Label")).not.toBeInTheDocument();
  });

  it("refuses an empty label without asking the server", () => {
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it("shows the server's refusal and stays open", async () => {
    mockPatch.mockRejectedValue(refused({ display: ["'Complete' is already a choice of disease_activity in this context."] }));
    panel();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
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

  it("reads again when the answer's decisions change while it is open", async () => {
    const { rerender } = render(<AnswerHistory choiceId={4} choiceLabel="In remission" signature="7:proposed" />);
    fireEvent.click(screen.getByRole("button", { name: /Show the history/ }));
    await screen.findByRole("region");
    const before = mockGet.mock.calls.length;
    rerender(<AnswerHistory choiceId={4} choiceLabel="In remission" signature="7:approved" />);
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThan(before));
    // Closed, a change costs nothing.
    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    const closed = mockGet.mock.calls.length;
    rerender(<AnswerHistory choiceId={4} choiceLabel="In remission" signature="7:rejected" />);
    expect(mockGet.mock.calls.length).toBe(closed);
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
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove />);
    expect(await screen.findByText("in_remission")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Show the history of In remission/ })).toBeInTheDocument();
    mockGet.mockResolvedValue({ data: choices({ retired: true }) });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Retire In remission" }));
    expect(await screen.findByText("Retired")).toBeInTheDocument();
  });

  it("shows what a save stores, read only, after the label has been renamed", async () => {
    mockGet.mockResolvedValue({ data: choices({
      display: "In remission", value: "Finished treatment and in remission",
      stored_value: "Finished treatment and in remission",
    }) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove />);
    const saved = await screen.findByText("Saved as");
    expect(saved).toHaveTextContent("Saved as Finished treatment and in remission");
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    expect(screen.queryByLabelText(/stored value|saved as/i)).not.toBeInTheDocument();
  });

  it("keeps the editor open when Escape is pressed in the spellings box", async () => {
    mockGet.mockResolvedValue({ data: choices() });
    const onClose = vi.fn();
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={onClose} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: /Edit details of/ }));
    fireEvent.keyDown(screen.getByLabelText("Other spellings"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("passes the categories on, so an answer's scope can be edited from the field's page", async () => {
    mockGet.mockResolvedValue({ data: choices({ diseases: ["MM"], context_key: "" }) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove
      diseaseOptions={[{ value: "MM", label: "Multiple Myeloma" }]} />);
    expect(await screen.findByText("Offered to: MM")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Edit details of/ }));
    expect(screen.getByLabelText(/^MM/)).toBeChecked();
  });

  it("shows a mapping curator who is not an approver what is offered, and no way to change it", async () => {
    mockGet.mockResolvedValue({ data: choices({
      diseases: ["MM"], codes: [{ id: 5, code: "1", vocabulary_id: "LOCAL", display: "", is_primary: true }],
    }) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}}
      diseaseOptions={[{ value: "MM", label: "Multiple Myeloma" }]} />);
    expect(await screen.findByText("Offered to: MM")).toBeInTheDocument();
    expect(screen.getByText("in_remission")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit details of/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retire In remission/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove code/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add code to/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete choice/ })).not.toBeInTheDocument();
    // What a curator does: reorder, add an answer, propose a concept.
    expect(screen.getByPlaceholderText(/New choice display name/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Map In remission to a concept/ })).toBeInTheDocument();
  });

  it("offers no history to someone who cannot read mappings", async () => {
    mockGet.mockResolvedValue({ data: choices({ value_mappings: undefined }) });
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    await screen.findByText("in_remission");
    expect(screen.queryByRole("button", { name: /Show the history/ })).not.toBeInTheDocument();
  });

  it("offers deleting an answer only to an approver", async () => {
    mockGet.mockResolvedValue({ data: choices() });
    const { unmount } = render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} />);
    await screen.findByText("in_remission");
    expect(screen.queryByRole("button", { name: /Delete choice/ })).not.toBeInTheDocument();
    unmount();
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove />);
    expect(await screen.findByRole("button", { name: /Delete choice In remission/ })).toBeInTheDocument();
  });

  it("says why a delete was refused and points to retiring", async () => {
    mockGet.mockResolvedValue({ data: choices() });
    mockDelete.mockRejectedValue(refused({ detail: "This answer has an approved concept mapping. Only org admins and staff can delete it; retire it to stop offering it." }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<FieldChoiceEditor fieldName="disease_activity" onClose={() => {}} canApprove />);
    fireEvent.click(await screen.findByRole("button", { name: /Delete choice In remission/ }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("retire it instead"));
    expect(await screen.findByText(/retire it to stop offering it/)).toBeInTheDocument();
  });
});

describe("lookup options in the editor", () => {
  const lookup = (over: Record<string, unknown> = {}) => ({
    source: {
      kind: "vocabulary", name: "her2-status", label: "Her2Status", enforced: false,
      source_name: "ASCO/CAP", source_url: "https://example.org/her2", ...over,
    },
    options: [{ code: "pos", title: "Positive" }, { code: "neg", title: "Negative" }],
  });
  const serve = (lookupBody: unknown, choiceList: unknown[] = []) =>
    mockGet.mockImplementation(async (url: string) =>
      ({ data: url.startsWith("/v1/field-choices/lookup/") ? lookupBody : choiceList }));

  const choices = () => [{
    id: 1, field_name: "her2_status", display: "Positive", sort_order: 0, codes: [], value_mappings: [], retired: false,
  }];

  beforeEach(() => vi.clearAllMocks());

  it("lists a field's lookup read only, without copying it into answers", async () => {
    serve(lookup());
    render(<FieldChoiceEditor fieldName="her2_status" onClose={() => {}} canApprove />);
    const section = await screen.findByRole("region", { name: "Lookup options" });
    expect(section).toHaveTextContent("Her2Status");
    expect(section).toHaveTextContent("2 entries");
    expect(section).toHaveTextContent("shown here read only");
    expect(mockGet).toHaveBeenCalledWith("/v1/field-choices/lookup/", { params: { field_name: "her2_status" } });
    expect(screen.queryByText("Positive")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show entries" }));
    expect(screen.getByText("Positive")).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: /Delete|Edit|Add/ })).not.toBeInTheDocument();
  });

  it("says an enforced lookup wins over answers added here", async () => {
    serve(lookup({ enforced: true }));
    render(<FieldChoiceEditor fieldName="her2_status" onClose={() => {}} />);
    expect(await screen.findByText(/only these entries are offered and answers added below are not/)).toBeInTheDocument();
  });

  it("says when answers added here replace the tab's lookup", async () => {
    serve(lookup(), choices());
    render(<FieldChoiceEditor fieldName="her2_status" onClose={() => {}} />);
    expect(await screen.findByText(/those are offered instead of these/)).toBeInTheDocument();
    expect(screen.getByText(/This field has answers below/)).toBeInTheDocument();
  });

  it("shows nothing for a field with no lookup, and the server's reason when it cannot load", async () => {
    serve({ source: null, options: [] });
    const { unmount } = render(<FieldChoiceEditor fieldName="hemoglobin_g_dl" onClose={() => {}} canApprove />);
    await screen.findByText("No choices defined yet.");
    expect(screen.queryByRole("region", { name: "Lookup options" })).not.toBeInTheDocument();
    unmount();
    mockGet.mockImplementation(async (url: string) => {
      if (url.startsWith("/v1/field-choices/lookup/")) throw refused({ detail: "Mapping access required." });
      return { data: [] };
    });
    render(<FieldChoiceEditor fieldName="her2_status" onClose={() => {}} canApprove />);
    expect(await screen.findByText("Mapping access required.")).toBeInTheDocument();
  });
});
