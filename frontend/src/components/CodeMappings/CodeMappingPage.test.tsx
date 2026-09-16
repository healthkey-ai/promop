import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import CodeMappingPage from "./CodeMappingPage";
import CodeMappingAccuracyPage from "./CodeMappingAccuracyPage";

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
  domain_id: "Measurement",
  source_vocabulary_id: "",                 // uncoded: a paper lab test name
  source_code: "M-PROTEIN, SERUM",
  source_code_description: "M-protein, serum",
  source_concept_id: null,
  destination_concept_id: 2039000101,
  destination_concept_name: "M-PROTEIN, SERUM",
  destination_concept_code: "hkl:m-protein-serum",
  destination_vocabulary_id: "HK-Labs",
  destination_concept_class_id: "Lab Test",
  destination_omop_table: "measurement",
  destination_domain_id: "Measurement",
  standard_concept: null,                   // minted, not an Athena standard
  status: "proposed" as const,
  notes: "",
  origin: "import",
  origin_system: "hk-labs",
  created_by: "",
  reviewer: "",              // never approved: the queue row this dialog exists for
  reviewed_at: null,
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
  created_by: "zoe@example.com",
  reviewer: "ada@example.com",          // signed off by someone other than its author
  reviewed_at: "2026-08-31T09:14:00Z",
  occurrence_count: 3,
};

/** Shape of GET /v1/code-mappings/reference/. */
const reference = {
  domains: [
    { domain_id: "Condition", label: "Condition — diagnoses, problems, findings" },
    { domain_id: "Drug", label: "Drug — medications and substances" },
    { domain_id: "Measurement", label: "Measurement — labs and quantitative results" },
    { domain_id: "Observation", label: "Observation — everything else recorded" },
    { domain_id: "Procedure", label: "Procedure — interventions" },
  ],
  source_code_systems_by_domain: {
    Condition: [
      { vocabulary_id: "", label: "None — uncoded / free text (common for labs)" },
      { vocabulary_id: "SNOMED", label: "SNOMED CT" },
      { vocabulary_id: "ICD10CM", label: "ICD-10-CM" },
      { vocabulary_id: "ICDO3", label: "ICD-O-3" },
    ],
    Drug: [
      { vocabulary_id: "", label: "None — uncoded / free text (common for labs)" },
      { vocabulary_id: "RxNorm", label: "RxNorm" },
      { vocabulary_id: "NDC", label: "NDC" },
    ],
    Measurement: [
      { vocabulary_id: "", label: "None — uncoded / free text (common for labs)" },
      { vocabulary_id: "LOINC", label: "LOINC" },
      { vocabulary_id: "SNOMED", label: "SNOMED CT" },
      { vocabulary_id: "CPT4", label: "CPT-4" },
    ],
    Observation: [
      { vocabulary_id: "", label: "None — uncoded / free text (common for labs)" },
      { vocabulary_id: "SNOMED", label: "SNOMED CT" },
      { vocabulary_id: "LOINC", label: "LOINC" },
    ],
    Procedure: [
      { vocabulary_id: "", label: "None — uncoded / free text (common for labs)" },
      { vocabulary_id: "SNOMED", label: "SNOMED CT" },
      { vocabulary_id: "CPT4", label: "CPT-4" },
    ],
  },
  destination_vocabularies: [
    { vocabulary_id: "SNOMED", vocabulary_name: "SNOMED", is_local: false },
    { vocabulary_id: "LOINC", vocabulary_name: "LOINC", is_local: false },
    { vocabulary_id: "HK-Labs", vocabulary_name: "HealthKey Labs", is_local: true },
  ],
  omop_tables: {
    Condition: "condition",
    Drug: "drug_exposure",
    Measurement: "measurement",
    Observation: "observation",
    Procedure: "procedure",
  },
  // The API supplies the full tab catalog, including source vocabularies whose
  // current queue contains only approved mappings. ICD10CM rows are merged
  // into the ICD-10 tab (#1028) via VOCABULARY_ALIASES.
  source_vocabulary_tabs: [
    { vocabulary_id: "", label: "Uncoded", is_standard: false },
    { vocabulary_id: "ICD10", label: "ICD-10", is_standard: false },
  ],
};

const loincHit = {
  concept_id: 3046299,
  concept_name: "Protein.monoclonal [Mass/volume] in Serum",
  concept_code: "33358-3",
  vocabulary_id: "LOINC",
  domain_id: "Measurement",
  concept_class_id: "Lab Test",
  standard_concept: "S",
  measurement_type: "quantitative" as const,
  suggested_unit: "mg/dL",
};

type TestMappingRow = Omit<typeof proposedRow, "status"> & {
  status: "proposed" | "approved" | "rejected" | "unmapped";
  mapping_origin?: "athena" | "healthkey";
  destination_count?: number;
  source_retired?: boolean | null;
  source_retirement_evidence?: string[];
};

/** A /suggest-runs/<id>/ payload, defaulted to a finished run. */
function suggestRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "11111111-1111-1111-1111-111111111111",
    state: "success",
    source_vocabulary_id: "",
    total: 0,
    retrieved: 0,
    done: 0,
    destinations: 0,
    remaining: 0,
    strategy_counts: {},
    landed_in: {},
    model_version: "v0.2",
    error: "",
    ...overrides,
  };
}

function renderPage(rows: TestMappingRow[] = [proposedRow, approvedRow]) {
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: [...rows] });
    if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
    if (url === "/v1/concepts/search/") {
      return Promise.resolve({ data: { results: [loincHit] } });
    }
    return Promise.resolve({ data: {} });
  });
  return render(
    <MemoryRouter>
      <CodeMappingPage />
    </MemoryRouter>,
  );
}

/**
 * The accessible name of a control, computed the way a screen reader would
 * reach it: aria-label, then an explicitly associated <label>, then a wrapping
 * one. Used by the regression test for the unlabelled source-code input.
 */
