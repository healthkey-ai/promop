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
    concept_id: 2039000001,
    concept_name: "HealthKey preferred language",
    concept_code: "HK-LANG-PREFERRED",
    concept_vocabulary_id: "HK-Language",
    domain_id: "Observation",
    concept_class_id: "Clinical Observation",
    mapping_id: null,
    source_vocabulary_id: "",
    source_code: "",
    source_code_description: "",
    source: "HK-Language",
    status: "unmapped",
    notes: "",
    has_mapping: false,
  },
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

const references = {
  vocabularies: [
    { vocabulary_id: "HK-Language", vocabulary_name: "HealthKey Language" },
    { vocabulary_id: "HK-Wearable", vocabulary_name: "HealthKey Wearable" },
    { vocabulary_id: "HK-Observation", vocabulary_name: "HealthKey Observation" },
  ],
  domains: [
    { domain_id: "Observation", domain_name: "Observation" },
    { domain_id: "Measurement", domain_name: "Measurement" },
  ],
  concept_classes: [
    { concept_class_id: "Clinical Observation", concept_class_name: "Clinical Observation" },
  ],
};

function renderPage() {
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: rows });
    if (url === "/v1/code-mappings/vocabularies/") return Promise.resolve({ data: references });
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
  it("organizes mapped and unmapped quarantined concepts by vocabulary tabs", async () => {
    renderPage();

    expect(await screen.findByText("Walking step length")).toBeInTheDocument();
    expect(screen.getByText("Resting heart rate")).toBeInTheDocument();
    expect(screen.queryByText("HealthKey preferred language")).not.toBeInTheDocument();
    expect(screen.getByText(/3 quarantined concepts, 1 approved, 1 proposed, 1 unmapped codes/)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Vocabulary" })).toBeInTheDocument();
    const tabs = screen.getAllByRole("button").filter((button) => button.textContent?.includes("HK-"));
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "HK-Wearable2",
      "HK-Language1/1",
      "HK-Observation0",
    ]);
    const visibleConcepts = screen.getAllByRole("row").slice(1).map((row) => row.textContent);
    expect(visibleConcepts[0]).toContain("Resting heart rate");
    expect(visibleConcepts[1]).toContain("Walking step length");

    fireEvent.click(screen.getByRole("button", { name: /HK-Language/ }));
    expect(screen.getByText("HealthKey preferred language")).toBeInTheDocument();
    expect(screen.getByText("Unmapped")).toBeInTheDocument();
    expect(screen.queryByText("Walking step length")).not.toBeInTheDocument();
  });

  it("creates a new source code mapping", async () => {
    mockPost.mockResolvedValue({ data: {} });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "New Code" }));
    const dialog = screen.getByRole("heading", { name: "New Code" }).closest("form")!;
    fireEvent.change(within(dialog).getByLabelText("Source vocabulary"), { target: { value: "HK-Wearable" } });
    fireEvent.change(within(dialog).getByLabelText("Code"), { target: { value: "HK-WEAR-HRV-RMSSD" } });
    fireEvent.change(within(dialog).getByLabelText("Destination OMOP Concept ID"), { target: { value: "2039000003" } });
    fireEvent.change(within(dialog).getByLabelText("Destination concept name"), { target: { value: "Heart rate variability RMSSD" } });
    fireEvent.change(within(dialog).getByLabelText("Target vocabulary"), { target: { value: "HK-Wearable" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save Mapping" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/", expect.objectContaining({
      source_vocabulary_id: "HK-Wearable",
      source_code: "HK-WEAR-HRV-RMSSD",
      target_concept_id: 2039000003,
      target_concept_name: "Heart rate variability RMSSD",
      target_vocabulary_id: "HK-Wearable",
    })));
  });

  it("edits an unmapped concept to add a source code", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    renderPage();

    await screen.findByText("Walking step length");
    fireEvent.click(screen.getByRole("button", { name: /HK-Language/ }));
    const languageRow = screen.getByText("HealthKey preferred language").closest("tr")!;
    fireEvent.click(within(languageRow).getByRole("button", { name: /Edit HealthKey preferred language/ }));
    const dialog = screen.getByRole("heading", { name: "Edit Code" }).closest("form")!;
    fireEvent.change(within(dialog).getByLabelText("Code"), { target: { value: "HK-LANG-EN" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Update/Approve Mapping" }));

    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/2039000001/", expect.objectContaining({
      source_vocabulary_id: "HK-Language",
      source_code: "HK-LANG-EN",
      status: "approved",
      target_concept_id: 2039000001,
    })));
  });

  it("suggests proposed code mappings for the active vocabulary tab", async () => {
    mockPost.mockResolvedValue({ data: { created: 1, codes: ["HK-Language:HK-LANG-PREFERRED"] } });
    renderPage();

    await screen.findByText("Walking step length");
    fireEvent.click(screen.getByRole("button", { name: /HK-Language/ }));
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/propose-all/", {
      source: "HK-Language",
    }));
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
    const dialog = screen.getByRole("heading", { name: "Edit Code" }).closest("form")!;
    fireEvent.click(within(dialog).getByRole("button", { name: "Suggest" }));

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/concepts/search/", {
      params: { q: "Resting heart rate", limit: "25" },
    }));
  });
});
