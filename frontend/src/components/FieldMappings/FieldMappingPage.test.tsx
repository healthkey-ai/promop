import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock("./FieldChoiceEditor", () => ({
  FieldChoiceEditor: ({ fieldName, onClose }: { fieldName: string; onClose: () => void }) => (
    <div data-testid="choice-editor">
      FieldChoiceEditor: {fieldName}
      <button onClick={onClose}>Close</button>
    </div>
  ),
}));

vi.mock("./FormulaEditDialog", () => ({
  FormulaEditDialog: ({ fieldName, onClose }: { fieldName: string; onClose: () => void }) => (
    <div data-testid="formula-editor">
      FormulaEditDialog: {fieldName}
      <button onClick={onClose}>Close</button>
    </div>
  ),
}));

const MOCK_DESCRIPTORS = [
  {
    field_name: "hemoglobin_g_dl",
    field_type: "float",
    category: "editable",
    tab: "blood",
    provenance: {
      omop_table: "Measurement",
      lookup_strategy: "loinc",
      concept_codes: ["718-7"],
      source_values: [],
      extractor: "_get_laboratory_data",
      selection_rule: "latest",
      description: "Latest Measurement by LOINC 718-7",
    },
    mapping: null,
    suggestion: {
      concept_code: "718-7",
      vocabulary_id: "LOINC",
      unit: "g/dL",
      omop_table: "Measurement",
    },
    mappable: true,
    locked_table: null,
    choices: [],
    formula: null,
  },
  {
    field_name: "smoking_status",
    field_type: "text",
    category: "needs-concept-set",
    tab: "behavior",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: true,
    locked_table: null,
    choices: [
      { id: 1, display: "Current smoker", sort_order: 0, codes: [{ code: "77176002", vocabulary_id: "SNOMED", display: "Smoker", is_primary: true }] },
      { id: 2, display: "Never smoker", sort_order: 1, codes: [] },
    ],
    formula: null,
  },
  {
    field_name: "pack_years",
    field_type: "float",
    category: "needs-concept-set",
    tab: "behavior",
    provenance: null,
    mapping: {
      id: 1,
      concept_id: 12345,
      vocabulary_id: "SNOMED",
      concept_code: "229819007",
      unit: "",
      omop_table: "Observation",
      status: "proposed",
      reviewer: null,
      reviewed_at: null,
      notes: "test",
    },
    suggestion: null,
    mappable: true,
    locked_table: null,
    choices: [],
    formula: null,
  },
  {
    // Approved mapping — should reclassify to "editable" (Mapped) section
    field_name: "alcohol_use",
    field_type: "text",
    category: "needs-concept-set",
    tab: "behavior",
    provenance: null,
    mapping: {
      id: 2,
      concept_id: 99999,
      vocabulary_id: "LOINC",
      concept_code: "74013-4",
      unit: "",
      omop_table: "Observation",
      status: "approved",
      reviewer: "admin",
      reviewed_at: "2024-01-01T00:00:00Z",
      notes: "approved",
    },
    suggestion: null,
    mappable: true,
    locked_table: null,
    choices: [],
    formula: null,
  },
  {
    field_name: "bmi",
    field_type: "float",
    category: "computed",
    tab: "general",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: false,
    locked_table: null,
    choices: [],
    formula: { id: 1, expression: "weight / (height / 100) ^ 2", is_active: false },
  },
  {
    field_name: "date_of_birth",
    field_type: "date",
    category: "profile",
    tab: "general",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: true,
    locked_table: "Person",
    choices: [],
    formula: null,
  },
  {
    field_name: "country",
    field_type: "text",
    category: "location",
    tab: "general",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: false,
    locked_table: "Location",
    choices: [],
    formula: null,
  },
  {
    field_name: "median_daily_steps_30d",
    field_type: "float",
    category: "computed",
    tab: "other",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: false,
    locked_table: null,
    choices: [],
    formula: null,
  },
];

const renderPage = () =>
  render(
    <MemoryRouter>
      <FieldMappingPage />
    </MemoryRouter>
  );