function accessibleName(el: Element): string {
  const aria = el.getAttribute("aria-label");
  if (aria && aria.trim()) return aria.trim();
  const id = el.getAttribute("id");
  if (id) {
    const explicit = el.ownerDocument.querySelector(`label[for="${id}"]`);
    if (explicit?.textContent?.trim()) return explicit.textContent.trim();
  }
  const wrapping = el.closest("label");
  if (wrapping?.textContent?.trim()) return wrapping.textContent.trim();
  return "";
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

  describe("duplicate source-code errors", () => {
    const proposed = { ...proposedRow, mapping_id: 101, source_vocabulary_id: "ICD10", source_code: "A02.0" };
    const mapped = { ...approvedRow, mapping_id: 102, source_code: "A02.0" };
    const athena = { ...mapped, mapping_id: 103, mapping_origin: "athena" as const };

    it("flags all three sections and reveals exact rows despite search and collapsed sections", async () => {
      const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
      renderPage([proposed, mapped, athena]);
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveClass("text-red-800", "bg-red-50");
      expect(alert).toHaveTextContent("1 duplicate source code on this tab");
      expect(within(alert).getAllByRole("link")).toHaveLength(3);

      fireEvent.change(screen.getByRole("textbox", { name: "Search mappings" }), { target: { value: "no-match" } });
      expect(screen.getByRole("alert")).toBeInTheDocument();
      fireEvent.click(within(alert).getByRole("link", { name: /Athena Mapped.*#103/ }));
      expect(screen.getByRole("textbox", { name: "Search mappings" })).toHaveValue("");
      await waitFor(() => expect(document.getElementById("code-mapping-103")).toHaveFocus());
      expect(scroll).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });

      fireEvent.click(within(alert).getByRole("link", { name: /— Mapped.*#102/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-102")).toHaveFocus());
      fireEvent.click(within(alert).getByRole("link", { name: /Unmapped.*#101/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-101")).toHaveFocus());

      fireEvent.click(screen.getByRole("button", { name: /Athena Mapped \(1\)/ }));
      fireEvent.click(within(alert).getByRole("link", { name: /Athena Mapped.*#103/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-103")).toHaveFocus());
      scroll.mockRestore();
    });

    it("includes hidden rejected duplicates and reveals them on navigation", async () => {
      renderPage([proposed, { ...mapped, status: "rejected" }]);
      const alert = await screen.findByRole("alert");
      expect(document.getElementById("code-mapping-102")).not.toBeInTheDocument();
      fireEvent.click(within(alert).getByRole("link", { name: /rejected.*#102/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-102")).toHaveFocus());
    });

    it("normalizes case and whitespace without conflating meaningful punctuation", async () => {
      renderPage([proposed, { ...mapped, source_code: " a02.0 " }, { ...athena, source_code: "A020" }]);
      const alert = await screen.findByRole("alert");
      expect(within(alert).getAllByRole("link")).toHaveLength(2);
    });

    it("does not flag distinct codes, blanks, or the same code in unrelated vocabularies on Overall", async () => {
      renderPage([proposed, { ...mapped, source_vocabulary_id: "LOINC" },
        { ...proposed, mapping_id: 104, source_code: "" }, { ...mapped, mapping_id: 105, source_code: " " }]);
      await screen.findByRole("tab", { name: /Overall/ });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("keeps real duplicate groups separate on Overall and scopes other tabs", async () => {
      renderPage([proposed, mapped, { ...proposed, mapping_id: 104, source_vocabulary_id: "LOINC" }]);
      await screen.findByRole("alert");
      fireEvent.click(screen.getByRole("tab", { name: /LOINC/ }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
      const alert = screen.getByRole("alert");
      expect(within(alert).getAllByRole("link")).toHaveLength(2);
      fireEvent.click(within(alert).getByRole("link", { name: /Unmapped.*#101/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-101")).toHaveFocus());
    });

    it("detects duplicates within a section and clears the error after curator deletion", async () => {
      const rows: TestMappingRow[] = [proposed, { ...mapped, status: "proposed" }];
      renderPage(rows);
      const alert = await screen.findByRole("alert");
      fireEvent.click(within(alert).getByRole("link", { name: /#102/ }));
      const row = document.getElementById("code-mapping-102")!;
      fireEvent.click(within(row).getByRole("button", { name: "Edit A02.0" }));
      mockDelete.mockImplementationOnce(() => {
        rows.splice(1, 1);
        return Promise.resolve({ data: {} });
      });
      fireEvent.click(screen.getByRole("button", { name: "Delete" }));
      await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/v1/code-mappings/102/"));
      await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
      expect(await screen.findByRole("button", { name: "Edit A02.0" })).toBeInTheDocument();
    });
  });

  it("shows the exact Athena duplicate error when table approval is blocked", async () => {
    const message = "This source and destination map is already supplied by Athena";
    mockPatch.mockRejectedValueOnce({ response: { data: { detail: message } } });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Approve M-PROTEIN, SERUM" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the exact Athena duplicate error inside the edit dialog", async () => {
    const message = "This source and destination map is already supplied by Athena";
    mockPatch.mockRejectedValueOnce({ response: { data: { detail: message } } });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit M-PROTEIN, SERUM" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Update Mapping" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toBe(message);
    expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.any(Object));
  });

  describe("source retirement and section sorting", () => {
    const high: TestMappingRow = { ...proposedRow, mapping_id: 31, source_vocabulary_id: "ICD10", source_code: "Z10",
      origin_system: "Zulu", source_code_description: "Zebra", destination_concept_name: "Zinc", destination_concept_id: 20,
      source_retired: true, source_retirement_evidence: ["Athena ICD10CM concept 45582496: invalid reason D; validity ended 2022-09-30"],
      occurrence_count: 20, destination_count: 20, status: "unmapped" };
    const low: TestMappingRow = { ...high, mapping_id: 32, source_code: "A2", origin_system: "Alpha",
      source_code_description: "Apple", destination_concept_name: "Apple", destination_concept_id: 3, source_retired: false,
      source_retirement_evidence: [], occurrence_count: 3, destination_count: 3, status: "proposed" };
    const ids = (table: HTMLElement) => Array.from(table.querySelectorAll("tbody tr[id]")).map((row) => row.id);

    it("shows retirement in the dialog, where #1080 left it after taking its column", async () => {
      // #1080 replaced the Retired column with Seen and added Dest count. The
      // retirement metadata is still served and still shown, but only once a
      // curator opens the row -- so this is the only place it can be asserted.
      renderPage([high, low]);
      const table = await screen.findByRole("table", { name: "Unmapped mappings" });
      const headers = within(table).getAllByRole("columnheader");
      expect(headers[1]).toHaveTextContent("Source code");
      expect(headers[2]).toHaveTextContent("Seen");
      expect(within(table).queryByRole("columnheader", { name: "Retired" })).not.toBeInTheDocument();
      const row = document.getElementById("code-mapping-31")!;
      fireEvent.click(within(row).getByRole("button", { name: "Edit Z10" }));
      await waitFor(() => expect(screen.queryByText("Loading source destinations…")).not.toBeInTheDocument());
      expect(screen.getByTestId("source-retirement")).toHaveValue("Retired");
      expect(within(screen.getByRole("dialog")).getByRole("status")).toHaveTextContent("invalid reason D");
      fireEvent.change(screen.getByLabelText("Source Code Value"), { target: { value: "A3" } });
      expect(screen.getByTestId("source-retirement")).toHaveValue("Unknown");
      expect(within(screen.getByRole("dialog")).queryByRole("status")).not.toBeInTheDocument();
    });

    it.each(["Provenance", "Source code", "Seen", "Source description", "Destination concept", "Concept ID", "Dest count", "Status"])(
      "sorts %s ascending and descending", async (column) => {
        renderPage([high, low]);
        const table = await screen.findByRole("table", { name: "Unmapped mappings" });
        const button = within(table).getByRole("button", { name: column });
        fireEvent.click(button);
        expect(ids(table)).toEqual(["code-mapping-32", "code-mapping-31"]);
        expect(button.closest("th")).toHaveAttribute("aria-sort", "ascending");
        fireEvent.click(button);
        expect(ids(table)).toEqual(["code-mapping-31", "code-mapping-32"]);
        expect(button.closest("th")).toHaveAttribute("aria-sort", "descending");
      },
    );

    it("keeps each section's sort independent without moving mappings between sections", async () => {
      renderPage([high, low,
        { ...high, mapping_id: 41, status: "approved" }, { ...low, mapping_id: 42, status: "approved" },
        { ...high, mapping_id: 51, status: "approved", mapping_origin: "athena" },
        { ...low, mapping_id: 52, status: "approved", mapping_origin: "athena" }]);
      const unmapped = await screen.findByRole("table", { name: "Unmapped mappings" });
      fireEvent.click(screen.getByRole("button", { name: /^Mapped \(/ }));
      fireEvent.click(screen.getByRole("button", { name: /^Athena Mapped \(/ }));
      const mapped = screen.getByRole("table", { name: "Mapped mappings" });
      const athena = screen.getByRole("table", { name: "Athena Mapped mappings" });
      for (const table of [unmapped, mapped, athena]) {
        fireEvent.click(within(table).getByRole("button", { name: "Concept ID" }));
      }
      fireEvent.click(within(mapped).getByRole("button", { name: "Concept ID" }));
      expect(ids(unmapped)).toEqual(["code-mapping-32", "code-mapping-31"]);
      expect(ids(mapped)).toEqual(["code-mapping-41", "code-mapping-42"]);
      expect(ids(athena)).toEqual(["code-mapping-52", "code-mapping-51"]);
      expect(within(athena).queryByRole("columnheader", { name: "Status" })).not.toBeInTheDocument();
    });

    it("still labels missing retirement metadata as Unknown in the dialog", async () => {
      // The Retired column and its sort went with #1080, so "sorts Unknown
      // last" has no subject any more. What survives is the distinction the
      // label exists for: absent metadata is not evidence of retirement.
      renderPage([{ ...low, mapping_id: 33, source_code: "A3", source_retired: null }]);
      const cell = await screen.findByText("A3", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(screen.getByTestId("source-retirement")).toHaveValue("Unknown");
    });
  });

  it("puts the source code first without repeating the selected source-system tab", async () => {
    renderPage();
    const row = (await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    expect(cells[1]).toHaveTextContent("M-PROTEIN, SERUM");
    expect(screen.queryByRole("columnheader", { name: "Source code system" })).not.toBeInTheDocument();
  });

  it("shows source descriptions beside source codes, and no OMOP table column", async () => {
    // Seen came back as its own column in #1080, so only OMOP table is gone.
    renderPage();
    const row = (await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    expect(cells[3]).toHaveTextContent("M-protein, serum");
    expect(screen.getByRole("columnheader", { name: "Source description" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "OMOP table" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Seen" })).toBeInTheDocument();
  });

  it("searches mappings across source-vocabulary tabs", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });

    fireEvent.change(screen.getByRole("textbox", { name: "Search mappings" }), {
      target: { value: "C90.00" },
    });
    // The matching ICD-10-CM row lives in the non-active tab and is approved,
    // so expand its section after the global search has located it.
    fireEvent.click(screen.getByRole("button", { name: /^Mapped/ }));
    expect(await screen.findByText("C90.00", { selector: "td" })).toBeInTheDocument();
  });

  it("splits proposed and approved into Unmapped and Mapped sections", async () => {
    renderPage();
    expect(await screen.findByText(/Unmapped/)).toBeInTheDocument();
    expect(screen.getByText(/^Mapped/)).toBeInTheDocument();
    // Approved rows live under Mapped, which is collapsed by default.
    expect(screen.queryByText("C90.00")).not.toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("tablist", { name: "Source vocabularies" }))
      .getByRole("tab", { name: /ICD-10/ }));
    fireEvent.click(screen.getByText(/^Mapped/));
    expect(await screen.findByText("C90.00")).toBeInTheDocument();
  });

  it("can collapse and expand the Unmapped section", async () => {
    renderPage();
    const unmapped = await screen.findByRole("button", { name: /Unmapped/ });
    expect(screen.getByText("M-PROTEIN, SERUM", { selector: "td" })).toBeInTheDocument();

    fireEvent.click(unmapped);
    expect(screen.queryByText("M-PROTEIN, SERUM", { selector: "td" })).not.toBeInTheDocument();

    fireEvent.click(unmapped);
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).toBeInTheDocument();
  });

  it("offers tabs for the source vocabularies represented in the queue", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
    const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
    expect(tabs.getByRole("tab", { name: /Uncoded/ })).toBeInTheDocument();
    expect(tabs.getByRole("tab", { name: /ICD-10/ })).toBeInTheDocument();
  });

  it("selects Uncoded instead of falling back to the default vocabulary", async () => {
    const proposedIcd10Row = { ...approvedRow, status: "proposed" as const };
    renderPage([proposedRow, proposedIcd10Row]);
    const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
    const uncoded = tabs.getByRole("tab", { name: /Uncoded/ });
    const icd10 = tabs.getByRole("tab", { name: /ICD-10/ });

    fireEvent.click(icd10);
    expect(icd10).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("C90.00", { selector: "td" })).toBeInTheDocument();

    fireEvent.click(uncoded);
    expect(uncoded).toHaveAttribute("aria-selected", "true");
    expect(icd10).toHaveAttribute("aria-selected", "false");
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).toBeInTheDocument();
    expect(screen.queryByText("C90.00", { selector: "td" })).not.toBeInTheDocument();
  });

  it("adds a tab for a source system that arrives in SCCM data", async () => {
    renderPage([{ ...proposedRow, source_vocabulary_id: "MedDRA", source_code: "10000001" }]);
    const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
    const medDra = tabs.getByRole("tab", { name: /MedDRA/ });
    fireEvent.click(medDra);
    expect(await screen.findByText("10000001", { selector: "td" })).toBeInTheDocument();
  });

  it("has no All tab", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
    const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
    expect(tabs.queryByRole("tab", { name: /^All/ })).not.toBeInTheDocument();
  });

  it("lands on the tab that has review work, not the first tab", async () => {
    // SNOMED sorts first; the proposals are in HK-Labs. Defaulting to SNOMED
    // would make the queue look empty when it is not.
    renderPage();
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).toBeInTheDocument();
  });

  describe("the dialog", () => {
    const openDialog = async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      return await screen.findByText("Edit Mapping");
    };

    it("shows individual candidates above search and preserves an early choice when the winner arrives", async () => {
      await openDialog();
      const alternative = { ...loincHit, concept_id: 555, concept_name: "Early lexical candidate", concept_code: "555" };
      const semantic = { ...loincHit, concept_id: 556, concept_name: "Later semantic candidate", concept_code: "556", vector_distance: 0.125 };
      const activity = [
        { stage: "candidates", strategy: "umls", candidates: [loincHit] },
        { stage: "candidates", strategy: "lexical", candidates: [alternative] },
      ];
      mockPost.mockResolvedValue({ data: suggestRun({ state: "running", activity }) });
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string) => url.includes("/suggest-runs/")
        ? Promise.resolve({ data: suggestRun({ activity: [...activity,
          { stage: "candidates", strategy: "semantic", candidates: [semantic] },
          { stage: "result", suggested: loincHit, candidates: [loincHit, alternative, semantic] },
        ] }) }) : originalGet(url));
      const dialog = within(screen.getByRole("dialog"));
      const suggest = dialog.getByRole("button", { name: "Suggest", exact: true });
      fireEvent.click(suggest);
      const section = await dialog.findByRole("region", { name: "Individual suggestion candidates" });
      expect(suggest.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(section.compareDocumentPosition(dialog.getByLabelText("Search destination concepts")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(within(section).getByRole("status")).toHaveTextContent("Searching");
      fireEvent.click(within(section).getByRole("button", { name: /Early lexical candidate/ }));
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(555);
      expect(mockPatch).not.toHaveBeenCalled();
      expect(await within(section).findByText("Distance 0.1250", {}, { timeout: 3000 })).toBeInTheDocument();
      expect(within(section).getByText(`Winner: ${loincHit.concept_name}`)).toBeInTheDocument();
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(555);
      expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.objectContaining({ async: true }));
      fireEvent.click(within(section).getByRole("button", { name: /Later semantic candidate/ }));
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(556);
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({ destination_concept_id: 556 })));
    });

    it("displays inline individual results and fills the winner when nothing was selected", async () => {
      await openDialog();
      mockPost.mockResolvedValue({ data: suggestRun({ activity: [
        { stage: "candidates", strategy: "umls", candidates: [loincHit] },
        { stage: "result", suggested: loincHit, candidates: [loincHit] },
      ] }) });
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(loincHit.concept_id));
      const section = screen.getByRole("region", { name: "Individual suggestion candidates" });
      expect(within(section).getByText("Winner")).toBeInTheDocument();
      expect(mockPatch).not.toHaveBeenCalled();
    });

    it("hides old individual candidates and ignores the winner after the source changes", async () => {
      await openDialog();
      mockPost.mockResolvedValue({ data: suggestRun({ state: "running", activity: [
        { stage: "candidates", strategy: "umls", candidates: [loincHit] },
      ] }) });
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest", exact: true }));
      await screen.findByRole("region", { name: "Individual suggestion candidates" });
      fireEvent.change(screen.getByLabelText("Source Code Value"), { target: { value: "CHANGED" } });
      expect(screen.queryByRole("region", { name: "Individual suggestion candidates" })).not.toBeInTheDocument();
      expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(proposedRow.destination_concept_id);
    });

    it("highlights imported alternatives and saves the curator's selected destination", async () => {
      renderPage([{ ...proposedRow, destination_count: 3 }]);
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string) => url === "/v1/code-mappings/7/"
        ? Promise.resolve({ data: { destination_options: [
          { ...loincHit, selectable: true, selected: false, origins: ["HT-One"] },
          { ...loincHit, concept_id: 555, concept_code: "555", concept_name: "Other source destination", selectable: true, selected: false, origins: ["HT-One"] },
          { ...loincHit, concept_id: null, concept_code: "999", concept_name: "Concept not loaded", selectable: false, selected: false, origins: ["HT-One"] },
        ] } }) : originalGet(url));
      fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: "td" })).closest("tr")!);
      const choices = await screen.findByLabelText("Source data destinations (3)");
      expect(screen.getByText(/Multiple destinations are available/)).toBeInTheDocument();
      expect(screen.getByRole("option", { name: /Concept not loaded/ })).toBeDisabled();
      fireEvent.change(choices, { target: { value: String(loincHit.concept_id) } });
      expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(loincHit.concept_id);
      expect(screen.getByTestId("destination-concept-code")).toHaveValue(loincHit.concept_code);
      expect(screen.getByTestId("destination-concept-class")).toHaveValue(loincHit.concept_class_id);
      fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({
        destination_concept_id: loincHit.concept_id,
        destination_vocabulary_id: "LOINC",
        domain_id: "Measurement",
        omop_table: "measurement",
      })));
    });

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

    it("gives every control an accessible name", async () => {
      // The regression test for #840: the source code value sat in an
      // unlabelled input. On a screen this conceptually dense, a field whose
      // meaning has to be inferred is a defect, so none of them may be nameless.
      await openDialog();
      const dialog = screen.getByRole("dialog");
      const controls = Array.from(dialog.querySelectorAll("input, select, textarea"));
      expect(controls.length).toBeGreaterThan(10);
      const nameless = controls.filter((el) => !accessibleName(el));
      expect(nameless.map((el) => el.outerHTML)).toEqual([]);
    });

    it("renders explanatory tooltip content for every dialog control", async () => {
      await openDialog();
      const dialog = screen.getByRole("dialog");
      const controls = Array.from(dialog.querySelectorAll("input, select, textarea"));
      const helpButtons = within(dialog).getAllByRole("button", { name: "Help" });
      const tooltips = within(dialog).getAllByRole("tooltip");
      expect(helpButtons.length).toBe(controls.length);
      expect(tooltips).toHaveLength(controls.length);
      expect(tooltips.every((tip) => Boolean(tip.textContent?.trim()))).toBe(true);
      expect(helpButtons.every((button) => button.getAttribute("aria-describedby"))).toBe(true);
    });

    it("labels the source code value field, and never calls it a concept code", async () => {
      await openDialog();
      const input = screen.getByLabelText("Source Code Value") as HTMLInputElement;
      expect(input.value).toBe("M-PROTEIN, SERUM");
      expect(screen.queryByText("Source concept code")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Source Description")).toHaveValue("M-protein, serum");
    });

    it("shows a populated Source Concept ID in the dialog", async () => {
      renderPage([{
        ...proposedRow,
        source_vocabulary_id: "ICD10CM",
        source_code: "C90.20",
        source_code_description: "Extramedullary plasmacytoma not having achieved remission",
        source_concept_id: 45542660,
      }]);
      const cell = await screen.findByText("C90.20", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      expect(screen.getByLabelText("Source Description")).toHaveValue(
        "Extramedullary plasmacytoma not having achieved remission",
      );
      expect(screen.getByTestId("source-concept-id")).toHaveValue("45542660");
    });

    it("puts Domain first in the source block", async () => {
      await openDialog();
      const labels = Array.from(
        screen.getByTestId("source-fields").querySelectorAll("label"),
      ).map((l) => l.textContent);
      expect(labels).toEqual([
        "Domain",
        "Source Code System",
        "Source Code Value",
        "Source Description",
        "Source Concept ID",
        "Source code retirement",
      ]);
      expect((screen.getByLabelText("Domain") as HTMLSelectElement).value).toBe("Measurement");
    });

    it("offers the source code system as a select with a blank option", async () => {
      await openDialog();
      const select = screen.getByLabelText("Source Code System") as HTMLSelectElement;
      expect(select.tagName).toBe("SELECT");
      const values = Array.from(select.options).map((o) => o.value);
      expect(values[0]).toBe("");             // uncoded is a real answer, and first
      expect(values).toContain("LOINC");
      expect(values.some((v) => v.startsWith("HK-"))).toBe(false);
    });

    it("re-scopes the source code systems and the destination table when Domain changes", async () => {
      await openDialog();
      expect(screen.getByTestId("destination-table")).toHaveValue("measurement");

      fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "Condition" } });

      const values = Array.from(
        (screen.getByLabelText("Source Code System") as HTMLSelectElement).options,
      ).map((o) => o.value);
      expect(values).toContain("ICD10CM");
      expect(values).not.toContain("LOINC");   // a lab code system, not a condition one
      expect(values[0]).toBe("");
      // The consequence of the Domain choice is shown, not implied.
      expect(screen.getByTestId("destination-table")).toHaveValue("condition");
    });

    it("clears a source code system the new domain does not offer", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Source Code System"), { target: { value: "LOINC" } });
      fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "Drug" } });
      expect((screen.getByLabelText("Source Code System") as HTMLSelectElement).value).toBe("");
    });

    it("orders the destination fields the way a curator checks them", async () => {
      await openDialog();
      const labels = Array.from(
        screen.getByTestId("destination-fields").querySelectorAll("label"),
      ).map((l) => l.textContent);
      expect(labels).toEqual([
        "Destination Concept ID",
        "Destination Concept Name",
        "Destination Concept Code",
        "Destination Vocabulary ID",
        "Destination Concept Class",
        "Standard Concept",
        "Destination Status",
        "Destination Table",
      ]);
    });

    it("edits only the destination id; the rest follow from the concept", async () => {
      await openDialog();
      const readOnly = (label: string) =>
        (screen.getByLabelText(label) as HTMLInputElement).readOnly;
      expect(readOnly("Destination Concept ID")).toBe(false);
      // Name is derived too: the API has no write path for a concept name.
      expect(readOnly("Destination Concept Name")).toBe(true);
      expect(readOnly("Destination Concept Code")).toBe(true);
      expect(readOnly("Destination Vocabulary ID")).toBe(true);
      expect(readOnly("Destination Concept Class")).toBe(true);
      expect(readOnly("Standard Concept")).toBe(true);
      expect(readOnly("Destination Table")).toBe(true);
      expect((screen.getByLabelText("Source Concept ID") as HTMLInputElement).readOnly).toBe(true);
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

    it("resolves a hand-typed destination concept id on blur", async () => {
      await openDialog();
      const input = screen.getByLabelText("Destination Concept ID");
      mockGet.mockImplementationOnce(() => Promise.resolve({ data: loincHit }));
      fireEvent.change(input, { target: { value: "3046299" } });
      fireEvent.blur(input, { target: { value: "3046299" } });

      await waitFor(() => {
        expect(screen.getByTestId("destination-concept-code")).toHaveValue("33358-3");
      });
      expect(screen.getByTestId("destination-vocabulary-id")).toHaveValue("LOINC");
      expect(screen.getByTestId("destination-concept-class")).toHaveValue("Lab Test");
      expect(screen.getByTestId("standard-concept")).toHaveValue("S");
    });

    it("shows the concept class read-only, derived from the chosen concept", async () => {
      await openDialog();
      expect(screen.getByTestId("destination-concept-class")).toHaveValue("Lab Test");
      expect((screen.getByLabelText("Destination Concept Class") as HTMLInputElement).readOnly)
        .toBe(true);
    });

    it("shows Standard Concept as S for an Athena concept and blank for a mint", async () => {
      await openDialog();
      // The row's destination is an HK-Labs mint: not standard.
      expect(screen.getByTestId("standard-concept")).toHaveValue("");

      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      const hit = await screen.findByText("Protein.monoclonal [Mass/volume] in Serum");
      fireEvent.click(hit.closest("button")!);

      await waitFor(() => {
        expect(screen.getByTestId("standard-concept")).toHaveValue("S");
      });
    });

    it("identifies a retired non-standard destination without rewriting its class", async () => {
      renderPage([{
        ...proposedRow,
        destination_concept_class_id: "Undefined",
        destination_invalid_reason: "U",
      }]);
      await openDialog();

      expect(screen.getByTestId("destination-concept-class")).toHaveValue("Undefined");
      expect(screen.getByTestId("standard-concept")).toHaveValue("");
      expect(screen.getByTestId("destination-status"))
        .toHaveValue("Retired / invalid (reason U)");
      expect(screen.getByRole("button", { name: "Find replacement" })).toBeInTheDocument();
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
      expect(screen.getByTestId("destination-concept-class")).toHaveValue("Lab Test");
      expect(screen.getByTestId("destination-concept-code")).toHaveValue("33358-3");
    });

    it("shows measurement type and unit in destination search results", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      expect(await screen.findByText("Quantitative · Unit: mg/dL")).toBeInTheDocument();
    });

    it("selects a reviewed mint candidate as the mapping destination", async () => {
      await openDialog();
      mockPost.mockResolvedValue({ data: { candidates: [loincHit], review_token: "checked" } });
      fireEvent.click(screen.getByRole("button", { name: "Mint new concept" }));
      const mint = within(screen.getByRole("dialog", { name: "Mint new concept" }));
      fireEvent.change(mint.getByLabelText("Custom vocabulary group"), { target: { value: "HK-Labs" } });
      fireEvent.change(mint.getByLabelText("Concept code"), { target: { value: "custom-protein" } });
      fireEvent.click(mint.getByText("Check existing destinations"));
      fireEvent.click(await mint.findByRole("button", { name: /Protein.monoclonal/ }));
      expect(screen.queryByRole("dialog", { name: "Mint new concept" })).not.toBeInTheDocument();
      expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(3046299);
      expect(mockPost).toHaveBeenCalledTimes(1);
    });

    it("scopes the concept search to the destination vocabulary", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      await waitFor(() => {
        const call = mockGet.mock.calls.find((c) => c[0] === "/v1/concepts/search/");
        expect(call?.[1]?.params?.vocabulary_id).toBe("HK-Labs");
      });
    });

    it("lets the search scope widen so a mint can be re-pointed at a standard concept", async () => {
      await openDialog();
      fireEvent.change(screen.getByLabelText("Search vocabulary"), { target: { value: "LOINC" } });
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      await waitFor(() => {
        const calls = mockGet.mock.calls.filter((c) => c[0] === "/v1/concepts/search/");
        expect(calls[calls.length - 1][1].params.vocabulary_id).toBe("LOINC");
      });
    });

    it("shows import provenance so an SME knows what they are reviewing", async () => {
      await openDialog();
      expect(screen.getByText(/Proposed by import/)).toHaveTextContent("hk-labs");
      // The occurrence count moved out of the provenance line and onto its own
      // badge in #1080, alongside the new Seen column. The text is split across
      // elements, so match the badge that contains it.
      expect(
        screen.getByText((_content, el) => el?.textContent === "Seen 14 times"),
      ).toBeInTheDocument();
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
      expect(mockPatch.mock.calls[0][1]).toMatchObject({
        domain_id: "Measurement",
        omop_table: "measurement",
        source_vocabulary_id: "",
        source_code: "M-PROTEIN, SERUM",
      });
    });

    it("deletes a mis-keyed mapping", async () => {
      await openDialog();
      fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
      await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/v1/code-mappings/7/"));
    });
  });

  describe("review regressions", () => {
    const openDialog = async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      return await screen.findByText("Edit Mapping");
    };

    it("lets a multi-word concept search be typed", async () => {
      // Trimming the controlled value before setState fed the same string back,
      // so React re-rendered without the space and a space could never be typed.
      await openDialog();
      const search = screen.getByLabelText(/Search destination concepts/i);
      fireEvent.change(search, { target: { value: "serum " } });
      expect(search).toHaveValue("serum ");
      fireEvent.change(search, { target: { value: "serum m-protein" } });
      expect(search).toHaveValue("serum m-protein");
    });

    it("does not offer to edit the destination concept name", async () => {
      // The API has no write path for it, so an editable box accepted a rename
      // and let the old value come back on refetch with no error.
      await openDialog();
      const name = screen.getByLabelText("Destination Concept Name") as HTMLInputElement;
      expect(name.readOnly).toBe(true);
    });

    it("shows a source system the domain's catalogue does not list", async () => {
      // An ICD-10-CM-coded row minted into HK-Labs has domain Measurement,
      // whose catalogue has no ICD10CM. Falling back to the first option
      // rendered it as "uncoded" — the defect this page exists to remove.
      renderPage([{ ...proposedRow, source_vocabulary_id: "ICD10CM", domain_id: "Measurement" }]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");

      const select = screen.getByLabelText("Source Code System") as HTMLSelectElement;
      expect(select.value).toBe("ICD10CM");
      expect(Array.from(select.options).map((o) => o.value)).toContain("ICD10CM");
    });
  });

  describe("Unmapped queue ordering", () => {
    const row = (over: Partial<typeof proposedRow>) => ({ ...proposedRow, ...over });

    it("puts the busiest code first, whoever raised it", async () => {
      // #1080 replaced the provenance-then-author grouping with occurrence
      // order across every section: the code seen 900 times is worth more of a
      // curator's time than one seen once, whoever proposed it.
      renderPage([
        row({ mapping_id: 1, source_code: "HUMAN-ZOE", origin: "curator", created_by: "zoe@example.com", occurrence_count: 900 }),
        row({ mapping_id: 2, source_code: "HUMAN-ADA", origin: "curator", created_by: "ada@example.com", occurrence_count: 1 }),
        row({ mapping_id: 3, source_code: "MACHINE", origin: "import", created_by: "", occurrence_count: 5 }),
      ]);
      await screen.findByText("MACHINE", { selector: "td" });

      // The data rows carry role="button" (whole-row click), which overrides
      // their implicit "row" role — so query the code cells directly. Testing
      // Library returns them in DOM order.
      const codes = screen
        .getAllByText(/^(MACHINE|HUMAN-ADA|HUMAN-ZOE)$/, { selector: "td" })
        .map((cell) => cell.textContent);
      // Zoe's 900 first, then the import's 5, then Ada's 1 -- who raised it no
      // longer changes the order.
      expect(codes).toEqual(["HUMAN-ZOE", "MACHINE", "HUMAN-ADA"]);
    });

    it("names the creating curator instead of an import system", async () => {
      renderPage([row({ origin: "curator", created_by: "ada@example.com", origin_system: "" })]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(screen.getByText(/Created by ada@example.com/)).toBeInTheDocument();
      expect(screen.queryByText(/Proposed by import/)).not.toBeInTheDocument();
    });

    it("locks Status to Proposed on a new mapping", async () => {
      // Approval is the only transition that rewrites patient data, and the
      // server enforces proposed-on-create; offering Approved here would
      // promise a one-step create-and-approve the API no longer honours.
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /New Mapping/ }));
      await screen.findByText("New Mapping", { selector: "h2" });

      const statusSelect = screen.getByLabelText("Status") as HTMLSelectElement;
      expect(statusSelect.value).toBe("proposed");
      expect(statusSelect.disabled).toBe(true);
    });
  });

  describe("Sign-off on the provenance line", () => {
    /** Open the dialog for a row that lives under the collapsed Mapped section. */
    const openApproved = async (code: string) => {
      const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
      fireEvent.click(tabs.getByRole("tab", { name: /ICD-10/ }));
      fireEvent.click(await screen.findByText(/^Mapped/));
      const cell = await screen.findByText(code, { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
    };

    it("names who approved the mapping and when, beside who created it", async () => {
      // The reviewer is deliberately not the author: approval is a separate
      // act by a separate person, and updated_by cannot stand in for it
      // because the next edit overwrites it.
      renderPage();
      await openApproved("C90.00");
      // The date renders in the viewer's own timezone, so derive the expected
      // string rather than hardcoding a UTC slice — a runner west of UTC would
      // otherwise see the previous day and fail.
      const when = new Date("2026-08-31T09:14:00Z").toLocaleDateString();
      expect(
        screen.getByText(
          `Created by zoe@example.com · approved by ada@example.com on ${when}`,
        ),
      ).toBeInTheDocument();
    });

    it("shows both halves when an import raised it and a human signed it off", async () => {
      renderPage([{
        ...approvedRow,
        source_code: "IMPORTED",
        origin: "import",
        origin_system: "fhir-sync",
        created_by: "",
      }]);
      await openApproved("IMPORTED");
      const when = new Date("2026-08-31T09:14:00Z").toLocaleDateString();
      expect(
        screen.getByText(
          `Proposed by import (fhir-sync) · approved by ada@example.com on ${when}`,
        ),
      ).toBeInTheDocument();
    });

    it("says nothing about approval on a row that is no longer approved", async () => {
      // Un-approving is one click in the list. A stale stamp had the dialog
      // assert "approved by ada@" over a proposed row.
      renderPage([{
        ...approvedRow, source_code: "UNAPPROVED", status: "proposed" as const,
      }]);
      const cell = await screen.findByText("UNAPPROVED", { selector: "td" });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(screen.queryByText(/approved by/)).not.toBeInTheDocument();
    });

    it("does not render an empty author when created_by is blank", async () => {
      // created_by is SET_NULL, so a deleted author serializes blank.
      renderPage([{
        ...approvedRow, source_code: "NOAUTHOR", origin: "curator", created_by: "",
      }]);
      await openApproved("NOAUTHOR");
      expect(screen.queryByText(/Created by\s*·/)).not.toBeInTheDocument();
      expect(screen.getByText(/approved by ada@example.com/)).toBeInTheDocument();
    });

    it("says nothing about approval on a row approved before reviewers were recorded", async () => {
      // Naming whoever last edited such a row would assert something we do not
      // know — the very confusion updated_by created.
      renderPage([{
        ...approvedRow, source_code: "LEGACY", reviewer: "", reviewed_at: null,
      }]);
      await openApproved("LEGACY");
      expect(screen.getByText(/Created by zoe@example.com/)).toBeInTheDocument();
      expect(screen.queryByText(/approved by/)).not.toBeInTheDocument();
    });
  });

  describe("Suggest", () => {
    it("keeps Suggest available after switching source vocabularies", async () => {
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      expect(screen.getByRole("button", { name: /Suggest/ })).toBeEnabled();

      const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
      fireEvent.click(tabs.getByRole("tab", { name: /ICD-10/ }));
      const button = screen.getByRole("button", { name: /Suggest/ });
      expect(button).toBeEnabled();
      expect(button).toHaveAttribute("title", expect.stringContaining("source codes"));
    });

    it("keeps Suggest available when the queue is empty", async () => {
      renderPage([]);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /Suggest/ })).toBeEnabled());
    });

    it("includes all Seen counts without a threshold control", async () => {
      mockPost.mockResolvedValue({ data: suggestRun({ updated: 3, total: 5, ranked: 2 }) });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      expect(screen.queryByLabelText(/seen at least/i)).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      expect(mockPost.mock.calls[0][0]).toBe("/v1/code-mappings/suggest/");
      expect(mockPost.mock.calls[0][1]).toMatchObject({
        source_vocabulary_id: "",
      });
      expect(mockPost.mock.calls[0][1]).not.toHaveProperty("min_occurrences");
    });

    it("reports what it proposed", async () => {
      mockPost.mockResolvedValue({
        data: suggestRun({ total: 5, done: 5, destinations: 3, landed_in: { LOINC: 3 } }),
      });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));

      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress")).toHaveTextContent(
          "Done — wrote 3 new destination(s) across 5 code(s).",
        ));
      expect(screen.getByTestId("suggest-progress")).toHaveTextContent("5/5");
      const logLink = within(screen.getByTestId("suggest-progress")).getByRole("link", { name: "View run log" });
      expect(logLink).toHaveAttribute("href", expect.stringContaining("/code-mappings/suggest-runs/"));
      expect(logLink).toHaveAttribute("target", "_blank");
    });

    it("says so when nothing on the tab is awaiting a suggestion", async () => {
      // Importer rows are deliberately left alone, so an empty result is a
      // normal state and not a failure.
      mockPost.mockResolvedValue({ data: suggestRun({ updated: 0, total: 0, ranked: 0 }) });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("nothing on this tab was awaiting a suggestion"));
    });

    it("puts Suggest first directly below the tabs, followed by the enabled strategies", async () => {
      mockPost.mockResolvedValue({ data: suggestRun() });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      const toolbar = screen.getByRole("group", { name: "Suggest controls" });
      expect(screen.getByRole("tablist").nextElementSibling).toBe(toolbar);
      const controls = toolbar.querySelectorAll("button, input");
      expect(controls[0]).toHaveTextContent("Suggest");
      for (const [index, name] of ["UMLS", "Lexical", "Semantic retrieval"].entries()) {
        const checkbox = within(toolbar).getByRole("checkbox", { name });
        expect(controls[index + 2]).toBe(checkbox);
        expect(checkbox).toBeChecked();
      }
      const batchSize = within(toolbar).getByRole("spinbutton", { name: "Number of suggestions" });
      expect(controls[1]).toBe(batchSize);
      expect(batchSize).toHaveValue(100);
      expect(batchSize.nextElementSibling).toHaveTextContent("Using");
      fireEvent.change(batchSize, { target: { value: "25" } });
      fireEvent.click(within(toolbar).getByRole("button", { name: "Suggest" }));
      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      expect(mockPost.mock.calls[0][1]).toMatchObject({
        limit: 25, strategies: ["umls", "lexical", "semantic"],
      });
      expect(mockPost.mock.calls[0][1]).not.toHaveProperty("lexical_limit");
      expect(mockPost.mock.calls[0][1]).not.toHaveProperty("min_occurrences");
    });

    it("can suggest with semantic retrieval alone and requires at least one retriever", async () => {
      mockPost.mockResolvedValue({ data: suggestRun() });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      const toolbar = screen.getByRole("group", { name: "Suggest controls" });
      for (const name of ["UMLS", "Lexical"]) {
        fireEvent.click(within(toolbar).getByRole("checkbox", { name }));
      }
      const button = within(toolbar).getByRole("button", { name: "Suggest" });
      const semantic = within(toolbar).getByRole("checkbox", { name: "Semantic retrieval" });
      expect(within(toolbar).queryByRole("checkbox", { name: /Vector/ })).not.toBeInTheDocument();
      expect(button).toBeEnabled();
      fireEvent.click(semantic);
      expect(button).toBeDisabled();
      fireEvent.click(semantic);
      fireEvent.click(button);
      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      expect(mockPost.mock.calls[0][1]).toMatchObject({ strategies: ["semantic"] });
    });

    it("uses the active server batch cap as the default", async () => {
      const page = renderPage();
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string) => url === "/v1/code-mappings/reference/"
        ? Promise.resolve({ data: { ...reference, suggest_max_per_run: 3 } }) : originalGet(url));
      // Refresh the reference by switching away and remounting with the capped response.
      page.unmount();
      render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
      await waitFor(() => expect(screen.getByLabelText("Number of suggestions")).toHaveValue(3));
      fireEvent.change(screen.getByLabelText("Number of suggestions"), { target: { value: "4" } });
      expect(screen.getByRole("button", { name: "Suggest" })).toBeDisabled();
    });

    it("shows incremental progress while the run is still working", async () => {
      // The run is queued and a code costs seconds, so a curator has to be able
      // to tell a working run from a stuck one.
      mockPost.mockResolvedValue({
        data: { ...suggestRun({ state: "running", total: 4, retrieved: 1, done: 0 }), activity: [{ stage: "candidates", mapping_id: 7, source_code: "LIVE", strategy: "umls", candidates: [{ concept_id: 1, concept_name: "UMLS candidate", concept_code: "A", vocabulary_id: "SNOMED" }] }] },
      });
      mockGet.mockImplementation((url: string) => {
        if (url.startsWith("/v1/code-mappings/suggest-runs/")) {
          return Promise.resolve({
            data: { ...suggestRun({ state: "success", total: 4, done: 4, destinations: 4 }), activity: [{ stage: "candidates", mapping_id: 7, source_code: "LIVE", strategy: "semantic", candidates: [{ concept_id: 2, concept_name: "Semantic candidate", concept_code: "B", vocabulary_id: "SNOMED", vector_distance: 0.25 }] }] },
          });
        }
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: [proposedRow] });
        if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
        return Promise.resolve({ data: {} });
      });
      render(
        <MemoryRouter>
          <CodeMappingPage />
        </MemoryRouter>,
      );
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));

      // The in-flight phase names what it is doing, against a known denominator.
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("Searching for candidates… 1 of 4"));
      expect(screen.getByText(/UMLS candidate/)).toBeInTheDocument();
      // Then it polls to completion.
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("Done — wrote 4 new destination(s) across 4 code(s)."),
        { timeout: 4000 });
      expect(screen.getByText(/Semantic candidate/)).toBeInTheDocument();
      expect(screen.getByText("Distance 0.2500")).toBeInTheDocument();
      expect(mockGet).toHaveBeenCalledWith(expect.stringContaining("/suggest-runs/"), { params: { include_activity: "1" } });
      expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest/", expect.objectContaining({ include_activity: true }));
    });

    it("says how many remain so the curator knows to run it again", async () => {
      // A run is capped well below a tab's backlog, so finishing is not the
      // same as being done.
      mockPost.mockResolvedValue({
        data: suggestRun({ total: 5, done: 5, destinations: 3, remaining: 28 }),
      });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("28 still awaiting a suggestion — run Suggest again."));
    });

    it("keeps polling through a transient failure", async () => {
      // The work is on a worker and carries on; treating a dropped GET as a
      // failed run would show an error over a run that succeeded.
      mockPost.mockResolvedValue({
        data: suggestRun({ state: "running", total: 2, retrieved: 1 }),
      });
      let polls = 0;
      mockGet.mockImplementation((url: string) => {
        if (url.startsWith("/v1/code-mappings/suggest-runs/")) {
          polls += 1;
          if (polls === 1) return Promise.reject(new Error("network blip"));
          return Promise.resolve({
            data: suggestRun({ state: "success", total: 2, done: 2, destinations: 2 }),
          });
        }
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: [proposedRow] });
        if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
        return Promise.resolve({ data: {} });
      });
      render(
        <MemoryRouter>
          <CodeMappingPage />
        </MemoryRouter>,
      );
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("wrote 2 new destination(s)"),
        { timeout: 6000 });
      expect(screen.queryByText("Failed to suggest mappings.")).not.toBeInTheDocument();
    });

    it("surfaces a failed run rather than leaving the bar stuck", async () => {
      mockPost.mockResolvedValue({
        data: suggestRun({ state: "failure", error: "retrieval exploded" }),
      });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress")).toHaveTextContent("retrieval exploded"));
    });

    it("shows a proposal with no destination yet in its domain's tab", async () => {
      // These have a blank destination vocabulary, so matching the tab on that
      // alone put them in no tab at all — and they are the rows that most need
      // a curator.
      renderPage([{
        ...proposedRow,
        source_code: "99999-9",
        destination_vocabulary_id: "",
        destination_concept_id: null as unknown as number,
        domain_id: "Measurement",
      }]);
      expect(await screen.findByText("99999-9", { selector: "td" })).toBeInTheDocument();
    });
  });
});

