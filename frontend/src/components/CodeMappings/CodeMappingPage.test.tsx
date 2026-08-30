import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import CodeMappingPage from "./CodeMappingPage";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDelete = vi.fn();

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

/**
 * Fixtures put an *external* code system (or none) on the source side and a
 * different concept on the destination. The previous suite mapped HK-Wearable
 * codes to the concepts carrying them, which encoded the direction bug of #834.
 */
const proposedRow = {
  mapping_id: 7,
  source_vocabulary_id: "",                 // uncoded: a paper lab test name
  source_code: "M-PROTEIN, SERUM",
  source_code_description: "M-protein, serum",
  destination_concept_id: 2039000101,
  destination_concept_name: "M-PROTEIN, SERUM",
  destination_concept_code: "hkl:m-protein-serum",
  destination_vocabulary_id: "HK-Labs",
  destination_concept_class_id: "Lab Test",
  destination_omop_table: "measurement",
  destination_domain_id: "Measurement",
  status: "proposed" as const,
  notes: "",
  origin: "import",
  origin_system: "hk-labs",
  occurrence_count: 14,
  has_mapping: true,
};

const approvedRow = {
  ...proposedRow,
  mapping_id: 8,
  source_vocabulary_id: "ICD10CM",
  source_code: "C90.00",
  source_code_description: "Multiple myeloma",
  destination_concept_id: 3046299,
  destination_concept_name: "Protein.monoclonal [Mass/volume] in Serum",
  destination_concept_code: "33358-3",
  destination_vocabulary_id: "HK-Labs",
  destination_concept_class_id: "Lab Test",
  status: "approved" as const,
  origin: "curator",
  origin_system: "",
  occurrence_count: 3,
};

const reference = {
  source_code_systems: [
    { vocabulary_id: "ICD10CM", vocabulary_name: "ICD-10-CM" },
    { vocabulary_id: "LOINC", vocabulary_name: "LOINC" },
    { vocabulary_id: "ICDO3", vocabulary_name: "ICD-O-3" },
  ],
  destination_vocabularies: [
    { vocabulary_id: "SNOMED", vocabulary_name: "SNOMED", is_local: false },
    { vocabulary_id: "LOINC", vocabulary_name: "LOINC", is_local: false },
    { vocabulary_id: "HK-Labs", vocabulary_name: "HealthKey Labs", is_local: true },
  ],
  omop_tables: [
    { value: "measurement", label: "Measurement" },
    { value: "condition", label: "Condition Occurrence" },
  ],
};

function renderPage(rows = [proposedRow, approvedRow]) {
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: rows });
    if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
    if (url === "/v1/concepts/search/") {
      return Promise.resolve({
        data: {
          results: [{
            concept_id: 3046299,
            concept_name: "Protein.monoclonal [Mass/volume] in Serum",
            concept_code: "33358-3",
            vocabulary_id: "LOINC",
            domain_id: "Measurement",
            concept_class_id: "Lab Test",
            standard_concept: "S",
          }],
        },
      });
    }
    return Promise.resolve({ data: {} });
  });
  return render(
    <MemoryRouter>
      <CodeMappingPage />
    </MemoryRouter>,
  );
}

