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

  it("keeps choices unavailable until the mapping result is saved and for dry runs", () => {
    const { rerender } = render(<SuggestCandidates activity={results.filter(event => event.stage !== "result")} finished={false} />);
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

  it("keeps manual search unavailable before the mapping result is saved or when it was a dry run", () => {
    const { rerender } = render(<SuggestCandidates activity={results.filter(event => event.stage !== "result")} finished={false} />);
    expect(screen.queryByRole("button", { name: /Search destination for/ })).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results.map(event => ({ ...event, dry_run: true }))} finished />);
    expect(screen.queryByRole("button", { name: /Search destination for/ })).not.toBeInTheDocument();
  });
});


describe("approve from suggest results", () => {
  it("approves the winner with a single click", async () => {
    const onSaved = vi.fn();
    patch.mockResolvedValue({ data: {} });
    render(<SuggestCandidates activity={results} finished onSaved={onSaved} canApprove />);
    fireEvent.click(screen.getByRole("button", { name: "Approve Exact match for LOCAL" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 1, status: "approved" });
    expect(screen.getByText("Approved")).toBeInTheDocument();
  });

  it("approves a non-winner alternative directly (sets destination and approves)", async () => {
    const onSaved = vi.fn();
    patch.mockResolvedValue({ data: {} });
    render(<SuggestCandidates activity={results} finished onSaved={onSaved} canApprove />);
    // The alternative has both Use and Approve buttons
    const approveButtons = screen.getAllByRole("button", { name: /Approve .* for LOCAL/ });
    // Find the one for the alternative
    const altApprove = approveButtons.find(b => b.getAttribute("aria-label")?.includes("Alternative match"));
    expect(altApprove).toBeDefined();
    fireEvent.click(altApprove!);
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 2, status: "approved" });
    expect(screen.getByText("Approved")).toBeInTheDocument();
  });

  it("does not render Approve buttons without canApprove", () => {
    render(<SuggestCandidates activity={results} finished />);
    expect(screen.queryByRole("button", { name: /Approve .* for/ })).not.toBeInTheDocument();
  });

  it("does not render Approve buttons for dry runs or unsaved results", () => {
    const { rerender } = render(<SuggestCandidates activity={results.filter(event => event.stage !== "result")} finished={false} canApprove />);
    expect(screen.queryByRole("button", { name: /Approve .* for/ })).not.toBeInTheDocument();
    rerender(<SuggestCandidates activity={results.map(event => ({ ...event, dry_run: true }))} finished canApprove />);
    expect(screen.queryByRole("button", { name: /Approve .* for/ })).not.toBeInTheDocument();
  });

  it("reports approve failures and allows retry", async () => {
    patch.mockRejectedValue(new Error("forbidden"));
    render(<SuggestCandidates activity={results} finished canApprove />);
    fireEvent.click(screen.getByRole("button", { name: "Approve Exact match for LOCAL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not approve");
    expect(screen.queryByText("Approved")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve Exact match for LOCAL" })).toBeEnabled();
  });
});


describe("standard concept badge", () => {
  it("shows Non-std badge when a candidate is not standard", () => {
    const nonStdCandidate = { ...alternative, standard_concept: null };
    const stdCandidate = { ...winner, standard_concept: "S" };
    const activity: CandidateActivity[] = [
      { ...source, stage: "candidates", strategy: "umls", candidates: [stdCandidate] },
      { ...source, stage: "candidates", strategy: "semantic", candidates: [nonStdCandidate] },
      { ...source, stage: "ranked", suggested: stdCandidate, candidates: [stdCandidate, nonStdCandidate] },
      { ...source, stage: "result", suggested: stdCandidate, updated: true },
    ];
    render(<SuggestCandidates activity={activity} finished />);
    expect(screen.getByText("Non-std")).toBeInTheDocument();
  });

  it("shows Classification badge for standard_concept=C", () => {
    const classCandidate = { ...alternative, standard_concept: "C" };
    const stdCandidate = { ...winner, standard_concept: "S" };
    const activity: CandidateActivity[] = [
      { ...source, stage: "candidates", strategy: "umls", candidates: [stdCandidate] },
      { ...source, stage: "candidates", strategy: "semantic", candidates: [classCandidate] },
      { ...source, stage: "ranked", suggested: stdCandidate, candidates: [stdCandidate, classCandidate] },
      { ...source, stage: "result", suggested: stdCandidate, updated: true },
    ];
    render(<SuggestCandidates activity={activity} finished />);
    expect(screen.getByText("Classification")).toBeInTheDocument();
  });

  it("hides badges when all candidates are standard", () => {
    const stdWinner = { ...winner, standard_concept: "S" };
    const stdAlt = { ...alternative, standard_concept: "S" };
    const activity: CandidateActivity[] = [
      { ...source, stage: "candidates", strategy: "umls", candidates: [stdWinner] },
      { ...source, stage: "candidates", strategy: "semantic", candidates: [stdAlt] },
      { ...source, stage: "ranked", suggested: stdWinner, candidates: [stdWinner, stdAlt] },
      { ...source, stage: "result", suggested: stdWinner, updated: true },
    ];
    render(<SuggestCandidates activity={activity} finished />);
    expect(screen.queryByText("Non-std")).not.toBeInTheDocument();
    expect(screen.queryByText("Classification")).not.toBeInTheDocument();
  });
});


const hkVocabs = [{ vocabulary_id: "HK-Labs", vocabulary_name: "Labs" }];
const domains = [{ domain_id: "Measurement", label: "Measurement" }];

describe("mint new concept from suggest results", () => {
  it("shows Mint button when HK vocabularies and domains are available", () => {
    render(<SuggestCandidates activity={results} finished vocabularies={hkVocabs} domains={domains} />);
    expect(screen.getByRole("button", { name: "Mint new concept for LOCAL" })).toBeInTheDocument();
  });

  it("hides Mint button when no HK vocabularies", () => {
    render(<SuggestCandidates activity={results} finished vocabularies={[{ vocabulary_id: "SNOMED", vocabulary_name: "SNOMED" }]} domains={domains} />);
    expect(screen.queryByRole("button", { name: /Mint new concept/ })).not.toBeInTheDocument();
  });

  it("hides Mint button when no domains", () => {
    render(<SuggestCandidates activity={results} finished vocabularies={hkVocabs} domains={[]} />);
    expect(screen.queryByRole("button", { name: /Mint new concept/ })).not.toBeInTheDocument();
  });

  it("opens MintConceptDialog when clicked", () => {
    render(<SuggestCandidates activity={results} finished vocabularies={hkVocabs} domains={domains} />);
    fireEvent.click(screen.getByRole("button", { name: "Mint new concept for LOCAL" }));
    expect(screen.getByRole("dialog", { name: "Mint new concept" })).toBeInTheDocument();
  });

  it("pre-fills vocabulary, domain, and concept code from omop_table and source_code", () => {
    const activityWithTable: CandidateActivity[] = results.map(e => ({ ...e, omop_table: "measurement" }));
    render(<SuggestCandidates activity={activityWithTable} finished vocabularies={hkVocabs} domains={domains} />);
    fireEvent.click(screen.getByRole("button", { name: "Mint new concept for LOCAL" }));
    const dialog = screen.getByRole("dialog", { name: "Mint new concept" });
    expect(dialog).toBeInTheDocument();
    // Vocabulary pre-filled to HK-Labs
    expect(screen.getByLabelText(/Custom vocabulary group/)).toHaveValue("HK-Labs");
    // Domain pre-filled to Measurement
    expect(screen.getByLabelText(/Concept domain/)).toHaveValue("Measurement");
    // Concept code pre-filled to source_code
    expect(screen.getByLabelText(/Concept code/)).toHaveValue("LOCAL");
  });

  it("saves the minted concept as the mapping destination", async () => {
    const onSaved = vi.fn();
    post.mockResolvedValue({ data: { candidates: [], review_token: "tok" } });
    patch.mockResolvedValue({ data: {} });
    render(<SuggestCandidates activity={results} finished vocabularies={hkVocabs} domains={domains} onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: "Mint new concept for LOCAL" }));
    // Fill out the mint form
    fireEvent.change(screen.getByLabelText(/Custom vocabulary group/), { target: { value: "HK-Labs" } });
    fireEvent.change(screen.getByLabelText(/Concept code/), { target: { value: "TEST-001" } });
    fireEvent.change(screen.getByLabelText(/Concept domain/), { target: { value: "Measurement" } });
    // Submit review step
    fireEvent.click(screen.getByRole("button", { name: /Check existing destinations/ }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    // Confirm none match and mint
    fireEvent.click(screen.getByLabelText(/None of these match/));
    post.mockResolvedValue({ data: { concept_id: 999, concept_name: "LOCAL", concept_code: "TEST-001", vocabulary_id: "HK-Labs", domain_id: "Measurement", concept_class_id: "Observation", standard_concept: null } });
    fireEvent.click(screen.getByRole("button", { name: /Mint concept/ }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledWith("/v1/code-mappings/7/", { destination_concept_id: 999, status: "proposed" });
    expect(screen.getByText(/Saved: LOCAL/)).toBeInTheDocument();
  });
});