describe("mapping dialog request isolation", () => {
  const first = { ...proposedRow, source_code: "Z94.81", source_code_description: "Bone marrow transplant status" };
  const second = { ...proposedRow, mapping_id: 99, source_code: "Z12.11", source_code_description: "Screening encounter" };

  it("clears a previous code's no-match message when opening another code", async () => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    mockPost.mockResolvedValueOnce({ data: suggestRun({ activity: [{ stage: "result", suggested: null, note: "No suitable concept: Z94.81" }] }) });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    expect(await screen.findByText("No suitable concept: Z94.81")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByText("Z12.11"));
    expect(within(screen.getByRole("dialog")).queryByText("No suitable concept: Z94.81")).not.toBeInTheDocument();
  });

  it("shows the suggestion method only in the dialog and clears it for another code", async () => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    mockPost.mockResolvedValueOnce({ data: suggestRun({ activity: [{ stage: "result", suggested: loincHit, strategy_used: "lexical" }] }) });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    const message = await screen.findByText("Winner filled in. You can choose another candidate before saving.");
    expect(screen.getByRole("dialog")).toContainElement(message);
    expect(screen.getAllByText("Winner filled in. You can choose another candidate before saving.")).toHaveLength(1);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Winner filled in. You can choose another candidate before saving.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Z12.11"));
    expect(screen.queryByText("Winner filled in. You can choose another candidate before saving.")).not.toBeInTheDocument();
  });

  it.each([false, true])("ignores a late suggestion for a closed dialog (destination=%s)", async (found) => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    let resolve!: (value: unknown) => void;
    mockPost.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByText("Z12.11"));
    resolve({ data: { suggested: found ? loincHit : null, note: "No suitable concept: Z94.81" } });
    await waitFor(() => expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" })).toBeEnabled());
    expect(screen.queryByText("No suitable concept: Z94.81")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Z12.11")).toBeInTheDocument();
    expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(second.destination_concept_id);
  });
});

