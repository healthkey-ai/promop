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
  const scoped = (diseases: string[] = [], options = CATEGORIES) => ({ diseases, options });
  // A field with no mapping row yet.
  const unmappedProps = {
    fieldName: "bulky_disease",
    fieldType: "text",
    fieldScope: scoped(),
    onClose: vi.fn(),
    onSaved: vi.fn(),
  };
  const base = () => screen.getByRole("radio", { name: "Base record (every patient)" });
  const diseaseSpecific = () => screen.getByRole("radio", { name: "Disease-specific" });
  const category = (code: string) => screen.getByRole("checkbox", { name: new RegExp(`^${code}`) });
  const SCOPE_URL = "/v1/field-mappings/genetic_mutations.allelic_frequency/diseases/";

  it("is not offered without a scope to edit, and such a save sends none", async () => {
    // What a caller without mapping write access gets, and a field that cannot have a scope.
    render(<ConceptAssignDialog {...editProps} />);
    expect(screen.queryByRole("radio", { name: "Disease-specific" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
    expect(screen.queryByText(/whose record this field is part of/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch.mock.calls[0][1]).not.toHaveProperty("diseases");
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("opens on a base field with no category list", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped()} />);
    expect(base()).toBeChecked();
    expect(diseaseSpecific()).not.toBeChecked();
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
  });

  it("opens on a disease-specific field with its categories ticked", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["FL", "MM"])} />);
    expect(diseaseSpecific()).toBeChecked();
    const boxes = within(screen.getByRole("group", { name: "Disease Categories" })).getAllByRole("checkbox");
    expect(boxes).toHaveLength(3);
    expect(category("FL")).toBeChecked();
    expect(category("MM")).toBeChecked();
    expect(category("BC")).not.toBeChecked();
  });

  it("keeps a category the field names that is not among the options", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["DLBCL"], [])} />);
    expect(category("DLBCL")).toBeChecked();
  });

  it("says when the categories could not be loaded, instead of that none are defined", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={{ diseases: [], options: [], optionsIncomplete: true }} />);
    fireEvent.click(diseaseSpecific());
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText("No Disease Categories are defined yet.")).not.toBeInTheDocument();
  });

  it("leaves the scope out of a save that did not change it", async () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["MM"])} />);
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Reviewed" } });
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/", expect.objectContaining({ notes: "Reviewed" }),
    ));
    expect(mockPatch.mock.calls[0][1]).not.toHaveProperty("diseases");
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("saves a scope-only change by the field's name, leaving the mapping row alone", async () => {
    const onSaved = vi.fn();
    render(<ConceptAssignDialog {...editProps} onSaved={onSaved} fieldScope={scoped(["MM"])} />);
    fireEvent.click(category("FL"));
    // The button says what will be saved.
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledWith(SCOPE_URL, { diseases: ["FL", "MM"] }));
    expect(mockPatch).not.toHaveBeenCalled();
    expect(onSaved).toHaveBeenCalled();
  });

  it.each([
    ["the notes", () => fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Also for FL" } }),
      { notes: "Also for FL" }],
    ["the unit", () => fireEvent.change(screen.getByLabelText("Unit"), { target: { value: "%" } }),
      { unit: "%" }],
    ["the table", () => fireEvent.change(screen.getByRole("combobox", { name: "Source Table" }), { target: { value: "Observation" } }),
      { omop_table: "Observation" }],
    ["the status", () => fireEvent.change(screen.getByLabelText("Status"), { target: { value: "proposed" } }),
      { status: "proposed" }],
    ["the concept", () => fireEvent.click(screen.getByTitle("Clear selection")),
      { concept: null }],
  ])("sends the scope with the mapping when %s changed too", async (_what, change, sent) => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["MM"])} />);
    change();
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/", expect.objectContaining({ ...sent, diseases: ["FL", "MM"] }),
    ));
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("saves a prefilled unit or table the row does not hold along with the scope", async () => {
    // The page opens the dialog on a suggestion's unit and table where the
    // mapping's own are blank. What is shown is what is saved.
    render(
      <ConceptAssignDialog
        {...editProps}
        initialUnit="g/dL" storedUnit=""
        initialOmopTable="Measurement" storedOmopTable=""
        fieldScope={scoped(["MM"])}
      />,
    );
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/42/",
      expect.objectContaining({ unit: "g/dL", omop_table: "Measurement", diseases: ["FL", "MM"] }),
    ));
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("makes a disease-specific field part of the base record with an empty list", async () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["FL", "MM"])} />);
    fireEvent.click(base());
    expect(screen.queryByRole("group", { name: "Disease Categories" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    await waitFor(() => expect(mockPut).toHaveBeenCalledWith(SCOPE_URL, { diseases: [] }));
  });

  it("asks for a category before saving a base field as disease-specific with none", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped()} />);
    fireEvent.click(diseaseSpecific());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one Disease Category");
    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("refuses to empty a disease-specific scope without choosing the base record", () => {
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["MM"])} />);
    fireEvent.click(category("MM"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one Disease Category");
    expect(mockPut).not.toHaveBeenCalled();
    // Choosing an answer clears it.
    fireEvent.click(base());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("saves the scope of a field with no mapping row by its name, creating no mapping", async () => {
    render(<ConceptAssignDialog {...unmappedProps} />);
    // Nothing to save yet: no concept chosen and no scope change.
    expect(screen.getByRole("button", { name: "Save Mapping" })).toBeDisabled();

    fireEvent.click(diseaseSpecific());
    // Choosing disease-specific is already something to answer for.
    expect(screen.getByRole("button", { name: "Save Scope" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose at least one Disease Category");

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

  it("shows a scope the by-name save refused under the control", async () => {
    // That endpoint answers with a plain string.
    mockPut.mockRejectedValue({ response: { status: 400, data: {
      diseases: '"Other" has no extension: its record is the base record. Leave the scope as base instead.',
    } } });
    const onSaved = vi.fn();
    render(<ConceptAssignDialog {...editProps} onSaved={onSaved} fieldScope={scoped(["MM"])} />);
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent('"Other" has no extension');
    // Under the control, not in the dialog's general error line.
    expect(screen.getByRole("group", { name: /Scope/ })).toContainElement(alert);
    expect(screen.getByRole("group", { name: /Scope/ })).toHaveAccessibleDescription(/has no extension/);
    expect(screen.queryByText(/\{"diseases"/)).not.toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Save Scope" })).toBeEnabled();
  });

  it.each([
    ["a list", ["Unknown disease XX. Choose from the Disease Categories: BC, FL, MM."]],
    ["a list per index", { 0: ["Unknown disease XX. Choose from the Disease Categories: BC, FL, MM."] }],
  ])("shows a scope the mapping save refused under the control, sent as %s", async (_shape, diseases) => {
    mockPatch.mockRejectedValue({ response: { status: 400, data: { diseases } } });
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["MM"])} />);
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Reviewed" } });
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    expect(await screen.findByRole("alert"))
      .toHaveTextContent("Unknown disease XX. Choose from the Disease Categories: BC, FL, MM.");
    expect(screen.queryByText(/\{"diseases"/)).not.toBeInTheDocument();
  });

  it("says a field the server gives no scope cannot have one", async () => {
    mockPut.mockRejectedValue({ response: { status: 404, data: { detail: "Not found." } } });
    render(<ConceptAssignDialog {...unmappedProps} />);
    fireEvent.click(diseaseSpecific());
    fireEvent.click(category("MM"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This field cannot have a scope.");
    expect(screen.queryByText(/Not found/)).not.toBeInTheDocument();
    expect(unmappedProps.onSaved).not.toHaveBeenCalled();
  });

  it("keeps any other refusal in the dialog's own error line", async () => {
    mockPut.mockRejectedValue({ response: { status: 403, data: { detail: "Mapping curator access required." } } });
    render(<ConceptAssignDialog {...editProps} fieldScope={scoped(["MM"])} />);
    fireEvent.click(category("FL"));
    fireEvent.click(screen.getByRole("button", { name: "Save Scope" }));
    expect(await screen.findByText('{"detail":"Mapping curator access required."}')).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

/** Create mode (#721): the dialog creates a field with its approved mapping. */
describe("create mode", () => {
  const CONCEPT = {
    concept_id: 4112853, concept_name: "Frailty index", concept_code: "F-1",
    vocabulary_id: "SNOMED", domain_id: "Observation", concept_class_id: "Clinical Finding",
    standard_concept: "S",
  };
  const TABS = [
    { value: "general", label: "General" },
    { value: "disease", label: "Disease" },
    { value: "labs", label: "Labs" },
  ];
  const OPTIONS = [
    { value: "BC", label: "Breast Cancer" },
    { value: "MM", label: "Multiple Myeloma" },
  ];
  const createProps = (tab: string, diseases: string[] = []) => ({
    fieldName: "",
    fieldType: "",
    create: { tab, tabOptions: TABS },
    fieldScope: { diseases, options: OPTIONS },
    onClose: vi.fn(),
    onSaved: vi.fn(),
  });
  const addButton = () => screen.getByRole("button", { name: "Add Field" });
  const pickConcept = async () => {
    fireEvent.change(screen.getByLabelText("Search concepts"), { target: { value: "frailty" } });
    fireEvent.click(await screen.findByRole("button", { name: /Frailty index/ }));
  };
  const posted = () => mockPost.mock.calls.find(([url]) => url === "/v1/custom-patient-fields/")?.[1];

  beforeEach(() => {
    mockGet.mockResolvedValue({ data: { results: [CONCEPT] } });
  });

  it("creates the field on the tab it was opened for once a concept is chosen", async () => {
    const props = createProps("labs");
    render(<ConceptAssignDialog {...props} />);
    expect(screen.getByRole("heading", { name: "Add Field" })).toBeInTheDocument();
    expect(screen.getByLabelText("Tab")).toHaveValue("labs");
    expect(screen.getByRole("radio", { name: /Base record/ })).toBeChecked();
    // A created field arrives approved, so there is no status to choose.
    expect(screen.queryByLabelText("Status")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Frailty Index" } });
    expect(screen.getByLabelText("Field name")).toHaveValue("frailty_index");
    fireEvent.change(screen.getByLabelText("Value type"), { target: { value: "number" } });
    expect(addButton()).toBeDisabled();
    await pickConcept();
    expect(addButton()).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    fireEvent.click(addButton());

    await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
    expect(posted()).toEqual({
      confirm_patient_record: true,
      display_name: "Frailty Index",
      field_name: "frailty_index",
      field_type: "number",
      mode: "editable",
      formula: undefined,
      tab: "labs",
      concept: 4112853,
      omop_table: "Measurement",
      unit: "",
      notes: "",
      diseases: [],
    });
    expect(mockPatch).not.toHaveBeenCalled();
    expect(mockPut).not.toHaveBeenCalled();
  });

  it("opens disease-specific on the disease it was given and sends that scope", async () => {
    const props = createProps("disease", ["MM"]);
    render(<ConceptAssignDialog {...props} />);
    expect(screen.getByLabelText("Tab")).toHaveValue("disease");
    expect(screen.getByRole("radio", { name: "Disease-specific" })).toBeChecked();
    expect(within(screen.getByRole("group", { name: "Disease Categories" })).getByRole("checkbox", { name: /MM/ })).toBeChecked();

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "M protein note" } });
    await pickConcept();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    fireEvent.click(addButton());

    await waitFor(() => expect(props.onSaved).toHaveBeenCalled());
    expect(posted()).toMatchObject({ field_name: "m_protein_note", tab: "disease", diseases: ["MM"] });
  });

  it("keeps a field name that was typed over when the display name changes", () => {
    render(<ConceptAssignDialog {...createProps("general")} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Frailty" } });
    fireEvent.change(screen.getByLabelText("Field name"), { target: { value: "cfs_score" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Frailty score" } });
    expect(screen.getByLabelText("Field name")).toHaveValue("cfs_score");
  });

  it("refuses disease-specific with no category ticked", async () => {
    const props = createProps("disease", ["MM"]);
    render(<ConceptAssignDialog {...props} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "M protein note" } });
    await pickConcept();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    fireEvent.click(within(screen.getByRole("group", { name: "Disease Categories" })).getByRole("checkbox", { name: /MM/ }));
    fireEvent.click(addButton());

    expect(await screen.findByText(/Choose at least one Disease Category/)).toBeInTheDocument();
    expect(posted()).toBeUndefined();
    expect(props.onSaved).not.toHaveBeenCalled();
  });

  it("needs a formula for a computed field", async () => {
    render(<ConceptAssignDialog {...createProps("general")} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Double weight" } });
    await pickConcept();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    expect(screen.queryByLabelText("Formula")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Field mode"), { target: { value: "computed" } });
    expect(addButton()).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Formula"), { target: { value: "weight * 2" } });
    fireEvent.click(addButton());
    await waitFor(() => expect(posted()).toMatchObject({ mode: "computed", formula: "weight * 2" }));
  });

  it("says which input the server refused and stays open", async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: {
      field_name: ["A custom PatientRecord field already uses this name."],
    } } });
    const props = createProps("general");
    render(<ConceptAssignDialog {...props} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Frailty Index" } });
    await pickConcept();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    fireEvent.click(addButton());

    expect(await screen.findByText("field_name: A custom PatientRecord field already uses this name.")).toBeInTheDocument();
    expect(props.onSaved).not.toHaveBeenCalled();
    expect(addButton()).toBeEnabled();
  });

  it("shows a refused scope under the scope control", async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: { diseases: ["ZZ is not a Disease Category."] } } });
    render(<ConceptAssignDialog {...createProps("disease", ["ZZ"])} />);
    // A category the list does not offer is still shown, ticked.
    expect(screen.getByRole("checkbox", { name: /ZZ/ })).toBeChecked();
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Note" } });
    await pickConcept();
    fireEvent.click(screen.getByLabelText(/I confirm/));
    fireEvent.click(addButton());
    expect(await screen.findByText("ZZ is not a Disease Category.")).toBeInTheDocument();
  });

  it("seeds Suggest from the display name", async () => {
    render(<ConceptAssignDialog {...createProps("general")} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Frailty Index" } });
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/concepts/search/", { params: { q: "Frailty Index", limit: "50" } },
    ));
  });
});
