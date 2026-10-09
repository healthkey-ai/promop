import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SuggestCandidates, { type CandidateActivity } from "./SuggestCandidates";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: api }));
const winner = { concept_id: 1, concept_name: "Winner concept", concept_code: "A", vocabulary_id: "SNOMED" };
const alternative = { ...winner, concept_id: 2, concept_name: "Other concept", concept_code: "B" };
const mappings = new Map<number, { destination_concept_id: number; status: string }>();
const idFrom = (url: string) => Number(url.split("/")[3]);
function events(id: number, confidence?: number, result = {}): CandidateActivity[] {
  mappings.set(id, { destination_concept_id: 1, status: "proposed" });
  const source = { mapping_id: id, source_code: `SOURCE-${id}` };
  return [
    { ...source, stage: "ranked", suggested: winner, candidates: [winner, alternative],
      alternatives: confidence === undefined ? [] : [{ concept_id: 1, confidence, ranker: "jev" }] },
    { ...source, stage: "result", suggested: winner, updated: true, ...result },
  ];
}
const bulk = () => screen.getByRole("button", { name: /Approve Matching Winners/ });
const threshold = () => screen.getByRole("spinbutton", { name: /Minimum winner confidence/ });
beforeEach(() => {
  vi.resetAllMocks();
  mappings.clear();
  api.post.mockResolvedValue({ data: {} });
  api.delete.mockResolvedValue({ data: {} });
  api.get.mockImplementation((url: string) => Promise.resolve({ data: mappings.get(idFrom(url)) }));
  api.patch.mockImplementation((url: string, value: { destination_concept_id: number; status: string }) => {
    mappings.set(idFrom(url), value);
    return Promise.resolve({ data: value });
  });
});