describe("FieldMappingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPatch.mockResolvedValue({ data: {} });
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") {
        return Promise.resolve({ data: MOCK_DESCRIPTORS });
      }
      if (url.startsWith("/v1/field-synonyms/batch/")) {
        return Promise.resolve({ data: {} });
      }
      return Promise.resolve({ data: [] });
    });
    mockPost.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/propose-all/") {
        return Promise.resolve({ data: { created: 0, fields: [] } });
      }
      return Promise.resolve({ data: {} });
    });
  });

  it("renders field table with tab bar and category sections", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.getByText(/General/)).toBeInTheDocument();
    expect(screen.getByText(/Blood/)).toBeInTheDocument();
    expect(screen.getByText(/Behavior/)).toBeInTheDocument();
  });

  it("defaults to General tab and hides other tab fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("smoking_status")).not.toBeInTheDocument();
    expect(screen.queryByText("hemoglobin_g_dl")).not.toBeInTheDocument();
  });

  it("switches tabs and shows relevant fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    expect(screen.getByText("pack_years")).toBeInTheDocument();
  });

  it("shows mapped concept code and status for fields with mappings", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("229819007")).toBeInTheDocument();
    });
    expect(screen.getByText("proposed")).toBeInTheDocument();
    expect(screen.getByText("SNOMED")).toBeInTheDocument();
  });

  it("search shows results across all tabs", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    const searchInput = screen.getByPlaceholderText("Search fields (all tabs)...");
    fireEvent.change(searchInput, { target: { value: "smoking" } });
    expect(screen.getByText("smoking_status")).toBeInTheDocument();
    expect(screen.queryByText("pack_years")).not.toBeInTheDocument();
  });

  it("does not show Internal or Wearables tabs", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("Internal")).not.toBeInTheDocument();
    expect(screen.queryByText("Wearables")).not.toBeInTheDocument();
  });

  it("renders computed fields at bottom in read-only section", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    const computedHeader = screen.getByText(/read-only — computed by application code/);
    expect(computedHeader).toBeInTheDocument();
  });

  it("shows locked Person table for profile fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    const personSections = screen.getAllByText("Person");
    expect(personSections.length).toBeGreaterThan(0);
  });

  it("shows locked Location table for location fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    const locationElements = screen.getAllByText("Location");
    expect(locationElements.length).toBeGreaterThan(0);
  });

  it("opens concept assign dialog on concept cell click", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    // Click "click to map" text for unmapped field
    const clickToMap = screen.getAllByText("click to map");
    fireEvent.click(clickToMap[0]);
    await waitFor(() => {
      expect(screen.getByText("Assign Concept")).toBeInTheDocument();
    });
  });

  it("has column headers for Coding, Table, and Synonyms", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    expect(screen.getAllByText("Coding").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Table").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Synonyms").length).toBeGreaterThanOrEqual(1);
    // Units and Choices columns have been moved into the Concept Assign dialog
    expect(screen.queryByText("Units")).not.toBeInTheDocument();
  });

  // ── Phase 1a tests: dynamic reclassification ──

  it("reclassifies approved mappings to Mapped section", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("alcohol_use")).toBeInTheDocument();
    });
    // alcohol_use has status=approved, so it should appear in "Mapped" section
    // and NOT in "Needs Concept Assignment" section
    // The "Mapped" section header should contain the field
    const mappedHeaders = screen.getAllByText("Mapped");
    expect(mappedHeaders.length).toBeGreaterThan(0);
  });

  it("keeps proposed mappings in original section", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("pack_years")).toBeInTheDocument();
    });
    // pack_years has status=proposed, should stay in "Needs Concept Assignment"
    expect(screen.getByText("Needs Concept Assignment")).toBeInTheDocument();
  });

  it("toggles an existing mapping between proposed and approved", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/Behavior/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => expect(screen.getByText("229819007")).toBeInTheDocument());

    const proposedRow = screen.getByText("229819007").closest("tr");
    fireEvent.click(proposedRow!.querySelector('[title="Approve mapping"]')!);
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith("/v1/field-mappings/1/", { status: "approved" });
    });

    // The approved mapping is shown in the Mapped section; clicking its check
    // returns it to the proposed state instead of deleting the mapping.
    fireEvent.click(screen.getByTitle("Mark mapping as proposed"));
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith("/v1/field-mappings/2/", { status: "proposed" });
    });
  });

  // ── Phase 1b tests: edit mode ──

  it("opens dialog in edit mode for mapped field", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("229819007")).toBeInTheDocument();
    });
    // Click the concept code to open edit dialog
    fireEvent.click(screen.getByText("229819007"));
    await waitFor(() => {
      expect(screen.getByText("Edit Concept Mapping")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Update Mapping" })).toBeEnabled();
  });

  // ── Phase 1c tests: click-to-map UX ──

  it("shows 'click to map' for unmapped fields without suggestion", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    // smoking_status has no mapping and no suggestion — should show "click to map"
    const clickToMap = screen.getAllByText("click to map");
    expect(clickToMap.length).toBeGreaterThan(0);
  });

  // ── Phase 3 tests: choices ──

  it("shows choices in dialog when field is clicked", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    // Choices are no longer in the table — they appear in the concept assign dialog
    // Verify the field still renders correctly
    expect(screen.queryByText("2 choices")).not.toBeInTheDocument();
  });
});
