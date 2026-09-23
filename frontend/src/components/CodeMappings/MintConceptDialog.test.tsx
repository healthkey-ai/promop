import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import MintConceptDialog from "./MintConceptDialog";
const post = vi.hoisted(() => vi.fn());
const get = vi.hoisted(() => vi.fn());
vi.mock("@/api/axios", () => ({ default: { post, get } }));
const candidate = { concept_id: 123, concept_name: "Existing protein", concept_code: "123-4", vocabulary_id: "LOINC", domain_id: "Measurement", concept_class_id: "Lab Test", standard_concept: "S" };
const select = vi.fn();
function open() {
  render(<MintConceptDialog vocabularies={[{ vocabulary_id: "HK-Labs", vocabulary_name: "Labs" }, { vocabulary_id: "LOINC", vocabulary_name: "LOINC" }]}
    domains={[{ domain_id: "Measurement", label: "Measurement" }]} initialDomain="Measurement" initialName="Protein" sourceCode="abc" sourceVocabulary="LOINC" onSelect={select} onClose={() => {}} />);
  fireEvent.change(screen.getByLabelText("Custom vocabulary group"), { target: { value: "HK-Labs" } });
  fireEvent.change(screen.getByLabelText("Concept code"), { target: { value: "my-protein" } });
}
describe("mint destination review", () => {
  beforeEach(() => { vi.clearAllMocks(); post.mockResolvedValue({ data: { candidates: [candidate], review_token: "reviewed" } }); });
  it("offers only existing custom groups and allows choosing a candidate without minting", async () => {
    open();
    expect(screen.queryByRole("option", { name: "LOINC — LOINC" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Check existing destinations"));
    fireEvent.click(await screen.findByRole("button", { name: /Existing protein/ }));
    expect(select).toHaveBeenCalledWith(candidate);
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][1].action).toBe("review");
  });
  it("requires confirmation after review and resets it when details change", async () => {
    open(); fireEvent.click(screen.getByText("Check existing destinations"));
    expect(await screen.findByText("Mint concept")).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByText("Mint concept")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Concept name"), { target: { value: "Changed protein" } });
    expect(screen.queryByText("Mint concept")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Check existing destinations"));
    expect(await screen.findByText("Mint concept")).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    post.mockResolvedValue({ data: candidate });
    fireEvent.click(screen.getByText("Mint concept"));
    await waitFor(() => expect(select).toHaveBeenCalledWith(candidate));
    expect(post.mock.calls[2][1]).toMatchObject({ action: "mint", none_match: true, review_token: "reviewed" });
  });
  it("does not allow minting after a failed candidate check", async () => {
    post.mockRejectedValue(new Error("Unavailable"));
    open(); fireEvent.click(screen.getByText("Check existing destinations"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to complete");
    expect(screen.queryByText("Mint concept")).not.toBeInTheDocument();
  });
});

describe("mint destination parent concept", () => {
  beforeEach(() => { vi.clearAllMocks(); post.mockResolvedValue({ data: { candidates: [], review_token: "reviewed" } }); });
  it("renders the parent concept search field", () => {
    open();
    expect(screen.getByPlaceholderText("Search for a broader concept...")).toBeInTheDocument();
  });
  it("sends parent_concept_id as null when no parent is selected", async () => {
    open();
    fireEvent.click(screen.getByText("Check existing destinations"));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(post.mock.calls[0][1].parent_concept_id).toBeNull();
  });
  it("searches and selects a parent concept, sends its id in the payload", async () => {
    const parentConcept = { concept_id: 42, concept_name: "Neoplasm", concept_code: "NEO", vocabulary_id: "SNOMED", domain_id: "Condition", concept_class_id: "Clinical Finding", standard_concept: "S" };
    get.mockResolvedValue({ data: { results: [parentConcept] } });
    open();
    fireEvent.change(screen.getByPlaceholderText("Search for a broader concept..."), { target: { value: "Neoplasm" } });
    await waitFor(() => expect(get).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("Neoplasm · SNOMED:NEO · ID 42"));
    expect(screen.getByText(/SNOMED:NEO — Neoplasm \(ID 42\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Check existing destinations"));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(post.mock.calls[0][1].parent_concept_id).toBe(42);
  });
  it("clears the selected parent when clear button is clicked", async () => {
    const parentConcept = { concept_id: 42, concept_name: "Neoplasm", concept_code: "NEO", vocabulary_id: "SNOMED", domain_id: "Condition", concept_class_id: "Clinical Finding", standard_concept: "S" };
    get.mockResolvedValue({ data: { results: [parentConcept] } });
    open();
    fireEvent.change(screen.getByPlaceholderText("Search for a broader concept..."), { target: { value: "Neoplasm" } });
    await waitFor(() => expect(get).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("Neoplasm · SNOMED:NEO · ID 42"));
    fireEvent.click(screen.getByLabelText("Clear parent concept"));
    expect(screen.getByPlaceholderText("Search for a broader concept...")).toBeInTheDocument();
    expect(screen.queryByText(/SNOMED:NEO — Neoplasm/)).not.toBeInTheDocument();
  });
});
