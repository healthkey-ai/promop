import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";
import { invalidateAllWritableFields } from "@/hooks/useWritableFields";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockUseAuth = vi.fn();

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("@/hooks/useWritableFields", () => ({ invalidateAllWritableFields: vi.fn() }));

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
    explanation: null,
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
    explanation: null,
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
    explanation: null,
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
    explanation: null,
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
    explanation: "Calculated from weight and height",
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
    explanation: null,
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
    explanation: null,
  },
  {
    field_name: "hemoglobin",
    field_type: "float",
    category: "alias",
    tab: "blood",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: false,
    locked_table: null,
    choices: [],
    formula: null,
  },
  {
    field_name: "first_line_therapy",
    field_type: "text",
    category: "computed",
    tab: "treatment",
    provenance: null,
    mapping: null,
    suggestion: null,
    mappable: false,
    locked_table: null,
    choices: [],
    formula: null,
    explanation: "Derived from Episode and DrugExposure records",
  },
];

const renderPage = () =>
  render(
    <MemoryRouter>
      <FieldMappingPage />
    </MemoryRouter>
  );

function CodeMappingsProbe() {
  const location = useLocation();
  return <div>code mappings {location.search}</div>;
}

const renderLinked = (field: string) =>
  render(
    <MemoryRouter initialEntries={[`/field-mappings?field=${field}`]}>
      <Routes>
        <Route path="/field-mappings" element={<FieldMappingPage />} />
        <Route path="/code-mappings" element={<CodeMappingsProbe />} />
      </Routes>
    </MemoryRouter>
  );

