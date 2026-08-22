import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

vi.mock("@/api/axios", () => ({
  default: {
    get: vi.fn().mockResolvedValue({
      data: [
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
        },
        {
          field_name: "smoking_status",
          field_type: "text",
          category: "needs-concept-set",
          tab: "behavior",
          provenance: null,
          mapping: null,
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
        },
        {
          field_name: "bmi",
          field_type: "float",
          category: "computed",
          tab: "general",
          provenance: null,
          mapping: null,
        },
        {
          field_name: "date_of_birth",
          field_type: "date",
          category: "profile",
          tab: "general",
          provenance: null,
          mapping: null,
        },
      ],
    }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

const renderPage = () =>
  render(
    <MemoryRouter>
      <FieldMappingPage />
    </MemoryRouter>
  );

describe("FieldMappingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders field table with tab bar and category sections", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    // Tab bar should show tabs with counts
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
    // Click Behavior tab
    const behaviorTab = screen.getByText(/Behavior/);
    fireEvent.click(behaviorTab);
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    expect(screen.getByText("pack_years")).toBeInTheDocument();
    // General fields should be hidden now
    expect(screen.queryByText("bmi")).not.toBeInTheDocument();
  });

  it("shows mapped concept info for fields with mappings", async () => {
    renderPage();
    // Switch to Behavior tab to see pack_years
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("SNOMED:229819007")).toBeInTheDocument();
    });
    expect(screen.getByText("proposed")).toBeInTheDocument();
  });

  it("search shows results across all tabs", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    const searchInput = screen.getByPlaceholderText("Search fields (all tabs)...");
    fireEvent.change(searchInput, { target: { value: "smoking" } });
    // Should find smoking_status even though we're on General tab
    expect(screen.getByText("smoking_status")).toBeInTheDocument();
    expect(screen.queryByText("pack_years")).not.toBeInTheDocument();
  });

  it("shows assign button for unmapped needs-concept-set fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    const assignButtons = screen.getAllByText("Assign");
    expect(assignButtons.length).toBeGreaterThan(0);
  });

  it("opens concept assign dialog on Assign click", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    const assignButtons = screen.getAllByText("Assign");
    fireEvent.click(assignButtons[0]);
    await waitFor(() => {
      expect(screen.getByText("Assign Concept")).toBeInTheDocument();
    });
  });

  it("renders Synonyms button in expanded sections", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText(/Behavior/)).toBeInTheDocument();
    });
    // Switch to Behavior tab which has needs-concept-set (not collapsed by default)
    fireEvent.click(screen.getByText(/Behavior/));
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    // Synonyms buttons should appear for expanded fields
    const synonymButtons = screen.getAllByText("Synonyms");
    expect(synonymButtons.length).toBeGreaterThan(0);
  });
});
