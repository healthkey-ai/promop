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
          provenance: null,
          mapping: null,
        },
        {
          field_name: "pack_years",
          field_type: "float",
          category: "needs-concept-set",
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

  it("renders field table with category sections", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("Field Concept Mappings")).toBeInTheDocument();
    });
    // "Needs Concept Assignment" appears in stats bar, dropdown, and section header
    expect(screen.getAllByText(/Needs Concept Assignment/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("smoking_status")).toBeInTheDocument();
    expect(screen.getByText("pack_years")).toBeInTheDocument();
  });

  it("shows mapped concept info for fields with mappings", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("SNOMED:229819007")).toBeInTheDocument();
    });
    expect(screen.getByText("proposed")).toBeInTheDocument();
  });

  it("filters by search text", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    const searchInput = screen.getByPlaceholderText("Search fields...");
    fireEvent.change(searchInput, { target: { value: "smoking" } });
    expect(screen.getByText("smoking_status")).toBeInTheDocument();
    expect(screen.queryByText("pack_years")).not.toBeInTheDocument();
  });

  it("shows assign button for unmapped needs-concept-set fields", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    const assignButtons = screen.getAllByText("Assign");
    expect(assignButtons.length).toBeGreaterThan(0);
  });

  it("opens concept assign dialog on Assign click", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("smoking_status")).toBeInTheDocument();
    });
    const assignButtons = screen.getAllByText("Assign");
    fireEvent.click(assignButtons[0]);
    await waitFor(() => {
      expect(screen.getByText("Assign Concept")).toBeInTheDocument();
    });
    // The dialog shows the field name
    expect(screen.getByText("smoking_status", { selector: "span" })).toBeInTheDocument();
  });
});
