import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConceptAssignDialog } from "./ConceptAssignDialog";

const { mockPatch, mockPost } = vi.hoisted(() => ({
  mockPatch: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("@/api/axios", () => ({
  default: { get: vi.fn(), patch: mockPatch, post: mockPost },
}));

const editProps = {
  fieldName: "genetic_mutations.allelic_frequency",
  fieldType: "number",
  existingMappingId: 42,
  initialConceptId: 123,
  initialConceptCode: "81258-6",
  initialVocabularyId: "LOINC",
  initialStatus: "approved" as const,
  onClose: vi.fn(),
  onSaved: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  mockPatch.mockResolvedValue({ data: {} });
  mockPost.mockResolvedValue({ data: {} });
});

describe("Source Table", () => {
  it.each([
    ["measurement", "Measurement"],
    ["observation", "Observation"],
    ["condition_occurrence", "ConditionOccurrence"],
    ["drug_exposure", "DrugExposure"],
    ["note_nlp", "NoteNlp"],
    ["person", "Person"],
    ["Measurement", "Measurement"],
    ["VisitOccurrence", "VisitOccurrence"],
    [" MEASUREMENT ", "Measurement"],
    ["legacy_table", "legacy_table"],
  ])("displays %s as %s and preserves the stored value on save", async (stored, label) => {
    render(<ConceptAssignDialog {...editProps} initialOmopTable={stored} />);

    expect(screen.getByRole("combobox", { name: "Source Table" })).toHaveDisplayValue(label);
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Reviewed table" } });
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/",
      expect.objectContaining({ omop_table: stored, notes: "Reviewed table", status: "approved" }),
    ));
  });

  it("saves an intentional table change", async () => {
    render(<ConceptAssignDialog {...editProps} initialOmopTable="measurement" />);
    fireEvent.change(screen.getByRole("combobox", { name: "Source Table" }), {
      target: { value: "Observation" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/", expect.objectContaining({ omop_table: "Observation" }),
    ));
  });

  it("defaults a new mapping to Measurement", () => {
    render(<ConceptAssignDialog {...editProps} existingMappingId={undefined} />);
    expect(screen.getByRole("combobox", { name: "Source Table" })).toHaveDisplayValue("Measurement");
  });
});