describe("CodeMappingPage", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDelete.mockReset();
    mockPost.mockResolvedValue({ data: {} });
    mockPatch.mockResolvedValue({ data: {} });
    mockDelete.mockResolvedValue({ data: {} });
  });

  it("puts the source code first and shows an uncoded source as such", async () => {
    renderPage();
    const row = (await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("M-PROTEIN, SERUM");
    // Not the destination's vocabulary: a mapping with no source code system
    // genuinely has none, and borrowing the destination's is the #834 bug.
    expect(cells[1]).toHaveTextContent("uncoded");
  });

  it("splits proposed and approved into Unmapped and Mapped sections", async () => {
    renderPage();
    expect(await screen.findByText(/Unmapped/)).toBeInTheDocument();
    expect(screen.getByText(/^Mapped/)).toBeInTheDocument();
    // Approved rows live under Mapped, which is collapsed by default.
    expect(screen.queryByText("C90.00")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/^Mapped/));
    expect(await screen.findByText("C90.00")).toBeInTheDocument();
  });

  it("offers destination tabs for standard vocabularies, not just HK-*", async () => {
    // A curator re-points a proposed mapping at a LOINC or SNOMED concept, so
    // those mappings need somewhere to live.
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
    const tabs = within(screen.getByRole("tablist", { name: "Destination vocabularies" }));
    expect(tabs.getByRole("button", { name: /SNOMED/ })).toBeInTheDocument();
    expect(tabs.getByRole("button", { name: /LOINC/ })).toBeInTheDocument();
    expect(tabs.getByRole("button", { name: /HK-Labs/ })).toBeInTheDocument();
  });

  it("has no All tab", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
    const tabs = within(screen.getByRole("tablist", { name: "Destination vocabularies" }));
    expect(tabs.queryByRole("button", { name: /^All/ })).not.toBeInTheDocument();
  });

  describe("the dialog", () => {
    const openDialog = async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      return await screen.findByText("Edit Mapping");
    };

    it("opens from a click anywhere on the row", async () => {
      await openDialog();
      expect(screen.getByText("Edit Mapping")).toBeInTheDocument();
    });

    it("does not open when the approve checkbox is clicked", async () => {
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /^Approve M-PROTEIN/ }));
      expect(screen.queryByText("Edit Mapping")).not.toBeInTheDocument();
    });

    it("labels the source field Source Code, never 'concept code'", async () => {
      await openDialog();
      expect(screen.getByText("Source Code")).toBeInTheDocument();
      expect(screen.queryByText("Source concept code")).not.toBeInTheDocument();
    });

    it("offers the source code system as a select with a blank option", async () => {
      await openDialog();
      const select = screen.getByLabelText("Source Code System") as HTMLSelectElement;
      expect(select.tagName).toBe("SELECT");
      const values = Array.from(select.options).map((o) => o.value);
      expect(values).toContain("");           // uncoded is a real answer
      expect(values).toContain("ICD10CM");
      expect(values.some((v) => v.startsWith("HK-"))).toBe(false);
    });

    it("leaves Destination Concept ID writable when editing", async () => {
      // Re-pointing a proposed mapping at a standard concept is the single most
      // common curation action; it must not require delete-and-recreate.
      await openDialog();
      const input = screen.getByLabelText("Destination Concept ID") as HTMLInputElement;
      expect(input.readOnly).toBe(false);
      fireEvent.change(input, { target: { value: "3046299" } });
      expect(input.value).toBe("3046299");
    });

    it("shows the concept class read-only, derived from the chosen concept", async () => {
      await openDialog();
      expect(screen.getByTestId("destination-concept-class")).toHaveTextContent("Lab Test");
      expect(screen.queryByLabelText("Destination Concept Class")).not.toBeInTheDocument();
    });

    it("fills the destination from a concept search result", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      const hit = await screen.findByText("Protein.monoclonal [Mass/volume] in Serum");
      fireEvent.click(hit.closest("button")!);

      await waitFor(() => {
        expect((screen.getByLabelText("Destination Concept ID") as HTMLInputElement).value)
          .toBe("3046299");
      });
      expect(screen.getByTestId("destination-concept-class")).toHaveTextContent("Lab Test");
    });

    it("shows import provenance so an SME knows what they are reviewing", async () => {
      await openDialog();
      expect(screen.getByText(/Proposed by import/)).toHaveTextContent("hk-labs");
      expect(screen.getByText(/Proposed by import/)).toHaveTextContent("14");
    });

    it("offers Update & Approve once the destination has moved", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Destination Concept ID"), {
        target: { value: "3046299" },
      });
      fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
      expect(await screen.findByRole("button", { name: "Update & Approve" })).toBeInTheDocument();
    });

    it("shows progress while the re-point runs, then what it rewrote", async () => {
      // The rewrite touches every stored row carrying this code and can run for
      // a while. A dialog that appears frozen gets clicked again; one that just
      // vanishes leaves a curator unsure anything happened.
      let release!: (value: unknown) => void;
      mockPatch.mockReturnValue(new Promise((resolve) => { release = resolve; }));

      await openDialog();
      fireEvent.change(screen.getByLabelText("Destination Concept ID"), {
        target: { value: "3046299" },
      });
      fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
      fireEvent.click(screen.getByRole("button", { name: "Update & Approve" }));

      // In flight: names both concepts so the curator can see what is moving.
      expect(await screen.findByText(/Updating concept 2039000101/)).toHaveTextContent("3046299");

      release({
        data: { repoint: { rows_updated: 1284, persons_marked_stale: 96, rows_collapsed: 2 } },
      });

      const status = await screen.findByText(/Updated 1284 row/);
      expect(status).toHaveTextContent("96 patient(s)");
      expect(status).toHaveTextContent("2 duplicate(s) collapsed");
    });

    it("patches the mapping by its own id, not its destination concept", async () => {
      // Two source codes can share one destination, which made the old
      // concept-keyed URL ambiguous about which row it addressed.
      await openDialog();
      fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalled());
      expect(mockPatch.mock.calls[0][0]).toBe("/v1/code-mappings/7/");
    });

    it("deletes a mis-keyed mapping", async () => {
      await openDialog();
      fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
      await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/v1/code-mappings/7/"));
    });
  });
});
