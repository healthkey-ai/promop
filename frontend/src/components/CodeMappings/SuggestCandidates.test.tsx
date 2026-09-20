import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SuggestCandidates, { type CandidateActivity } from "./SuggestCandidates";

const patch = vi.fn();
const get = vi.fn();
const post = vi.fn();
const remove = vi.fn();
vi.mock("@/api/axios", () => ({ default: { delete: (...args: unknown[]) => remove(...args), get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args), patch: (...args: unknown[]) => patch(...args) } }));
const winner = { concept_id: 1, concept_name: "Exact match", concept_code: "A", vocabulary_id: "SNOMED" };
const alternative = { concept_id: 2, concept_name: "Alternative match", concept_code: "B", vocabulary_id: "SNOMED", vector_distance: 0.123456 };
const source = { mapping_id: 7, source_code: "LOCAL", source_vocabulary_id: "Local" };
const searches: CandidateActivity[] = [
  { ...source, stage: "candidates", strategy: "umls", candidates: [winner] },
  { ...source, stage: "candidates", strategy: "lexical", candidates: [] },
  { ...source, stage: "candidates", strategy: "semantic", candidates: [alternative] },
];
const results: CandidateActivity[] = [
  ...searches,
  { ...source, stage: "ranked", suggested: winner, candidates: [winner, alternative] },
  { ...source, stage: "result", suggested: winner, updated: true },
];
beforeEach(() => {
  vi.resetAllMocks();
  post.mockResolvedValue({ data: {} });
  remove.mockResolvedValue({ data: {} });
  get.mockResolvedValue({ data: { mapping_id: 7, destination_concept_id: 1, status: "proposed" } });
});

describe("progressive suggestion candidates", () => {
  it("shows candidates as they arrive, then the winner and selectable alternatives", async () => {
    const onSaved = vi.fn();
    const { rerender } = render(<SuggestCandidates activity={searches.slice(0, 1)} finished={false} onSaved={onSaved} />);
    expect(screen.getByText(/Exact match/)).toBeInTheDocument();
    expect(screen.queryByText(/Alternative match/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Use .* for/ })).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={searches} finished={false} onSaved={onSaved} />);
    // Deduplicated table shows distance as 3-decimal
    expect(screen.getByText("0.123")).toBeInTheDocument();
    expect(screen.queryByText("Winner")).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results} finished onSaved={onSaved} />);
    expect(screen.getByText("Winner: Exact match")).toBeInTheDocument();
    patch.mockResolvedValue({ data: {} });
    fireEvent.click(screen.getByRole("button", { name: "Use Alternative match for LOCAL" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 2, status: "proposed" });
    expect(screen.getByText("Selected")).toBeInTheDocument();
  });

  it("keeps choices unavailable until writes finish and for dry runs", () => {
    const { rerender } = render(<SuggestCandidates activity={results} finished={false} />);
    expect(screen.queryByRole("button", { name: /Use .* for/ })).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results.map(event => ({ ...event, dry_run: true }))} finished />);
    expect(screen.queryByRole("button", { name: /Use .* for/ })).not.toBeInTheDocument();
  });

  it("reports failed saves and allows retry without claiming selection", async () => {
    patch.mockRejectedValue(new Error("failed"));
    render(<SuggestCandidates activity={results} finished />);
    fireEvent.click(screen.getByRole("button", { name: "Use Alternative match for LOCAL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
    expect(screen.queryByText("Selected")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use Alternative match for LOCAL" })).toBeEnabled();
  });

  it("deduplicates candidates found by multiple strategies", () => {
    const shared = { concept_id: 1, concept_name: "Shared", concept_code: "A", vocabulary_id: "SNOMED" };
    const activity: CandidateActivity[] = [
      { ...source, stage: "candidates", strategy: "umls", candidates: [shared] },
      { ...source, stage: "candidates", strategy: "lexical", candidates: [{ ...shared, lexical_score: 0.9 }] },
      { ...source, stage: "ranked", suggested: shared, candidates: [shared] },
      { ...source, stage: "result", suggested: shared, updated: true },
    ];
    render(<SuggestCandidates activity={activity} finished />);
    // Should appear once, not twice
    const rows = screen.getAllByText(/Shared/);
    // "Shared" appears in the table row and in "Winner: Shared"
    expect(rows.length).toBe(2);
    // Strategy badge shows both UMLS and Lexical
    expect(screen.getByText("UL")).toBeInTheDocument();
  });
});


describe("batch manual destination search", () => {
  it("offers manual search for declined rows and filters the run by low confidence", async () => {
    const activity: CandidateActivity[] = [
      ...results,
      { ...source, mapping_id: 8, source_code: "DECLINED", stage: "result", suggested: null, updated: false },
      { ...source, mapping_id: 9, source_code: "LOW", stage: "ranked", suggested: winner, alternatives: [{ concept_id: 1, confidence: 0.2 }] },
      { ...source, mapping_id: 9, source_code: "LOW", stage: "result", suggested: winner, updated: true },
    ];
    render(<SuggestCandidates activity={activity} finished />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Needs attention/ }));
    expect(screen.queryByRole("button", { name: "Search destination for LOCAL" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search destination for LOW" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search destination for DECLINED" }));
    expect(await screen.findByRole("combobox", { name: "Search destination concepts inline" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("saves a manual pick for a declined row and removes it from needs attention", async () => {
    const onSaved = vi.fn();
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("/search/")
      ? { results: [{ ...alternative, domain_id: "Condition", standard_concept: "S" }] }
      : { mapping_id: 7, destination_concept_id: null, status: "proposed" } }));
    patch.mockResolvedValue({ data: { mapping_id: 7, destination_concept_id: 2, status: "proposed" } });
    render(<SuggestCandidates activity={[{ ...source, stage: "result", suggested: null, updated: false }]} finished onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Needs attention/ }));
    fireEvent.click(screen.getByRole("button", { name: "Search destination for LOCAL" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Search destination concepts inline" }), { target: { value: "alternative" } });
    fireEvent.click(await screen.findByRole("option", { name: /Alternative match/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save choice" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 2, status: "proposed" });
    expect(await screen.findByText("No remaining mappings need attention in this run.")).toBeInTheDocument();
  });

  it("keeps manual search unavailable while a run is writing or when it was a dry run", () => {
    const { rerender } = render(<SuggestCandidates activity={results} finished={false} />);
    expect(screen.queryByRole("button", { name: /Search destination for/ })).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results.map(event => ({ ...event, dry_run: true }))} finished />);
    expect(screen.queryByRole("button", { name: /Search destination for/ })).not.toBeInTheDocument();
  });
});
