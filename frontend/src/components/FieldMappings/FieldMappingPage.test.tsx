import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const mockGet = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
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
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") {
        return Promise.resolve({ data: MOCK_DESCRIPTORS });
      }
      if (url.startsWith("/v1/field-synonyms/batch/")) {
        return Promise.resolve({ data: {} });
      }
      return Promise.resolve({ data: [] });
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
    // Behavior tab fields should not be visible on the General tab
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

  it("shows suggestion in italic for unmapped fields with suggestions", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Blood/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Blood/));
    await waitFor(() => {
      expect(screen.getByText("hemoglobin_g_dl")).toBeInTheDocument();
    });
    // Suggestion concept code should appear
    expect(screen.getByText("718-7")).toBeInTheDocument();
    // Suggestion unit should appear
    expect(screen.getByText("g/dL")).toBeInTheDocument();
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
    // General tab should have the Computed section header (bmi is computed, tab=general)
    // The section header says "Computed" with a count — check for the bmi field name
    // after expanding the section (Computed starts collapsed)
    const computedHeader = screen.getByText(/read-only — computed by application code/);
    expect(computedHeader).toBeInTheDocument();
  });

  it("shows locked Person table for profile fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    // date_of_birth is in profile category, general tab
    // The Person section header should exist as a category section
    const personSections = screen.getAllByText("Person");
    expect(personSections.length).toBeGreaterThan(0);
  });

  it("shows locked Location table for location fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    // Location appears as both category label and locked table value
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
    // Click the dash (—) in the concept cell for smoking_status (unmapped, no suggestion)
    const dashes = screen.getAllByText("—");
    fireEvent.click(dashes[0]);
    await waitFor(() => {
      expect(screen.getByText("Assign Concept")).toBeInTheDocument();
    });
  });

  it("has new column headers: Concept, Coding, Table, Units, Synonyms", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    // Table headers should include the new columns
    expect(screen.getByText("Coding")).toBeInTheDocument();
    expect(screen.getByText("Table")).toBeInTheDocument();
    expect(screen.getByText("Units")).toBeInTheDocument();
    expect(screen.getByText("Synonyms")).toBeInTheDocument();
  });
});
