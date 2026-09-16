import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import MintConceptDialog from "./MintConceptDialog";
const post = vi.hoisted(() => vi.fn());
vi.mock("@/api/axios", () => ({ default: { post } }));
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
