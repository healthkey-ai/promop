import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConceptAssignDialog } from "./ConceptAssignDialog";

const { mockGet, mockPatch, mockPost, mockPut } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPatch: vi.fn(),
  mockPost: vi.fn(),
  mockPut: vi.fn(),
}));

vi.mock("@/api/axios", () => ({
  default: { get: mockGet, patch: mockPatch, post: mockPost, put: mockPut },
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
  mockPut.mockResolvedValue({ data: {} });
  mockGet.mockResolvedValue({ data: { results: [] } });
});

describe("{codes}", () => {
  it("opens the selected concept's source codes", () => {
    const onShowSourceCodes = vi.fn();
    render(<ConceptAssignDialog {...editProps} initialConceptName="Allele frequency" onShowSourceCodes={onShowSourceCodes} />);
    fireEvent.click(screen.getByRole("button", { name: "Source codes for Allele frequency" }));
    expect(onShowSourceCodes).toHaveBeenCalledWith(123);
  });

  it("is absent without a handler or a selected concept", () => {
    const { unmount } = render(<ConceptAssignDialog {...editProps} />);
    expect(screen.queryByText("{codes}")).not.toBeInTheDocument();
    unmount();
    render(<ConceptAssignDialog {...editProps} initialConceptId={null} onShowSourceCodes={vi.fn()} />);
    expect(screen.queryByText("{codes}")).not.toBeInTheDocument();
  });
});

describe("Suggest", () => {
  it("searches with the field's suggest query", async () => {
    render(<ConceptAssignDialog {...editProps} fieldName="genomics_t414" suggestQuery="t(4;14)" />);
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/concepts/search/", { params: { q: "t(4;14)", limit: "50" } },
    ));
  });

  it("falls back to the field name without a suggest query", async () => {
    render(<ConceptAssignDialog {...editProps} fieldName="smoking_status" />);
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/concepts/search/", { params: { q: "smoking status", limit: "50" } },
    ));
  });
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