describe("server mapping pages", () => {
  it("requests the next page and sorts the full section on the server", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/v1/code-mappings/") {
        const page = Number(config?.params?.page_0 || 1);
        return Promise.resolve({ data: {
          results: [{ ...proposedRow, source_code: page === 1 ? "FIRST PAGE" : "SECOND PAGE" }],
          duplicates: [], selected_source: "",
          tabs: [{ vocabulary_id: "", label: "Uncoded", is_standard: false, proposed: 101, approved: 0, athena: 0 }],
          pages: { Unmapped: { page, page_size: 100, total: 101 }, Mapped: { page: 1, page_size: 100, total: 0 }, "Athena Mapped": { page: 1, page_size: 100, total: 0 } },
          rejected_count: 0,
        } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByText("FIRST PAGE")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2 · 101 mappings")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("SECOND PAGE")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Sort Unmapped by Seen"));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ page_0: 1, order_0: "occurrence_count" }) }));
  });
});

describe("Overall review counters and refresh", () => {
  const metrics = { approved: 0, accepted: 0, rejected: 0, overridden: 0, reviewed: 0, precision: null, recall: null, f1: null, model_version: "v0.2" };
  beforeEach(() => { mockGet.mockReset(); mockPatch.mockReset(); });

  it("shows overall reviews across models regardless of the selected vocabulary", async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes("accuracy") ? {
      overall: { ...metrics, review_totals: { approved: 6, rejected: 2, overridden: 3 } },
      by_source_vocabulary: { "": { ...metrics, review_totals: { approved: 99, rejected: 99, overridden: 99 } } },
    } : url.includes("reference") ? reference : [proposedRow] }));
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("6");
    expect(within(section).getByText("Rejected").parentElement).toHaveTextContent("2");
    expect(within(section).getByText("Other destination").parentElement).toHaveTextContent("3");
    expect(within(section).queryByText(/^Metrics:/)).not.toBeInTheDocument();
    expect(screen.queryByText("Review counts: all models")).not.toBeInTheDocument();
    const controls = screen.getByRole("group", { name: "Suggest controls" });
    const semantic = within(controls).getByRole("checkbox", { name: "Semantic retrieval" });
    const replace = within(controls).getByRole("checkbox", { name: "Replace Current Suggestions" });
    expect(semantic.closest("span")?.nextElementSibling).toBe(replace.closest("label"));
    // Approved leads the strip now that no box names a model version.
    expect(section.firstElementChild).toHaveTextContent("Approved");
  });

  it("scores precision, recall and F1 over every model rather than the newest reviewed one", async () => {
    // v0.3 alone would read 100%; all models together are 2 of 4 accepted.
    const latestReviewed = { ...metrics, model_version: "v0.3", reviewed: 1, approved: 1, precision: 1, recall: 1, f1: 1 };
    const allModels = { ...metrics, model_versions: 2, suggestions: 4, reviewed: 4, approved: 2, rejected: 1, overridden: 1, precision: 0.5, recall: 2 / 3, f1: 4 / 7 };
    mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes("accuracy") ? {
      overall: url.includes("dashboard") ? allModels : { ...metrics, all_models: allModels },
      models: [latestReviewed, { ...metrics, model_version: "v0.2" }],
      by_source_vocabulary: { "": { ...latestReviewed, all_models: { ...latestReviewed, model_versions: 1 } } },
    } : url.includes("reference") ? reference : [proposedRow] }));
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(await within(section).findByText("Precision")).toBeInTheDocument();
    expect(within(section).getByText("Precision").parentElement).toHaveTextContent("50.0%");
    expect(within(section).getByText("Recall").parentElement).toHaveTextContent("66.7%");
    expect(within(section).getByText("F1").parentElement).toHaveTextContent("57.1%");
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("2");
    expect(within(section).getByText("Rejected").parentElement).toHaveTextContent("1");
    expect(within(section).getByText("Other destination").parentElement).toHaveTextContent("1");
    expect(within(section).queryByText(/model reviews/)).not.toBeInTheDocument();
    const labels = ["Approved", "Rejected", "Other destination", "Precision", "Recall", "F1"];
    const displayed = labels.map(label => within(section).getByText(label).parentElement?.lastElementChild?.textContent);
    fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
    expect(labels.map(label => within(section).getByText(label).parentElement?.lastElementChild?.textContent)).toEqual(displayed);
    render(<MemoryRouter><CodeMappingAccuracyPage /></MemoryRouter>);
    const historyRow = (await screen.findByText("All models (2)")).closest("tr")!;
    expect(within(historyRow).getAllByRole("cell").slice(2).map(cell => cell.textContent)).toEqual(displayed);
  });

  it("shows dashes rather than one model's score when the API predates all_models", async () => {
    // A web instance mid-roll answers without the key. Counts still span
    // versions, so a single version's score beside them would be #1154 again
    // with nothing left on the strip to explain it.
    const latestReviewed = { ...metrics, model_version: "v0.2", reviewed: 2, approved: 2, precision: 1, recall: 1, f1: 1 };
    mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes("accuracy") ? {
      overall: { ...metrics, latest_reviewed: latestReviewed, review_totals: { approved: 5, rejected: 1, overridden: 0 } },
      by_source_vocabulary: {},
    } : url.includes("reference") ? reference : [proposedRow] }));
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(await within(section).findByText("Precision")).toBeInTheDocument();
    expect(within(section).queryByText("100.0%")).not.toBeInTheDocument();
    expect(within(section).getAllByText("—")).toHaveLength(3);
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("5");
  });

  it("updates confirmed reviews and counters without waiting for the table reload", async () => {
    let loads = 0;
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        loads += 1;
        return loads === 1 ? Promise.resolve({ data: [proposedRow] }) : new Promise(() => {});
      }
      if (url.includes("reference")) return Promise.resolve({ data: reference });
      return Promise.resolve({ data: { overall: { ...metrics, review_totals: { approved: loads > 1 ? 1 : 0, rejected: 0, overridden: 0 } }, by_source_vocabulary: {} } });
    });
    mockPatch.mockResolvedValue({ data: { ...proposedRow, status: "approved" } });
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Approve M-PROTEIN, SERUM" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve M-PROTEIN, SERUM" })).not.toBeInTheDocument());
    const section = screen.getByRole("region", { name: "Suggestion accuracy" });
    await waitFor(() => expect(within(section).getByText("Approved").parentElement).toHaveTextContent("1"));
    expect(mockGet.mock.calls.filter(([url]) => url === "/v1/code-mappings/reference/")).toHaveLength(1);
  });

  it("shows overall reviews even when the selected vocabulary has no suggestions", async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({ data: url.includes("accuracy") ? {
      overall: { ...metrics, approved: 99, review_totals: { approved: 99, rejected: 0, overridden: 0 } },
      by_source_vocabulary: {},
    } : url.includes("reference") ? reference : [proposedRow] }));
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("99");
    // Older API responses still cannot supply cross-version scores.
    expect(within(section).getAllByText("—")).toHaveLength(3);
  });
});


