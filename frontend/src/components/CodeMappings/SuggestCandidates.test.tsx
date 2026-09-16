import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SuggestCandidates, { type CandidateActivity } from "./SuggestCandidates";

const patch = vi.fn();
vi.mock("@/api/axios", () => ({ default: { patch: (...args: unknown[]) => patch(...args) } }));
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
beforeEach(() => { patch.mockReset(); });

describe("progressive suggestion candidates", () => {
  it("shows stages as they arrive, then the winner and selectable alternatives", async () => {
    const onSaved = vi.fn();
    const { rerender } = render(<SuggestCandidates activity={searches.slice(0, 1)} finished={false} onSaved={onSaved} />);
    expect(screen.getByText(/Exact match/)).toBeInTheDocument();
    expect(screen.queryByText(/Alternative match/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={searches} finished={false} onSaved={onSaved} />);
    expect(screen.getByText("No matches")).toBeInTheDocument();
    expect(screen.getByText("Distance 0.1235")).toBeInTheDocument();
    expect(screen.queryByText("Winner")).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results} finished onSaved={onSaved} />);
    expect(screen.getByText("Winner: Exact match")).toBeInTheDocument();
    patch.mockResolvedValue({ data: {} });
    fireEvent.click(screen.getByRole("button", { name: "Use Alternative match for LOCAL" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 2, status: "proposed" });
    expect(screen.getByText("Selected alternative · awaiting review")).toBeInTheDocument();
  });

  it("keeps choices unavailable until writes finish and for dry runs", () => {
    const { rerender } = render(<SuggestCandidates activity={results} finished={false} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results.map(event => ({ ...event, dry_run: true }))} finished />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("reports failed saves and allows retry without claiming selection", async () => {
    patch.mockRejectedValue(new Error("failed"));
    render(<SuggestCandidates activity={results} finished />);
    fireEvent.click(screen.getByRole("button", { name: "Use Alternative match for LOCAL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
    expect(screen.queryByText("Selected alternative · awaiting review")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Use Alternative match for LOCAL" })).toBeEnabled();
  });
});