describe("Scope (#2332)", () => {
  const CATEGORIES = [
    { value: "BC", label: "Breast Cancer" },
    { value: "FL", label: "Follicular Lymphoma" },
    { value: "MM", label: "Multiple Myeloma" },
  ];
  const scopeProps = { ...editProps, canEditScope: true, diseaseOptions: CATEGORIES };
  // A field with no mapping row yet.
  const unmappedProps = {
    fieldName: "bulky_disease",
    fieldType: "text",
    canEditScope: true,
    diseaseOptions: CATEGORIES,
    onClose: vi.fn(),
    onSaved: vi.fn(),
  };
  const base = () => screen.getByRole("radio", { name: "Base record (every patient)" });
  const diseaseSpecific = () => screen.getByRole("radio", { name: "Disease-specific" });
  const category = (code: string) => screen.getByRole("checkbox", { name: new RegExp(`^${code}`) });

  it("is not offered to a caller who may not write mappings, and such a save sends no scope", async () => {
    render(<ConceptAssignDialog {...editProps} initialDiseases={["MM"]} diseaseOptions={CATEGORIES} />);
    expect(screen.queryByRole("radio", { name: "Disease-specific" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
    expect(screen.queryByText(/whose record this field is part of/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch.mock.calls[0][1]).not.toHaveProperty("diseases");
  });

  it("opens on a base field with no category list", () => {
    render(<ConceptAssignDialog {...scopeProps} />);
    expect(base()).toBeChecked();
    expect(diseaseSpecific()).not.toBeChecked();
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
  });

  it("opens on a disease-specific field with its categories ticked", () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["FL", "MM"]} />);
    expect(diseaseSpecific()).toBeChecked();
    const boxes = within(screen.getByRole("group", { name: "Disease Categories" })).getAllByRole("checkbox");
    expect(boxes).toHaveLength(3);
    expect(category("FL")).toBeChecked();
    expect(category("MM")).toBeChecked();
    expect(category("BC")).not.toBeChecked();
  });

  it("keeps a category the field names that is not among the options", () => {
    render(<ConceptAssignDialog {...scopeProps} diseaseOptions={[]} initialDiseases={["DLBCL"]} />);
    expect(category("DLBCL")).toBeChecked();
  });

  it("leaves the scope out of a save that did not change it", async () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/", expect.objectContaining({ notes: "Reviewed" }),
    ));
    expect(mockPatch.mock.calls[0][1]).not.toHaveProperty("diseases");
  });

  it("sends the scope alone when only the scope changed, so the mapping row is not rewritten", async () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/", { diseases: ["FL", "MM"] },
    ));
    expect(scopeProps.onSaved).toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("sends the scope with the mapping when both changed", async () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Also for FL" } });
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/",
      expect.objectContaining({ notes: "Also for FL", concept: 123, diseases: ["FL", "MM"] }),
    ));
  });

  it("makes a disease-specific field part of the base record with an empty list", async () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["FL", "MM"]} />);
    fireEvent.click(base());
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/field-mappings/42/", { diseases: [] }));
  });

  it("asks for a category before saving a base field as disease-specific with none", () => {
    render(<ConceptAssignDialog {...scopeProps} />);
    fireEvent.click(diseaseSpecific());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one Disease Category");
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it("refuses to empty a disease-specific scope without choosing the base record", () => {
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.click(category("MM"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one Disease Category");
    expect(mockPatch).not.toHaveBeenCalled();
    // Choosing an answer clears it.
    fireEvent.click(base());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("saves the scope of a field with no mapping row by its name, creating no mapping", async () => {
    render(<ConceptAssignDialog {...unmappedProps} />);
    // Nothing to save yet: no concept chosen and no scope change.
    expect(screen.getByRole("button", { name: "Save Mapping" })).toBeDisabled();

    fireEvent.click(diseaseSpecific());
    fireEvent.click(category("MM"));
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledWith(
      "/v1/field-mappings/bulky_disease/diseases/", { diseases: ["FL", "MM"] },
    ));
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
    expect(unmappedProps.onSaved).toHaveBeenCalled();
  });

  it("sends the scope with a new mapping", async () => {
    mockGet.mockResolvedValue({ data: { results: [{
      concept_id: 900, concept_name: "Bulky disease", concept_code: "B-1", vocabulary_id: "SNOMED",
      domain_id: "Observation", concept_class_id: "Clinical Finding", standard_concept: "S",
    }] } });
    render(<ConceptAssignDialog {...unmappedProps} />);
    fireEvent.change(screen.getByPlaceholderText("Search concepts..."), { target: { value: "bulky" } });
    fireEvent.click(await screen.findByText("Bulky disease"));
    fireEvent.click(diseaseSpecific());
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Save Mapping" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/field-mappings/",
      expect.objectContaining({ field_name: "bulky_disease", concept: 900, diseases: ["FL"] }),
    ));
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("shows a scope the mapping save refused under the control", async () => {
    mockPatch.mockRejectedValue({ response: { status: 400, data: {
      diseases: ['Unknown disease XX. Choose from the Disease Categories: BC, FL, MM.'],
    } } });
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Unknown disease XX. Choose from the Disease Categories: BC, FL, MM.");
    // Under the control, not in the dialog's general error line.
    expect(screen.getByRole("group", { name: /Scope/ })).toContainElement(alert);
    expect(screen.getByRole("group", { name: /Scope/ })).toHaveAccessibleDescription(/Unknown disease XX/);
    expect(screen.queryByText(/\{"diseases"/)).not.toBeInTheDocument();
    expect(scopeProps.onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Update Mapping" })).toBeEnabled();
  });

  it("shows a scope the by-name save refused under the control", async () => {
    // That endpoint answers with a plain string, not a list.
    mockPut.mockRejectedValue({ response: { status: 400, data: {
      diseases: '"Other" has no extension: its record is the base record. Leave the scope as base instead.',
    } } });
    render(<ConceptAssignDialog {...unmappedProps} />);
    fireEvent.click(diseaseSpecific());
    fireEvent.click(category("MM"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(await screen.findByRole("alert")).toHaveTextContent('"Other" has no extension');
    expect(unmappedProps.onSaved).not.toHaveBeenCalled();
  });

  it("keeps any other refusal in the dialog's own error line", async () => {
    mockPatch.mockRejectedValue({ response: { status: 403, data: { detail: "Mapping curator access required." } } });
    render(<ConceptAssignDialog {...scopeProps} initialDiseases={["MM"]} />);
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    expect(await screen.findByText('{"detail":"Mapping curator access required."}')).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
