import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import CodeMappingPage from "./CodeMappingPage";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
  },
}));

const rows = [
  {
    concept_id: 2039000002,
    concept_name: "Walking step length",
    concept_code: "HK-WEAR-STEP-LENGTH",
    concept_vocabulary_id: "HK-Wearable",
    domain_id: "Measurement",
    concept_class_id: "Clinical Observation",
    mapping_id: 7,
    source_vocabulary_id: "HK-Wearable",
    source_code: "HK-WEAR-STEP-LENGTH",
    source_code_description: "Walking step length",
    source: "HK-Wearable",
    status: "approved",
    notes: "",
    has_mapping: true,
  },
  {
    concept_id: 2039000003,
    concept_name: "Resting heart rate",
    concept_code: "HK-WEAR-RESTING-HR",
    concept_vocabulary_id: "HK-Wearable",
    domain_id: "Measurement",
    concept_class_id: "Clinical Observation",
    mapping_id: 8,
    source_vocabulary_id: "HK-Wearable",
    source_code: "HK-WEAR-RESTING-HR",
    source_code_description: "Resting heart rate",
    source: "HK-Wearable",
    status: "proposed",
    notes: "",
    has_mapping: true,
  },
];

function renderPage() {
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: rows });
    if (url === "/v1/concepts/search/") {
      return Promise.resolve({
        data: {
          results: [{
            concept_id: 2039000004,
            concept_name: "Heart rate variability RMSSD",
            concept_code: "HK-WEAR-HRV-RMSSD",
            vocabulary_id: "HK-Wearable",
            domain_id: "Measurement",
            standard_concept: null,
          }],
        },
      });
    }
    return Promise.reject(new Error(`Unexpected URL ${url}`));
  });
  return render(
    <MemoryRouter>
      <CodeMappingPage />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CodeMappingPage", () => {
  it("organizes source-code mappings by source code system", async () => {
    renderPage();

    expect(await screen.findByText("Walking step length")).toBeInTheDocument();
    expect(screen.getByText("Resting heart rate")).toBeInTheDocument();
    expect(screen.queryByText("HealthKey preferred language")).not.toBeInTheDocument();
    expect(screen.getByText(/2 source-code mappings, 1 approved, 1 proposed/)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Source code system" })).toBeInTheDocument();
    const tabs = screen.getAllByRole("button").filter((button) => button.textContent?.includes("HK-"));
    expect(tabs.map((tab) => tab.textContent)).toEqual(["HK-Wearable2"]);
    const visibleConcepts = screen.getAllByRole("row").slice(1).map((row) => row.textContent);
    expect(visibleConcepts[0]).toContain("Resting heart rate");
    expect(visibleConcepts[1]).toContain("Walking step length");

  });

  it("creates a new source code mapping", async () => {
    mockPost.mockResolvedValue({ data: {} });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "New Mapping" }));
    const dialog = screen.getByRole("heading", { name: "New Mapping" }).closest("form")!;
    fireEvent.change(within(dialog).getByLabelText("Source code system"), { target: { value: "FHIR" } });
    fireEvent.change(within(dialog).getByLabelText("Source concept code"), { target: { value: "8867-4" } });
    fireEvent.change(within(dialog).getByLabelText("Destination OMOP Concept ID"), { target: { value: "2039000003" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save Mapping" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/", expect.objectContaining({
      source_vocabulary_id: "FHIR",
      source_code: "8867-4",
      target_concept_id: 2039000003,
    })));
  });

  it("edits a mapping source code", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    renderPage();

    const row = (await screen.findByText("Walking step length")).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /Edit Walking step length/ }));
    const dialog = screen.getByRole("heading", { name: "Edit Mapping" }).closest("form")!;
    fireEvent.change(within(dialog).getByLabelText("Source concept code"), { target: { value: "8867-4" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Update Mapping" }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/2039000002/", expect.objectContaining({
      source_vocabulary_id: "HK-Wearable",
      source_code: "8867-4",
      status: "approved",
      target_concept_id: 2039000002,
    })));
  });

  it("toggles a proposed code mapping to approved from the row checkbox", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    renderPage();

    const restingRow = (await screen.findByText("Resting heart rate")).closest("tr")!;
    fireEvent.click(within(restingRow).getByTitle("Approve mapping"));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/2039000003/", expect.objectContaining({
      mapping_id: 8,
      status: "approved",
    })));
  });

  it("searches individual code suggestions from the edit dialog", async () => {
    renderPage();

    const restingRow = (await screen.findByText("Resting heart rate")).closest("tr")!;
    fireEvent.click(within(restingRow).getByRole("button", { name: /Edit Resting heart rate/ }));
    const dialog = screen.getByRole("heading", { name: "Edit Mapping" }).closest("form")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Suggest" }));

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/concepts/search/", {
      params: { q: "Resting heart rate", limit: "25" },
    }));
  });
});