describe("saved batch log discovery", () => {
  it("recovers the completed run link after leaving and reopening the mapping page", async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({ data:
      url.endsWith("suggest-runs/latest/") ? { run_id: "saved-run" }
        : url.includes("reference") ? reference : url.includes("accuracy") ? {} : [proposedRow],
    }));
    const page = render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "View latest batch run log" }))
      .toHaveAttribute("href", "/code-mappings/suggest-runs/saved-run");
    page.unmount();
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "View latest batch run log" }))
      .toHaveAttribute("href", "/code-mappings/suggest-runs/saved-run");
  });
});


describe("expanded ICD10 review feedback", () => {
  it.each(["approved", "rejected"])("updates the expanded queue and counts immediately after a confirmed %s response", async (status) => {
    const mapping = { ...proposedRow, source_vocabulary_id: "ICD10", source_code: "Z12.11", mapping_origin: "healthkey" };
    let loads = 0;
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        if (++loads > 1) return new Promise(() => {});
        return Promise.resolve({ data: {
          results: [mapping], duplicates: [], selected_source: "ICD10", rejected_count: 0,
          tabs: [{ vocabulary_id: "ICD10", label: "ICD10", is_standard: true, proposed: 1, approved: 0, athena: 0 }],
          pages: { Unmapped: { page: 1, page_size: 100, total: 1 }, Mapped: { page: 1, page_size: 100, total: 0 }, "Athena Mapped": { page: 1, page_size: 100, total: 0 } },
        } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    mockPatch.mockResolvedValue({ data: { ...mapping, status } });
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByText("Z12.11", { selector: "td" }));
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: status } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Unmapped (0)" })).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Unmapped mappings" })).queryByText("Z12.11")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: `Mapped (${status === "approved" ? 1 : 0})` })).toBeInTheDocument();
  });
});