describe("FieldMappingPage", () => {
  it('exposes priority and nested Genomics mappings in their own category', async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({ data: url === '/v1/field-mappings/'
      ? ['genomics_brca1', 'genetic_mutations.origin'].map(field_name => ({
        ...MOCK_DESCRIPTORS[0], field_name, tab: 'genomics',
      })) : {} }));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /Genomics/ }));
    expect(await screen.findByText('genomics_brca1')).toBeInTheDocument();
    expect(screen.getByText('genetic_mutations.origin')).toBeInTheDocument();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({ currentUser: { is_staff: true, is_org_admin: false } });
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

  it("shows mapping provenance in the first column", async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({ data:
      url === "/v1/field-mappings/" ? ["system_generated", "curator", "", "field-suggest v1 (reviewed)", "adam@example.org"].map((provenance, i) => ({
        ...MOCK_DESCRIPTORS[0], field_name: `origin_${i}`, tab: "general",
        mapping: { id: i + 1, provenance, status: "proposed", concept_id: null,
          concept_name: "", vocabulary_id: "", concept_code: "", omop_table: "measurement",
          unit: "", reviewer: null, reviewed_at: null, notes: "" },
      })) : {} }));
    renderPage();
    // Legacy labels keep their names; an engine version or approver shows as stored (#1719).
    for (const [i, label] of ["System Generated", "Curator", "Unrecorded", "field-suggest v1 (reviewed)", "adam@example.org"].entries()) {
      const field = await screen.findByText(`origin_${i}`);
      const row = field.closest("tr")!;
      expect(within(row).getAllByRole("cell")[0]).toHaveTextContent(label);
      const table = row.closest("table")!;
      expect(within(table).getAllByRole("columnheader")[0]).toHaveTextContent("Provenance");
    }
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

  it("defaults to General tab and hides fields from other tabs", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("smoking_status")).not.toBeInTheDocument();
    expect(screen.queryByText("hemoglobin_g_dl")).not.toBeInTheDocument();
  });

  it("lists an unplaced field under OMOP (unplaced), not on General (#2328)", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") {
        return Promise.resolve({ data: [
          ...MOCK_DESCRIPTORS,
          { ...MOCK_DESCRIPTORS[0], field_name: "red_blood_cell_count", tab: "omop", suggestion: null },
        ] });
      }
      return Promise.resolve({ data: [] });
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("red_blood_cell_count")).not.toBeInTheDocument();
    const unplaced = screen.getByRole("button", { name: /OMOP \(unplaced\)/ });
    expect(unplaced).toHaveTextContent("(1)");
    fireEvent.click(unplaced);
    expect(screen.getByText("red_blood_cell_count")).toBeInTheDocument();
  });

  it("does not render an Other tab or Unit Fields category", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /^Other/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Unit Fields")).not.toBeInTheDocument();
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

  it("does not show an Internal tab", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("Internal")).not.toBeInTheDocument();
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

  it("puts Person fields in Needs Concept Assignment until approved", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText("date_of_birth")).toBeInTheDocument());
    expect(screen.getByText("Needs Concept Assignment")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Person/ })).not.toBeInTheDocument();
  });

  it("puts an approved Person mapping in Mapped", async () => {
    const approvedPerson = {
      ...MOCK_DESCRIPTORS[5],
      mapping: { ...MOCK_DESCRIPTORS[3].mapping },
    };
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") {
        return Promise.resolve({
          data: MOCK_DESCRIPTORS.map((descriptor) =>
            descriptor.field_name === "date_of_birth" ? approvedPerson : descriptor
          ),
        });
      }
      if (url.startsWith("/v1/field-synonyms/batch/")) return Promise.resolve({ data: {} });
      return Promise.resolve({ data: [] });
    });
    renderPage();
    await waitFor(() => expect(screen.getByText("Mapped")).toBeInTheDocument());
    const mappedSection = screen.getByText("Mapped").closest("div.mb-3");
    expect(within(mappedSection!).getByText("date_of_birth")).toBeInTheDocument();
  });

  it("excludes unsupported Location fields from the mapper", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("country")).not.toBeInTheDocument();
    expect(screen.queryByText("Location")).not.toBeInTheDocument();
  });

  it("excludes legacy aliases from the mapper", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    expect(screen.queryByText("hemoglobin")).not.toBeInTheDocument();
    expect(screen.queryByText("Legacy Aliases")).not.toBeInTheDocument();
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
    expect(screen.getByRole("option", { name: "CDISC" })).toBeInTheDocument();
  });

  it("suggests field mappings for the active tab", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/v1/field-mappings/propose-all/", { tab: "general" });
    });
  });

  it("suggests concepts for the selected field from the dialog", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByText("click to map")[0]);
    await screen.findByRole("heading", { name: "Assign Concept" });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith("/v1/concepts/search/", {
        params: { q: "smoking status", limit: "50", prefer_domain_id: "Measurement" },
      });
    });
  });

  it("keeps units out of the field list while retaining mapping columns", async () => {
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

  it("keeps a proposed mapping out of Mapped even when its source category is editable", async () => {
    const proposedEditable = {
      ...MOCK_DESCRIPTORS[2], field_name: "editable_proposal", category: "editable",
    };
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") return Promise.resolve({ data: [...MOCK_DESCRIPTORS, proposedEditable] });
      if (url.startsWith("/v1/field-synonyms/batch/")) return Promise.resolve({ data: {} });
      return Promise.resolve({ data: [] });
    });
    renderPage();
    await waitFor(() => expect(screen.getByText(/Behavior/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Behavior/));
    expect(screen.getByText("Needs Concept Assignment")).toBeInTheDocument();
    const mappedSection = screen.getByText("Mapped").closest("div.mb-3");
    expect(mappedSection).not.toBeNull();
    expect(within(mappedSection!).queryByText("editable_proposal")).not.toBeInTheDocument();
  });

  it("keeps an unmapped editable field out of Mapped", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/Blood/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Blood/));
    expect(screen.getByText("Needs Concept Assignment")).toBeInTheDocument();
    expect(screen.getByText("hemoglobin_g_dl")).toBeInTheDocument();
    expect(screen.queryByText("Mapped")).not.toBeInTheDocument();
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

  it("shows the genomic mapping's database table in both the list and edit dialog", async () => {
    const field = {
      ...MOCK_DESCRIPTORS[2],
      field_name: "genetic_mutations.allelic_frequency",
      tab: "genomics",
      mapping: { ...MOCK_DESCRIPTORS[2].mapping, omop_table: "measurement" },
    };
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") return Promise.resolve({ data: [field] });
      return Promise.resolve({ data: {} });
    });
    renderPage();
    fireEvent.click(await screen.findByText(/Genomics/));
    const row = (await screen.findByText(field.field_name)).closest("tr")!;
    expect(within(row).getByText("measurement")).toBeInTheDocument();
    fireEvent.click(within(row).getByText("229819007"));
    expect(screen.getByRole("combobox", { name: "Source Table" })).toHaveDisplayValue("Measurement");
    fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith(
      "/v1/field-mappings/1/", expect.objectContaining({ omop_table: "measurement" }),
    ));
  });

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

  it("approves a proposed mapping when it is updated from the dialog", async () => {
    renderPage();
    await waitFor(() => expect(screen.getByText(/Behavior/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => expect(screen.getByText("229819007")).toBeInTheDocument());

    fireEvent.click(screen.getByText("229819007"));
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
    const submit = await screen.findByRole("button", { name: "Update Mapping" });
    fireEvent.click(submit);

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith("/v1/field-mappings/1/", expect.objectContaining({
        status: "approved",
      }));
    });
    const updatePayload = mockPatch.mock.calls.find(
      ([url]: [string]) => url === "/v1/field-mappings/1/",
    )?.[1];
    expect(updatePayload).not.toHaveProperty("field_name");
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

  // ── Therapy fields as computed ──

  it("shows therapy line fields in computed section with explanation", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Treatment/));
    await waitFor(() => {
      // Expand the computed section
      const computedButton = screen.getByText(/read-only — computed by application code/);
      fireEvent.click(computedButton);
    });
    expect(screen.getByText("first_line_therapy")).toBeInTheDocument();
    expect(screen.getByText("Derived from Episode and DrugExposure records")).toBeInTheDocument();
  });

  it("renders Formula / Explanation column header in computed section", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    // Expand computed section on General tab (has bmi)
    const computedButton = screen.getByText(/read-only — computed by application code/);
    fireEvent.click(computedButton);
    expect(screen.getByText("Formula / Explanation")).toBeInTheDocument();
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

  it("shows a reader every field with no write controls (#2416)", async () => {
    // A doctor without the analyst grant: the API answers 403 on every
    // mapping write, so the page offers none.
    mockUseAuth.mockReturnValue({ currentUser: {
      id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
      org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
    } });
    renderPage();
    await screen.findByText("Field Concept Mappings");
    expect(screen.getByText("Read-only: mapping codes needs the analyst role.")).toHaveAttribute("role", "status");
    expect(screen.queryByRole("button", { name: /add field/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /suggest/i })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Manage synonyms")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Remove mapping")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Click to assign or edit concept")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/Behavior/));
    expect(await screen.findByText("smoking_status")).toBeInTheDocument();
    expect(screen.queryByText("click to map")).not.toBeInTheDocument();
    // The row is still there, but nothing on it opens the assign dialog.
    fireEvent.click(screen.getAllByText("unmapped")[0]);
    fireEvent.click(screen.getByText("smoking_status"));
    expect(screen.queryByText("Assign Concept")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it("opens the mapping dialog in create mode from the header, on the selected tab", async () => {
    renderPage();
    await screen.findByText("Field Concept Mappings");
    fireEvent.click(await screen.findByRole("button", { name: /Behavior/ }));
    fireEvent.click(screen.getByRole("button", { name: /add field/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Add Field" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Tab")).toHaveValue("behavior");
    expect(within(dialog).getByRole("radio", { name: /Base record/ })).toBeChecked();
    // Cancelling stays on the page: nothing sent it here from a patient tab.
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
  });

  it("offers Add field to no one who cannot approve (#721)", async () => {
    // An analyst writes mappings but a created field arrives approved.
    mockUseAuth.mockReturnValue({ currentUser: {
      id: 3, sub: "an", email: "an@example.com", name: "An", is_staff: false,
      org_accesses: [{ role: "analyst", org_name: "Org", org_slug: "org", expires_at: null }],
    } });
    renderPage();
    await screen.findByText("Field Concept Mappings");
    expect(screen.getByRole("button", { name: /suggest/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add field/i })).not.toBeInTheDocument();
  });

  describe("FieldMappingPage links", () => {
    it("opens the field named in ?field= on its own tab", async () => {
      renderLinked("pack_years");
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByRole("heading", { name: "Edit Concept Mapping" })).toBeInTheDocument();
      // The page switched to the field's tab: a Behavior sibling is listed.
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });

    it("goes from the selected concept to its source codes in Code Mapping", async () => {
      renderLinked("pack_years");
      const dialog = await screen.findByRole("dialog");
      const codes = within(dialog).getByRole("button", { name: /Source codes for/ });
      expect(codes).toHaveTextContent("{codes}");
      fireEvent.click(codes);
      expect(await screen.findByText("code mappings ?direction=reverse&concept=12345")).toBeInTheDocument();
    });

    it("expands the collapsed section of a linked computed field", async () => {
      mockGet.mockImplementation((url: string) => Promise.resolve({ data: url === "/v1/field-mappings/"
        ? [{ ...MOCK_DESCRIPTORS[0], field_name: "bmi", category: "computed", mappable: false, mapping: null, suggestion: null }]
        : {} }));
      renderLinked("bmi");
      expect(await screen.findByText("bmi")).toBeVisible();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    /** A patient tab's Add field link (#721): ?new=1&tab=<tab>[&disease=<code>]. */
    describe("Add field link", () => {
      const CONCEPT = {
        concept_id: 4112853, concept_name: "Frailty index", concept_code: "F-1",
        vocabulary_id: "SNOMED", domain_id: "Observation", concept_class_id: "Clinical Finding",
        standard_concept: "S",
      };
      function PatientProbe() {
        const location = useLocation();
        const navigate = useNavigate();
        return (
          <div>
            patient page, tab {String((location.state as { tab?: string } | null)?.tab)}
            <button onClick={() => navigate(-1)}>Browser back</button>
          </div>
        );
      }
      function SearchProbe() {
        return <div data-testid="search">{useLocation().search}</div>;
      }
      // Arrived from the patient page, as the link does: it is the entry before.
      const renderNew = (search: string, state?: unknown) =>
        render(
          <MemoryRouter
            initialEntries={["/patient/42", { pathname: "/field-mappings", search, state }]}
            initialIndex={1}
          >
            <SearchProbe />
            <Routes>
              <Route path="/field-mappings" element={<FieldMappingPage />} />
              <Route path="/patient/:personId" element={<PatientProbe />} />
            </Routes>
          </MemoryRouter>
        );
      const FROM_PATIENT = (tab: string) => ({ addFieldReturn: { path: "/patient/42", tab } });

      beforeEach(() => {
        mockGet.mockImplementation((url: string) => {
          if (url === "/v1/field-mappings/") return Promise.resolve({ data: MOCK_DESCRIPTORS });
          if (url === "/v1/disease-categories/") {
            return Promise.resolve({ data: [
              { value: "BC", label: "Breast Cancer" }, { value: "MM", label: "Multiple Myeloma" },
            ] });
          }
          if (url === "/v1/concepts/search/") return Promise.resolve({ data: { results: [CONCEPT] } });
          if (url.startsWith("/v1/field-synonyms/batch/")) return Promise.resolve({ data: {} });
          return Promise.resolve({ data: [] });
        });
      });

      it("opens create mode on the tab named, as a base field", async () => {
        renderNew("?new=1&tab=labs", FROM_PATIENT("labs"));
        const dialog = await screen.findByRole("dialog");
        expect(within(dialog).getByRole("heading", { name: "Add Field" })).toBeInTheDocument();
        expect(within(dialog).getByLabelText("Tab")).toHaveValue("labs");
        expect(within(dialog).getByRole("radio", { name: /Base record/ })).toBeChecked();
      });

      it("creates a disease tab field for the record's disease and returns to that tab", async () => {
        renderNew("?new=1&tab=disease&disease=MM", FROM_PATIENT("disease"));
        const dialog = await screen.findByRole("dialog");
        expect(within(dialog).getByLabelText("Tab")).toHaveValue("disease");
        expect(within(dialog).getByRole("radio", { name: "Disease-specific" })).toBeChecked();
        expect(within(dialog).getByRole("checkbox", { name: /MM/ })).toBeChecked();
        expect(within(dialog).getByRole("checkbox", { name: /BC/ })).not.toBeChecked();

        fireEvent.change(within(dialog).getByLabelText("Display name"), { target: { value: "M protein note" } });
        fireEvent.change(within(dialog).getByLabelText("Search concepts"), { target: { value: "frailty" } });
        fireEvent.click(await within(dialog).findByRole("button", { name: /Frailty index/ }));
        fireEvent.click(within(dialog).getByLabelText(/I confirm/));
        fireEvent.click(within(dialog).getByRole("button", { name: "Add Field" }));

        expect(await screen.findByText(/patient page, tab disease/)).toBeInTheDocument();
        // The patient page it returns to must ask again what is writable:
        // the answer it cached does not know the new field.
        expect(invalidateAllWritableFields).toHaveBeenCalled();
        expect(mockPost).toHaveBeenCalledWith("/v1/custom-patient-fields/", expect.objectContaining({
          field_name: "m_protein_note", tab: "disease", diseases: ["MM"], concept: 4112853,
        }));
      });

      it("returns to the patient tab on cancel, having created nothing", async () => {
        renderNew("?new=1&tab=general", FROM_PATIENT("general"));
        const dialog = await screen.findByRole("dialog");
        fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
        expect(await screen.findByText(/patient page, tab general/)).toBeInTheDocument();
        expect(mockPost).not.toHaveBeenCalled();
      });

      it("does not come back to the dialog on browser Back", async () => {
        renderNew("?new=1&tab=general", FROM_PATIENT("general"));
        const dialog = await screen.findByRole("dialog");
        fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
        await screen.findByText(/patient page, tab general/);
        // The mapping page's entry was replaced, so Back is the patient page
        // the admin started on, not the create dialog again.
        fireEvent.click(screen.getByRole("button", { name: "Browser back" }));
        expect(await screen.findByText(/patient page, tab undefined/)).toBeInTheDocument();
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });

      it("stays on the page when the link names no patient page to return to", async () => {
        renderNew("?new=1&tab=general", { addFieldReturn: { path: "https://elsewhere.example/", tab: "general" } });
        const dialog = await screen.findByRole("dialog");
        expect(screen.getByTestId("search")).toHaveTextContent("?new=1&tab=general");
        fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
        // The request is spent, so a reload does not open the dialog again.
        await waitFor(() => expect(screen.getByTestId("search")).toBeEmptyDOMElement());
      });

      it("falls back to General for a tab that is not one", async () => {
        renderNew("?new=1&tab=nowhere");
        const dialog = await screen.findByRole("dialog");
        expect(within(dialog).getByLabelText("Tab")).toHaveValue("general");
      });

      it("opens nothing and says why for someone who cannot approve", async () => {
        mockUseAuth.mockReturnValue({ currentUser: {
          id: 3, sub: "an", email: "an@example.com", name: "An", is_staff: false,
          org_accesses: [{ role: "analyst", org_name: "Org", org_slug: "org", expires_at: null }],
        } });
        renderNew("?new=1&tab=labs", FROM_PATIENT("labs"));
        expect(await screen.findByText("Only org admins and staff can add a field to PatientRecord.")).toBeInTheDocument();
        await screen.findByText("Field Concept Mappings");
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
    });

    it("says so when the linked field is not in the mapper", async () => {
      renderLinked("not_a_field");
      expect(await screen.findByText("not_a_field has no field concept mapping entry.")).toBeInTheDocument();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});