describe("explicit matching Winner approval", () => {
  it("defaults to exactly 100%, excludes unknown confidence, and only writes on a click", async () => {
    render(<SuggestCandidates activity={[...events(7, 1), ...events(8, 0.99999), ...events(9)]} finished={false} canApprove />);
    expect(threshold()).toHaveValue(100);
    expect(bulk()).toHaveTextContent("(1)");
    expect(screen.getByText("99.99% J")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
    fireEvent.click(bulk());
    await screen.findByText("Approved 1 of 1 matching Winners.");
    expect(api.patch).toHaveBeenCalledExactlyOnceWith("/v1/code-mappings/7/", { destination_concept_id: 1, status: "approved" });
    fireEvent.change(threshold(), { target: { value: "99" } });
    expect(api.patch).toHaveBeenCalledTimes(1);
    expect(bulk()).toHaveTextContent("(1)");
    fireEvent.click(bulk());
    await waitFor(() => expect(mappings.get(8)?.status).toBe("approved"));
    expect(mappings.get(9)?.status).toBe("proposed");
  });

  it("uses only the Winner's ranker confidence and requires a saved non-dry result", async () => {
    const dual = events(7, 0.5);
    dual[0].alternatives!.push({ concept_id: 1, confidence: 1, ranker: "anthropic" });
    const wrong = events(8, 0.2);
    wrong[0].alternatives!.push({ concept_id: 2, confidence: 1 });
    render(<SuggestCandidates activity={[...dual, ...wrong, ...events(9, 1, { updated: false }),
      ...events(10, 1, { dry_run: true }), ...events(11, 1).slice(0, 1), ...events(12, 1.2)]} finished={false} canApprove />);
    expect(bulk()).toHaveTextContent("(1)");
    fireEvent.click(bulk());
    await screen.findByText("Approved 1 of 1 matching Winners.");
    expect(api.patch).toHaveBeenCalledTimes(1);
  });

  it.each(["", "-1", "101"])("rejects invalid threshold %s without blocking individual approval", async value => {
    render(<SuggestCandidates activity={events(7, 0.2)} finished={false} canApprove />);
    fireEvent.change(threshold(), { target: { value } });
    expect(bulk()).toBeDisabled();
    expect(threshold()).toHaveAttribute("aria-invalid", "true");
    fireEvent.click(screen.getByRole("button", { name: "Approve Winner concept for SOURCE-7" }));
    await screen.findByText("Saved: Winner concept — Approved");
    expect(mappings.get(7)?.status).toBe("approved");
  });

  it("allows Use then Approve mid-stream and excludes that manual choice from bulk approval", async () => {
    render(<SuggestCandidates activity={events(7, 1)} finished={false} canApprove />);
    fireEvent.click(screen.getByRole("button", { name: "Use Other concept for SOURCE-7" }));
    await screen.findByText("Saved: Other concept — Proposed");
    expect(bulk()).toHaveTextContent("(0)");
    fireEvent.click(screen.getByRole("button", { name: "Approve Other concept for SOURCE-7" }));
    await screen.findByText("Saved: Other concept — Approved");
    expect(mappings.get(7)).toEqual({ destination_concept_id: 2, status: "approved" });
    expect(screen.queryByRole("button", { name: /Approve .* for SOURCE-7/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Search destination/ })).not.toBeInTheDocument();
  });

  it("rechecks destination and proposed status under a lock, continuing after failures", async () => {
    const activity = [7, 8, 9, 10, 11].flatMap(id => events(id, 1));
    mappings.set(7, { destination_concept_id: 2, status: "proposed" });
    mappings.set(8, { destination_concept_id: 1, status: "approved" });
    mappings.set(9, { destination_concept_id: 1, status: "rejected" });
    api.post.mockImplementation((url: string) => idFrom(url) === 10
      ? Promise.reject({ response: { status: 423, data: { locked_by: "another curator" } } })
      : Promise.resolve({ data: {} }));
    render(<SuggestCandidates activity={activity} finished canApprove />);
    fireEvent.click(bulk());
    await screen.findByText("Approved 1 of 5 matching Winners.");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not approve 4 mapping(s)");
    expect(api.patch).toHaveBeenCalledExactlyOnceWith("/v1/code-mappings/11/", { destination_concept_id: 1, status: "approved" });
    expect(api.delete).toHaveBeenCalledTimes(4);
    expect(api.delete).not.toHaveBeenCalledWith("/v1/code-mappings/10/lock/");
  });

  it("captures matches at click time, prevents duplicate clicks, and retries only failures", async () => {
    const activity = [...events(7, 1), ...events(8, 1)];
    let finish!: (value: { data: object }) => void;
    api.patch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockRejectedValueOnce(new Error("Temporary failure"));
    const { rerender } = render(<SuggestCandidates activity={activity} finished={false} canApprove />);
    fireEvent.click(bulk());
    await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));
    expect(bulk()).toBeDisabled();
    fireEvent.click(bulk());
    rerender(<SuggestCandidates activity={[...activity, ...events(9, 1)]} finished={false} canApprove />);
    await act(async () => finish({ data: { status: "approved" } }));
    await screen.findByText("Approved 1 of 2 matching Winners.");
    expect(api.patch).toHaveBeenCalledTimes(2);
    expect(bulk()).toHaveTextContent("(2)");
    fireEvent.click(bulk());
    await screen.findByText("Approved 2 of 2 matching Winners.");
    expect(api.patch.mock.calls.map(call => call[0])).toEqual([
      "/v1/code-mappings/7/", "/v1/code-mappings/8/", "/v1/code-mappings/8/", "/v1/code-mappings/9/",
    ]);
  });

  it("honors the displayed Needs attention filter", async () => {
    render(<SuggestCandidates activity={[...events(7, 1), ...events(8, 0.3)]} finished canApprove />);
    fireEvent.change(threshold(), { target: { value: "0" } });
    expect(bulk()).toHaveTextContent("(2)");
    fireEvent.click(screen.getByRole("checkbox", { name: /Needs attention/ }));
    expect(bulk()).toHaveTextContent("(1)");
    fireEvent.click(bulk());
    await screen.findByText("Approved 1 of 1 matching Winners.");
    expect(api.patch).toHaveBeenCalledExactlyOnceWith("/v1/code-mappings/8/", { destination_concept_id: 1, status: "approved" });
  });

  it("hides bulk approval and its threshold without approval permission", () => {
    render(<SuggestCandidates activity={events(7, 1)} finished />);
    expect(screen.queryByRole("button", { name: /Approve Matching Winners/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it.each([false, true])("preserves search approval state (approved=%s) for an off-list destination", async approved => {
    const activity = events(7, 1);
    const offList = { ...alternative, concept_id: 3, concept_name: "Search choice", domain_id: "Condition", standard_concept: "S" };
    api.get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("/search/")
      ? { results: [offList] } : mappings.get(idFrom(url)) }));
    render(<SuggestCandidates activity={activity} finished={false} canApprove />);
    fireEvent.click(screen.getByRole("button", { name: "Search destination for SOURCE-7" }));
    expect(bulk()).toBeDisabled();
    fireEvent.change(await screen.findByRole("combobox", { name: "Search destination concepts inline" }), { target: { value: "search choice" } });
    fireEvent.click(await screen.findByRole("option", { name: /Search choice/ }));
    fireEvent.click(screen.getByRole("button", { name: approved ? /Save.*approve/i : "Save choice" }));
    await screen.findByText(`Saved: Search choice — ${approved ? "Approved" : "Proposed"}`);
    expect(bulk()).toHaveTextContent("(0)");
    if (!approved) {
      fireEvent.click(screen.getByRole("button", { name: "Approve Search choice for SOURCE-7" }));
      await screen.findByText("Saved: Search choice — Approved");
    }
    expect(mappings.get(7)).toEqual({ destination_concept_id: 3, status: "approved" });
    expect(screen.queryByRole("button", { name: /Approve .* for SOURCE-7/ })).not.toBeInTheDocument();
  });
});
