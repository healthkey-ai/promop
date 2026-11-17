import { cleanup, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import CodeMappingPage from "./CodeMappingPage";
import CodeMappingAccuracyPage from "./CodeMappingAccuracyPage";
import type { SourceSystemContext } from "./SourceEvidencePanel";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDelete = vi.fn();

// The page is exercised as an analyst: a mapping writer who cannot approve (#2416).
const ANALYST_USER = { id: 7, org_accesses: [{ org_slug: "acme", org_name: "Acme", role: "analyst", expires_at: null }] };
const mockUseAuth = vi.hoisted(() => vi.fn((): { currentUser: unknown; loading?: boolean } => ({ currentUser: null })));
mockUseAuth.mockReturnValue({ currentUser: ANALYST_USER });
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));
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
  release_commit: "8798ec24f4cc16e47f8c0ceec69d38d7e5858b17",
  source_catalog_vocabularies: ["NCIt", "MeSH"],
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
  // The API supplies the full tab catalog. ICD10CM and ICD10 are separate tabs.
  source_vocabulary_tabs: [
    { vocabulary_id: "", label: "Uncoded", is_standard: false },
    { vocabulary_id: "ICD10CM", label: "ICD-10-CM", is_standard: false },
    { vocabulary_id: "ICD10", label: "ICD-10 (WHO)", is_standard: false },
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
  source_evidence?: Record<string, unknown>;
  source_system_context?: SourceSystemContext;
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
    if (url.endsWith("/canonical-unit/")) return Promise.resolve({ data: { unit: "", revision: 0, available_units: ["mg/dL", "g/L"], example_units: ["mg/dL"], can_edit: true } });
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: [...rows] });
    if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
    const detail = url.match(/^\/v1\/code-mappings\/(\d+)\/$/);
    if (detail) {
      const row = rows.find((candidate) => candidate.mapping_id === Number(detail[1]));
      return Promise.resolve({ data: {
        ...row,
        destination_options: [],
        source_evidence: row?.source_evidence || {
          organization: null,
          occurrence_count: row?.occurrence_count || 0,
          group_occurrence_count: null,
          metadata: {},
          units: [],
        },
      } });
    }
    if (url === "/v1/concepts/search/") {
      return Promise.resolve({ data: { results: [loincHit] } });
    }
    return Promise.resolve({ data: {} });
  });
  return render(
    <MemoryRouter initialEntries={["/code-mappings?direction=forward"]}>
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

// Columns are asserted by name rather than by position: #1443 moved Provenance
// and every index-based expectation broke, which says nothing about whether the
// right value is in the right column.
function columnIndex(table: HTMLElement, name: string) {
  const headers = within(table).getAllByRole("columnheader");
  const index = headers.findIndex(
    // A header that also holds a filter checkbox names itself.
    (header) => (header.getAttribute("aria-label") ?? header.textContent?.replace(/[↕↑↓]/g, "").trim()) === name,
  );
  expect(index, `no "${name}" column`).toBeGreaterThanOrEqual(0);
  return index;
}

function cellUnder(row: HTMLElement, name: string) {
  const table = row.closest("table")!;
  return within(row).getAllByRole("cell")[columnIndex(table, name)];
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

  it("shows the deployed commit beside the Code Mapping title", async () => {
    renderPage([proposedRow]);
    const marker = await screen.findByLabelText(
      `Deployed release ${reference.release_commit}`,
    );
    expect(marker).toHaveTextContent("release 8798ec24");
    expect(marker).toHaveAttribute(
      "title",
      `Deployed commit ${reference.release_commit}`,
    );
  });

  it("flags a source code whose units imply several LOINC axis sets", async () => {
    renderPage([
      { ...proposedRow, axis_variant_count: 2 },
      { ...proposedRow, mapping_id: 9, source_code: "GLUCOSE", source_code_description: "Glucose", axis_variant_count: 0 },
    ]);
    const flag = await screen.findByText("2 axis values");
    expect(flag).toHaveClass("text-blue-600");
    expect(flag).toHaveAttribute("title", expect.stringMatching(/imply 2 different LOINC axis sets/));
    // On the code's own line, right after the code.
    expect(flag.previousElementSibling).toHaveTextContent(/^M-PROTEIN, SERUM$/);
    expect(flag.previousElementSibling).toHaveAttribute("data-testid", "source-code");
    const plain = screen.getByText("GLUCOSE", { selector: '[data-testid=source-code]' });
    expect(within(plain.parentElement!).queryByText(/axis values/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/axis values/)).toHaveLength(1);
  });

  it("lets a curator choose a destination inline without opening the edit dialog", async () => {
    renderPage([proposedRow]);
    const previousGet = mockGet.getMockImplementation()!;
    mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/"
      ? Promise.resolve({ data: proposedRow }) : previousGet(url, ...rest));
    mockPatch.mockResolvedValue({ data: { ...proposedRow, destination_concept_id: loincHit.concept_id, origin_system: "curator" } });
    const trigger = await screen.findByRole("button", { name: "Choose destination for M-PROTEIN, SERUM" });
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    const input = await screen.findByRole("combobox", { name: "Search destination concepts inline" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "monoclonal" } });
    fireEvent.click(await screen.findByRole("option", { name: /Protein.monoclonal/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save choice" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", {
      destination_concept_id: loincHit.concept_id, status: "proposed",
    }));
    await waitFor(() => expect(screen.queryByRole("combobox", { name: "Search destination concepts inline" })).not.toBeInTheDocument());
    expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/7/lock/");
    expect(mockDelete).toHaveBeenCalledWith("/v1/code-mappings/7/lock/");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("switches between source-first and concept-first curation", async () => {
    renderPage([proposedRow]);
    await screen.findByRole("button", { name: "Concept → Source Code" });
    const previousGet = mockGet.getMockImplementation()!;
    mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/concept-to-code/"
      ? Promise.resolve({ data: { results: [], total: 0, page: 1, page_size: 50 } }) : previousGet(url, ...rest));
    fireEvent.click(screen.getByRole("button", { name: "Concept → Source Code" }));
    expect(await screen.findByRole("region", { name: "Concept to source code" })).toBeInTheDocument();
    await screen.findByText(/No matching standard concepts/);
    fireEvent.click(screen.getByRole("button", { name: "Source Code → Concept" }));
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
  });

  it("opens the concept-first tab on the domain the account chose, once the account is known (#2656)", async () => {
    localStorage.setItem(`promop-concept-to-code:${ANALYST_USER.id}`, JSON.stringify({ domain: "wearables" }));
    const conceptCalls = () => mockGet.mock.calls.filter(([url]) => url === "/v1/concept-to-code/");
    try {
      mockUseAuth.mockReturnValue({ currentUser: null, loading: true });
      const view = renderPage([proposedRow]);
      const previousGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/concept-to-code/"
        ? Promise.resolve({ data: { results: [], total: 0, page: 1, page_size: 50 } }) : previousGet(url, ...rest));
      fireEvent.click(await screen.findByRole("button", { name: "Concept → Source Code" }));
      // No account yet: no tab, so no unfiltered list request and nothing to save a choice under.
      expect(screen.queryByRole("region", { name: "Concept to source code" })).not.toBeInTheDocument();
      expect(conceptCalls()).toHaveLength(0);

      mockUseAuth.mockReturnValue({ currentUser: ANALYST_USER, loading: false });
      view.rerender(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
      expect(await screen.findByRole("region", { name: "Concept to source code" })).toBeInTheDocument();
      expect(screen.getByLabelText("Concept domain")).toHaveValue("wearables");
      await waitFor(() => expect(conceptCalls().length).toBeGreaterThan(0));
      // The first list request already asks for the saved domain.
      expect((conceptCalls()[0][1] as { params: { domain: string } }).params.domain).toBe("wearables");
    } finally {
      mockUseAuth.mockReturnValue({ currentUser: ANALYST_USER });
      localStorage.removeItem(`promop-concept-to-code:${ANALYST_USER.id}`);
    }
  });

  describe("duplicate source-code errors", () => {
    const proposed = { ...proposedRow, mapping_id: 101, source_vocabulary_id: "ICD10", source_code: "A02.0" };
    // Same vocabulary as `proposed`: a duplicate is one code twice in one vocabulary.
    const mapped = { ...approvedRow, mapping_id: 102, source_vocabulary_id: "ICD10", source_code: "A02.0" };
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
      fireEvent.click(within(alert).getByRole("link", { name: /Approved.*#103/ }));
      expect(screen.getByRole("textbox", { name: "Search mappings" })).toHaveValue("");
      await waitFor(() => expect(document.getElementById("code-mapping-103")).toHaveFocus());
      expect(scroll).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });

      fireEvent.click(within(alert).getByRole("link", { name: /— Approved.*#102/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-102")).toHaveFocus());
      fireEvent.click(within(alert).getByRole("link", { name: /Proposed.*#101/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-101")).toHaveFocus());

      fireEvent.click(screen.getByRole("button", { name: /Approved\(2\)/ }));
      fireEvent.click(within(alert).getByRole("link", { name: /Approved.*#103/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-103")).toHaveFocus());
      scroll.mockRestore();
    });

    it("includes hidden rejected duplicates and reveals them on navigation", async () => {
      renderPage([proposed, { ...mapped, status: "rejected" }]);
      const alert = await screen.findByRole("alert");
      expect(document.getElementById("code-mapping-102")).toBeInTheDocument();
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

    it("does not flag one code in different vocabularies sharing the Wearables tab", async () => {
      const wearable = (mapping_id: number, source_vocabulary_id: string) =>
        ({ ...proposed, mapping_id, source_vocabulary_id, source_code: "heart_rate" });
      renderPage([wearable(201, "Apple"), wearable(202, "Garmin"), wearable(203, "OpenWearables")]);
      await screen.findByRole("tab", { name: /Overall/ });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("still flags a code repeated within one vocabulary on the Wearables tab", async () => {
      const wearable = (mapping_id: number, source_vocabulary_id: string, source_code = "heart_rate") =>
        ({ ...proposed, mapping_id, source_vocabulary_id, source_code });
      renderPage([wearable(201, "Apple"), wearable(202, "Garmin"), wearable(204, "Garmin", " HEART_RATE ")]);
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("1 duplicate source code on this tab");
      expect(alert).toHaveTextContent("Garmin: HEART_RATE");
      expect(within(alert).getAllByRole("link")).toHaveLength(2);
    });

    it("keeps real duplicate groups separate on Overall and scopes other tabs", async () => {
      renderPage([proposed, mapped, { ...proposed, mapping_id: 104, source_vocabulary_id: "LOINC" }]);
      await screen.findByRole("alert");
      fireEvent.click(screen.getByRole("tab", { name: /LOINC/ }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
      const alert = screen.getByRole("alert");
      expect(within(alert).getAllByRole("link")).toHaveLength(2);
      fireEvent.click(within(alert).getByRole("link", { name: /Proposed.*#101/ }));
      await waitFor(() => expect(document.getElementById("code-mapping-101")).toHaveFocus());
    });

    it("detects duplicates within a section and clears the error after curator deletion", async () => {
      const rows: TestMappingRow[] = [proposed, { ...mapped, status: "proposed" }];
      renderPage(rows);
      const alert = await screen.findByRole("alert");
      fireEvent.click(within(alert).getByRole("link", { name: /#102/ }));
      const row = document.getElementById("code-mapping-102")!;
      fireEvent.click(within(row).getByRole("button", { name: "Edit A02.0" }));
      await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
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
    const dialog = await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
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

    it("shows retirement inline with the unchanged source code", async () => {
      // #1080 replaced the Retired column with Seen and added Dest count. The
      // retirement metadata is still served and still shown, but only once a
      // curator opens the row -- so this is the only place it can be asserted.
      renderPage([high, low]);
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      expect(columnIndex(table, "Source")).toBe(0);
      expect(within(table).queryByRole("columnheader", { name: "Retired" })).not.toBeInTheDocument();
      const row = document.getElementById("code-mapping-31")!;
      fireEvent.click(within(row).getByRole("button", { name: "Edit Z10" }));
      // Wait for the dialog before waiting for the loader: opening takes an edit
      // lock first (#1440), so the loader guard below was passing before the
      // dialog — and its GET — even existed.
      await screen.findByRole("dialog");
      await waitFor(() => expect(screen.queryByText("Loading source destinations…")).not.toBeInTheDocument());
      expect(screen.getByTestId("source-code-metadata")).toHaveTextContent("Retired");
      expect(within(screen.getByTestId("source-code-metadata")).getByText("Retired"))
        .toHaveAttribute("title", expect.stringContaining("invalid reason D"));
      fireEvent.change(screen.getByLabelText("Source Code Value"), { target: { value: "A3" } });
      expect(screen.queryByTestId("source-code-metadata")).not.toBeInTheDocument();
    });

    // #1575 replaced the Provenance-then-Seen default with plain Seen. Grouping
    // by provenance buried the rows a Suggest run had just answered, because a
    // run rewrites origin_system from '' to 'suggest v0.4', which sorts last.
    it("defaults Proposed to Seen descending", async () => {
      renderPage([
        { ...high, origin_system: "suggest v0.4", occurrence_count: 200 },
        { ...low, origin_system: "curator", occurrence_count: 3 },
        { ...high, mapping_id: 33, source_code: "Z20", origin_system: "curator", occurrence_count: 20 },
        { ...low, mapping_id: 34, source_code: "A1", origin_system: "curator", occurrence_count: 20 },
      ]);
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      expect(within(table).getByRole("button", { name: "Seen" }).closest("th")).toHaveAttribute("aria-sort", "descending");
      expect(ids(table)).toEqual(["code-mapping-31", "code-mapping-34", "code-mapping-33", "code-mapping-32"]);
    });

    it.each(["Source", "Seen", "Destination"])(
      "sorts %s ascending and descending", async (column) => {
        renderPage([high, low]);
        const table = await screen.findByRole("table", { name: "Proposed mappings" });
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
      const unmapped = await screen.findByRole("table", { name: "Proposed mappings" });
      fireEvent.click(screen.getByRole("button", { name: /^Approved\(/ }));
      const mapped = screen.getByRole("table", { name: "Approved mappings" });
      fireEvent.click(within(unmapped).getByRole("button", { name: "Destination" }));
      fireEvent.click(within(mapped).getByRole("button", { name: "Destination" }));
      fireEvent.click(within(mapped).getByRole("button", { name: "Destination" }));
      expect(ids(unmapped)).toEqual(["code-mapping-32", "code-mapping-31"]);
      expect(ids(mapped)).toEqual(["code-mapping-41", "code-mapping-51", "code-mapping-42", "code-mapping-52"]);
      expect(within(mapped).queryByRole("columnheader", { name: "Status" })).not.toBeInTheDocument();
    });

    it("does not spend dialog space on absent retirement metadata", async () => {
      renderPage([{ ...low, mapping_id: 33, source_code: "A3", source_retired: null }]);
      const cell = await screen.findByText("A3", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(screen.queryByTestId("source-code-metadata")).not.toBeInTheDocument();
    });
  });

  it("stacks source code, description and system in one Source column", async () => {
    renderPage();
    const row = (await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!;
    const sourceCell = cellUnder(row, "Source");
    expect(sourceCell).toHaveTextContent("M-PROTEIN, SERUM");
    expect(sourceCell).toHaveTextContent("M-protein, serum");
    expect(screen.queryByRole("columnheader", { name: "Source code system" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "OMOP table" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("columnheader", { name: "Seen" })[0]).toBeInTheDocument();
  });

  it("also filters legacy list responses and keeps the checkbox synchronized across sections", async () => {
    renderPage([proposedRow, { ...proposedRow, mapping_id: 77, source_code: "NEVER SEEN", occurrence_count: 0 },
      { ...approvedRow, source_vocabulary_id: "" }]);
    await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
    expect(screen.queryByText("NEVER SEEN")).not.toBeInTheDocument();
    expect(screen.getByTestId("all-mappings-count")).toHaveTextContent("(2)");
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));
    expect(screen.getByRole("checkbox", { name: "Approved: only codes with Seen greater than zero" })).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Proposed: only codes with Seen greater than zero" }));
    await screen.findByText("NEVER SEEN");
    expect(screen.getByRole("checkbox", { name: "Approved: only codes with Seen greater than zero" })).not.toBeChecked();
    expect(screen.getByTestId("all-mappings-count")).toHaveTextContent("(3)");
  });

  it("searches mappings across source-vocabulary tabs", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });

    fireEvent.change(screen.getByRole("textbox", { name: "Search mappings" }), {
      target: { value: "C90.00" },
    });
    // The matching ICD-10-CM row lives in the non-active tab and is approved,
    // so expand its section after the global search has located it.
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));
    expect(await screen.findByText("C90.00", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
  });

  it("labels cross-tab hits with their coding system in browse mode (#963)", async () => {
    // The page always asks for browse=1, and the server answers a query with
    // rows from every coding system (mapping_browse: `mappings if search`).
    // System info is stacked in the Source column (no separate System column).
    const pages = { Unmapped: { page: 1, page_size: 100, total: 1 }, Approved: { page: 1, page_size: 100, total: 1 },
      Rejected: { page: 1, page_size: 100, total: 0 } };
    const tabs = [
      { vocabulary_id: "", label: "Uncoded", is_standard: false, proposed: 1, approved: 0, athena: 0 },
      { vocabulary_id: "ICD10CM", label: "ICD-10-CM", is_standard: false, proposed: 0, approved: 1, athena: 0 },
    ];
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/v1/code-mappings/") {
        const search = String(config?.params?.search || "");
        return Promise.resolve({ data: {
          results: search ? [proposedRow, approvedRow].filter((r) => r.source_code.includes(search)) : [proposedRow],
          duplicates: [], selected_source: "", rejected_count: 0, tabs, pages,
        } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });

    fireEvent.change(screen.getByRole("textbox", { name: "Search mappings" }), { target: { value: "C90.00" } });
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));
    const row = (await screen.findByText("C90.00", { selector: '[data-testid=source-code]' })).closest("tr")!;
    // System label is stacked in the Source column, not a separate column.
    expect(cellUnder(row, "Source")).toHaveTextContent("ICD-10-CM");
    expect(await screen.findByText(/Searching all coding systems — 1 match from other tabs/)).toBeInTheDocument();
  });

  it("splits proposed and approved into Unmapped and Mapped sections", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: /^Proposed\(/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Approved\(/ })).toBeInTheDocument();
    // Approved rows live under Mapped, which is collapsed by default.
    expect(screen.queryByText("C90.00")).not.toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("tablist", { name: "Source vocabularies" }))
      .getByRole("tab", { name: /ICD-10-CM/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Approved\(/ }));
    expect(await screen.findByText("C90.00")).toBeInTheDocument();
  });

  it("can collapse and expand the Unmapped section", async () => {
    renderPage();
    const unmapped = await screen.findByRole("button", { name: /Proposed/ });
    expect(screen.getByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).toBeInTheDocument();

    fireEvent.click(unmapped);
    expect(screen.queryByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).not.toBeInTheDocument();

    fireEvent.click(unmapped);
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
  });

  it("offers tabs for the source vocabularies represented in the queue", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
    const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
    expect(tabs.getByRole("tab", { name: /Uncoded/ })).toBeInTheDocument();
    expect(tabs.getByRole("tab", { name: /ICD-10-CM/ })).toBeInTheDocument();
  });

  it("selects Uncoded instead of falling back to the default vocabulary", async () => {
    const proposedIcd10Row = { ...approvedRow, status: "proposed" as const };
    renderPage([proposedRow, proposedIcd10Row]);
    const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
    const uncoded = tabs.getByRole("tab", { name: /Uncoded/ });
    const icd10 = tabs.getByRole("tab", { name: /ICD-10-CM/ });

    fireEvent.click(icd10);
    expect(icd10).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("C90.00", { selector: '[data-testid=source-code]' })).toBeInTheDocument();

    fireEvent.click(uncoded);
    expect(uncoded).toHaveAttribute("aria-selected", "true");
    expect(icd10).toHaveAttribute("aria-selected", "false");
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
    expect(screen.queryByText("C90.00", { selector: '[data-testid=source-code]' })).not.toBeInTheDocument();
  });

  it("adds a tab for a source system that arrives in SCCM data", async () => {
    renderPage([{ ...proposedRow, source_vocabulary_id: "MedDRA", source_code: "10000001" }]);
    const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
    const medDra = tabs.getByRole("tab", { name: /MedDRA/ });
    fireEvent.click(medDra);
    expect(await screen.findByText("10000001", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
  });

  it("files Apple, Garmin, HL7-PHR and HealthConnect rows under the one Wearables tab", async () => {
    const wearable = (mapping_id: number, source_vocabulary_id: string, source_code: string) =>
      ({ ...proposedRow, mapping_id, source_vocabulary_id, source_code });
    renderPage([wearable(301, "Apple", "apple_steps"), wearable(302, "Garmin", "garmin_steps"),
      wearable(303, "HL7-PHR", "phr_steps"), wearable(304, "HealthConnect", "StepsRecord")]);
    const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
    // One tab for the four vocabularies, and none of its own for HL7-PHR or HealthConnect.
    const wearables = await tabs.findByRole("tab", { name: /OpenWearables/ });
    expect(tabs.queryByRole("tab", { name: /HL7-PHR|HealthConnect/ })).not.toBeInTheDocument();
    expect(tabs.queryByRole("tab", { name: /Apple|Garmin/ })).not.toBeInTheDocument();
    fireEvent.click(wearables);
    expect(wearables).toHaveAttribute("aria-selected", "true");
    for (const code of ["apple_steps", "garmin_steps", "phr_steps", "StepsRecord"]) {
      expect(await screen.findByText(code, { selector: '[data-testid=source-code]' })).toBeInTheDocument();
    }
  });

  it("has no All tab", async () => {
    renderPage();
    await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
    const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
    expect(tabs.queryByRole("tab", { name: /^All/ })).not.toBeInTheDocument();
  });

  it("lands on the tab that has review work, not the first tab", async () => {
    // SNOMED sorts first; the proposals are in HK-Labs. Defaulting to SNOMED
    // would make the queue look empty when it is not.
    renderPage();
    expect(await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
  });

  describe("the dialog", () => {
    const openDialog = async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
          { stage: "result", suggested: loincHit, candidates: [loincHit, alternative, semantic],
            alternatives: [{ concept_id: loincHit.concept_id, confidence: 0.9 }, { concept_id: 556, confidence: 0.72 }] },
        ] }) }) : originalGet(url));
      const dialog = within(screen.getByRole("dialog"));
      const suggest = dialog.getByRole("button", { name: "Suggest", exact: true });
      fireEvent.click(suggest);
      const section = await dialog.findByRole("region", { name: "Individual suggestion candidates" });
      expect(suggest.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(section.compareDocumentPosition(dialog.getByLabelText("Search destination concepts")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(within(section).getByRole("status")).toHaveTextContent("Searching");
      fireEvent.click(within(section).getByText(/Early lexical candidate/).closest("tr")!);
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(555);
      expect(mockPatch).not.toHaveBeenCalled();
      expect(await within(section).findByText("0.125", {}, { timeout: 3000 })).toBeInTheDocument();
      expect(within(section).getByText(`Winner: ${loincHit.concept_name}`)).toBeInTheDocument();
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(555);
      expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.objectContaining({ async: true }));
      fireEvent.click(within(section).getByText(/Later semantic candidate/).closest("tr")!);
      expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(556);
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      // The decision records this tab and the confidence the run gave the
      // candidate the curator chose, not the winner's (#2382).
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({
        destination_concept_id: 556, decision_surface: "code2concept", decision_confidence: 0.72,
      })));
    });

    it("keeps the run's confidence through concept-search keystrokes that change nothing it scored", async () => {
      await openDialog();
      const activity = [
        { stage: "result", suggested: loincHit, candidates: [loincHit], alternatives: [{ concept_id: loincHit.concept_id, confidence: 0.9 }] },
      ];
      mockPost.mockResolvedValue({ data: suggestRun({ state: "success", activity }) });
      const dialog = within(screen.getByRole("dialog"));
      fireEvent.click(dialog.getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(dialog.getByLabelText("Destination Concept ID")).toHaveValue(loincHit.concept_id));
      fireEvent.change(dialog.getByLabelText("Search destination concepts"), { target: { value: "prot" } });
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({
        destination_concept_id: loincHit.concept_id, decision_confidence: 0.9,
      })));
    });

    it("sends no confidence when the destination was not chosen from a Suggest run here", async () => {
      await openDialog();
      const dialog = within(screen.getByRole("dialog"));
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({ decision_surface: "code2concept" })));
      const [, payload] = mockPatch.mock.calls.at(-1) as [string, Record<string, unknown>];
      expect(payload).not.toHaveProperty("decision_confidence");
    });

    it("sends the note only when the curator changed it, against the stored one (#2423)", async () => {
      // The list's copy is stale: the stored note has moved on since it loaded.
      const open = async () => {
        renderPage();
        const listed = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/"
          ? Promise.resolve({ data: { ...proposedRow, notes: "high confidence: stored since." } })
          : listed(url, ...rest));
        const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
        fireEvent.click(cell.closest("tr")!);
        const dialog = within(await screen.findByRole("dialog"));
        await waitFor(() => expect(dialog.getByLabelText("Notes")).toHaveValue("high confidence: stored since."));
        return dialog;
      };
      const dialog = await open();
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalled());
      expect((mockPatch.mock.calls.at(-1) as [string, Record<string, unknown>])[1].notes).toBeUndefined();

      cleanup();
      mockPatch.mockClear();
      const reopened = await open();
      fireEvent.change(reopened.getByLabelText("Notes"), { target: { value: "Ask the lab." } });
      fireEvent.click(reopened.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.objectContaining({ notes: "Ask the lab." })));
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

    it("says the LOINC axes are loading, with the row's flagged count, until they arrive (#2030)", async () => {
      renderPage([{ ...proposedRow, axis_variant_count: 2 }]);
      const originalGet = mockGet.getMockImplementation()!;
      let answer: (value: unknown) => void = () => {};
      mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/loinc-axes/"
        ? new Promise(resolve => { answer = resolve; }) : originalGet(url, ...rest));
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      const panel = await screen.findByRole("region", { name: "LOINC axes" });
      expect(panel).toHaveAttribute("aria-busy", "true");
      // The queue flagged this code, so its section starts open: there is work in it.
      expect(within(panel).getByRole("button", { name: /LOINC axes/ })).toHaveAttribute("aria-expanded", "true");
      expect(within(panel).getByRole("status")).toHaveTextContent(
        "Loading the 2 axis values the queue flagged for this code, and their destinations…");
      answer({ data: {
        mapping_id: 7, applies: true, loinc_axes: {}, source_axes: { axes: {}, hints: {} }, axis_variant_count: 2, variant_notes: [],
        variants: [
          { id: 1, axis_key: "property=MCnc", axis_values: { property: "MCnc" }, units: ["mg/dL"], occurrence_count: 40, status: "proposed", target_concept: null, reviewed_at: null },
          { id: 2, axis_key: "property=SCnc", axis_values: { property: "SCnc" }, units: ["mmol/L"], occurrence_count: 3, status: "proposed", target_concept: null, reviewed_at: null },
        ],
      } });
      await within(panel).findByRole("table", { name: "Axis variants" });
      expect(panel).not.toHaveAttribute("aria-busy");
    });

    it("fills in the axis variants with proposed LOINCs when Suggest runs for a code with several (#2012)", async () => {
      renderPage();
      const glucose = { concept_id: 3004501, concept_name: "Glucose [Mass/volume] in Serum or Plasma", concept_code: "2345-7", vocabulary_id: "LOINC" };
      const axes = (variants: unknown[]) => ({
        mapping_id: 7, applies: true, loinc_axes: {}, source_axes: { axes: {}, hints: {} }, axis_variant_count: variants.length,
        variants, variant_notes: [],
      });
      const empty = [
        { id: 1, axis_key: "property=MCnc", axis_values: { property: "MCnc" }, units: ["mg/dL"], occurrence_count: 40, status: "proposed", target_concept: null, reviewed_at: null },
        { id: 2, axis_key: "property=SCnc", axis_values: { property: "SCnc" }, units: ["mmol/L"], occurrence_count: 3, status: "proposed", target_concept: null, reviewed_at: null },
      ];
      let variants = empty;
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/loinc-axes/"
        ? Promise.resolve({ data: axes(variants) }) : originalGet(url, ...rest));
      mockPost.mockImplementation((url: string) => {
        if (url === "/v1/code-mappings/7/axis-variants/suggest/") {
          // The run is queued like the code's own Suggest; inline it finishes in the request.
          variants = empty.map(v => ({ ...v, target_concept: glucose }));
          return Promise.resolve({ data: suggestRun({ run_id: "variants-run", activity: [{ stage: "result", mapping_id: 7,
            suggestions: [{ axis_key: "property=MCnc", suggested: glucose }, { axis_key: "property=SCnc", suggested: glucose }],
            variants }] }) });
        }
        return Promise.resolve({ data: suggestRun({ activity: [] }) });
      });
      fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
      const panel = await screen.findByRole("region", { name: "LOINC axes" });
      fireEvent.click(within(panel).getByRole("button", { name: /LOINC axes/ }));
      await within(panel).findByRole("table", { name: "Axis variants" });
      const dialog = within(screen.getByRole("dialog"));
      fireEvent.click(dialog.getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/7/axis-variants/suggest/", expect.objectContaining({
        strategies: expect.arrayContaining(["lexical"]), ranking_model: expect.any(String) })));
      expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.anything());
      expect(await screen.findByTestId("variant-suggestion")).toHaveTextContent("Suggested a LOINC for 2 of 2 axis variants (2 in all); approve each under LOINC axes.");
      // The panel re-read the variants: each now shows the proposed LOINC, awaiting approval.
      await waitFor(() => expect(within(panel).getAllByText(glucose.concept_name)).toHaveLength(2));
      expect(within(panel).getAllByText("proposed")).toHaveLength(2);
    });

    it("does not call the variant suggestion for a code with one axis set", async () => {
      await openDialog();
      mockPost.mockResolvedValue({ data: suggestRun({ activity: [] }) });
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.anything()));
      expect(mockPost).not.toHaveBeenCalledWith(expect.stringContaining("/axis-variants/suggest/"), expect.anything());
      expect(screen.queryByTestId("variant-suggestion")).not.toBeInTheDocument();
    });

    describe("LOINC axes and SNOMED evidence sections", () => {
      const openWithEvidence = async () => {
        renderPage();
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => {
          if (url === "/v1/code-mappings/7/loinc-axes/") return Promise.resolve({ data: {
            mapping_id: 7, applies: true, loinc_axes: { system: "Ser" },
            source_axes: { axes: { scale_type: { value: "Qn", basis: "value_type", evidence: "valueQuantity" } }, hints: {} },
            axis_variant_count: 0, variants: [], variant_notes: [],
          } });
          if (url === "/v1/code-mappings/7/snomed-attributes/") return Promise.resolve({ data: {
            mapping_id: 7, applies: true, snomed_attributes: { laterality: "Left" }, source: null,
            hierarchies: { Disorder: "disorder" }, lateralities: ["Left", "Right", "Right and left"],
          } });
          return originalGet(url, ...rest);
        });
        fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
        return {
          axes: await screen.findByRole("region", { name: "LOINC axes" }),
          snomed: await screen.findByRole("region", { name: "SNOMED evidence" }),
        };
      };

      it("opens with both collapsed, each heading saying what it holds", async () => {
        const { axes, snomed } = await openWithEvidence();
        const axesToggle = within(axes).getByRole("button", { name: /LOINC axes/ });
        const snomedToggle = within(snomed).getByRole("button", { name: /SNOMED evidence/ });
        expect(axesToggle).toHaveAttribute("aria-expanded", "false");
        expect(snomedToggle).toHaveAttribute("aria-expanded", "false");
        await waitFor(() => expect(within(axes).getByTestId("section-marker")).toHaveTextContent("1 set · 1 inferred"));
        expect(within(snomed).getByTestId("section-marker")).toHaveTextContent("1 set");
        expect(within(axes).queryByTestId("axis-value-system")).not.toBeInTheDocument();
        expect(within(axes).queryByRole("button", { name: "Save axes" })).not.toBeInTheDocument();
        expect(within(snomed).queryByLabelText("Hierarchy")).not.toBeInTheDocument();
      });

      it("reveals a section's fields when its heading is clicked, and hides them again", async () => {
        const { axes, snomed } = await openWithEvidence();
        const axesToggle = within(axes).getByRole("button", { name: /LOINC axes/ });
        await waitFor(() => expect(within(axes).getByTestId("section-marker")).toHaveTextContent("1 set"));
        fireEvent.click(axesToggle);
        expect(axesToggle).toHaveAttribute("aria-expanded", "true");
        expect(within(axes).getByTestId("axis-value-system")).toHaveTextContent("Ser");
        expect(within(axes).getByTestId("axis-value-scale_type")).toHaveTextContent("Qn");
        expect(within(axes).queryByTestId("section-marker")).not.toBeInTheDocument();
        fireEvent.click(within(snomed).getByRole("button", { name: /SNOMED evidence/ }));
        expect(within(snomed).getByLabelText("Laterality")).toHaveValue("Left");
        fireEvent.click(axesToggle);
        expect(axesToggle).toHaveAttribute("aria-expanded", "false");
        expect(within(axes).queryByTestId("axis-value-system")).not.toBeInTheDocument();
      });

      it("shows no marker on a section that holds nothing", async () => {
        renderPage();
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/loinc-axes/"
          ? Promise.resolve({ data: { mapping_id: 7, applies: true, loinc_axes: {}, source_axes: { axes: {}, hints: {} },
            axis_variant_count: 0, variants: [], variant_notes: [] } })
          : originalGet(url, ...rest));
        fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
        const axes = await screen.findByRole("region", { name: "LOINC axes" });
        await waitFor(() => expect(axes).not.toHaveAttribute("aria-busy"));
        expect(within(axes).queryByTestId("section-marker")).not.toBeInTheDocument();
      });

      const glucose = { concept_id: 3004501, concept_name: "Glucose [Mass/volume] in Serum or Plasma", concept_code: "2345-7", vocabulary_id: "LOINC" };
      const twoVariants = [
        { id: 1, axis_key: "property=MCnc", axis_values: { property: "MCnc" }, units: ["mg/dL"], occurrence_count: 40, status: "proposed", target_concept: null, reviewed_at: null },
        { id: 2, axis_key: "property=SCnc", axis_values: { property: "SCnc" }, units: ["mmol/L"], occurrence_count: 3, status: "proposed", target_concept: null, reviewed_at: null },
      ];
      const axesWith = (variants: unknown[], loinc_axes: Record<string, string | null> = {}) => ({
        mapping_id: 7, applies: true, loinc_axes, source_axes: { axes: {}, hints: {} },
        axis_variant_count: variants.length, variants, variant_notes: [],
      });
      /** A code the list did not flag, whose axes request finds two variants. */
      const openWithVariants = async (current: { variants: unknown[] }) => {
        renderPage();
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/loinc-axes/"
          ? Promise.resolve({ data: axesWith(current.variants) }) : originalGet(url, ...rest));
        fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
        const panel = await screen.findByRole("region", { name: "LOINC axes" });
        await waitFor(() => expect(within(panel).getByTestId("section-marker")).toHaveTextContent("2 axis values"));
        return panel;
      };

      it("opens LOINC axes when Suggest has filled in the variants it sends the curator to", async () => {
        const current = { variants: twoVariants as unknown[] };
        const panel = await openWithVariants(current);
        const toggle = within(panel).getByRole("button", { name: /LOINC axes/ });
        expect(toggle).toHaveAttribute("aria-expanded", "false");
        mockPost.mockImplementation((url: string) => {
          if (url === "/v1/code-mappings/7/axis-variants/suggest/") {
            current.variants = twoVariants.map(v => ({ ...v, target_concept: glucose }));
            return Promise.resolve({ data: suggestRun({ run_id: "variants-run", activity: [{ stage: "result", mapping_id: 7,
              suggestions: twoVariants.map(v => ({ axis_key: v.axis_key, suggested: glucose })), variants: current.variants }] }) });
          }
          return Promise.resolve({ data: suggestRun({ activity: [] }) });
        });
        fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest", exact: true }));
        expect(await screen.findByTestId("variant-suggestion")).toHaveTextContent("approve each under LOINC axes");
        // Opened without a click, showing what the run proposed.
        await waitFor(() => expect(toggle).toHaveAttribute("aria-expanded", "true"));
        await waitFor(() => expect(within(panel).getAllByText(glucose.concept_name)).toHaveLength(2));
      });

      it("opens LOINC axes when a candidate is used for a variant while the section is closed", async () => {
        const panel = await openWithVariants({ variants: twoVariants });
        const toggle = within(panel).getByRole("button", { name: /LOINC axes/ });
        fireEvent.click(toggle);
        mockPost.mockImplementation((url: string, body: { axis_key?: string; target_concept_id?: number }) => url === "/v1/code-mappings/7/axis-variants/"
          ? Promise.resolve({ data: { ...axesWith(twoVariants.map(v => v.axis_key === body.axis_key ? { ...v, target_concept: glucose } : v)), variant_status: "proposed" } })
          : Promise.resolve({ data: suggestRun({ activity: [
            { stage: "result", suggested: glucose, candidates: [glucose], alternatives: [{ concept_id: glucose.concept_id, confidence: 0.9 }] },
          ] }) }));
        fireEvent.click(within(panel).getAllByRole("button", { name: /^Suggest for / })[0]);
        const candidates = await screen.findByRole("region", { name: "Individual suggestion candidates" });
        await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.objectContaining({ axis_key: "property=MCnc" })));
        fireEvent.click(toggle);
        expect(toggle).toHaveAttribute("aria-expanded", "false");
        fireEvent.click((await within(candidates).findAllByText(new RegExp("Glucose")))[0].closest("tr")!);
        await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/7/axis-variants/",
          { axis_key: "property=MCnc", target_concept_id: glucose.concept_id, approve: false }));
        // The outcome, and the Approve it leads to, are in the section: it opens.
        await waitFor(() => expect(toggle).toHaveAttribute("aria-expanded", "true"));
        expect(await within(panel).findByText(/Variant destination saved/)).toBeInTheDocument();
      });

      it("starts a code the queue flagged with LOINC axes open, and any other closed", async () => {
        renderPage([{ ...proposedRow, axis_variant_count: 2 }, { ...approvedRow, status: "proposed" as const, source_vocabulary_id: "" }]);
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => /\/loinc-axes\/$/.test(url)
          ? Promise.resolve({ data: axesWith(url.includes("/7/") ? twoVariants : []) }) : originalGet(url, ...rest));
        fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
        const flagged = await screen.findByRole("region", { name: "LOINC axes" });
        expect(within(flagged).getByRole("button", { name: /LOINC axes/ })).toHaveAttribute("aria-expanded", "true");
        expect(await within(flagged).findByRole("table", { name: "Axis variants" })).toBeInTheDocument();
        fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
        fireEvent.click((await screen.findByText("C90.00", { selector: '[data-testid=source-code]' })).closest("tr")!);
        const plain = await screen.findByRole("region", { name: "LOINC axes" });
        expect(within(plain).getByRole("button", { name: /LOINC axes/ })).toHaveAttribute("aria-expanded", "false");
      });

      it("sends the axes and SNOMED settings with Suggest though neither section was opened", async () => {
        const { axes } = await openWithEvidence();
        await waitFor(() => expect(within(axes).getByTestId("section-marker")).toHaveTextContent("1 set"));
        mockPost.mockResolvedValue({ data: suggestRun({ activity: [] }) });
        const dialog = within(screen.getByRole("dialog"));
        fireEvent.click(dialog.getByRole("button", { name: "Suggest", exact: true }));
        await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.objectContaining({
          loinc_axes: { system: "Ser" }, snomed_attributes: { laterality: "Left" } })));
      });

      it("saves the mapping the same whether or not the sections were opened", async () => {
        // Axes and SNOMED settings are saved by their own sections, never by
        // the mapping's Save, so a closed section cannot drop them.
        await openWithEvidence();
        const dialog = within(screen.getByRole("dialog"));
        fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
        await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.any(Object)));
        const [, payload] = mockPatch.mock.calls.at(-1) as [string, Record<string, unknown>];
        expect(payload).not.toHaveProperty("loinc_axes");
        expect(payload).not.toHaveProperty("snomed_attributes");
      });
    });

    it("sends the SNOMED evidence settings with Suggest only while the source is unchanged", async () => {
      renderPage();
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/snomed-attributes/"
        ? Promise.resolve({ data: { mapping_id: 7, applies: true, snomed_attributes: { laterality: "Left" }, source: null,
          hierarchies: { Disorder: "disorder" }, lateralities: ["Left", "Right", "Right and left"] } })
        : originalGet(url, ...rest));
      fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
      const panel = await screen.findByRole("region", { name: "SNOMED evidence" });
      fireEvent.click(within(panel).getByRole("button", { name: /SNOMED evidence/ }));
      fireEvent.change(within(panel).getByLabelText("Hierarchy"), { target: { value: "__none__" } });
      mockPost.mockResolvedValue({ data: suggestRun({ activity: [] }) });
      const dialog = within(screen.getByRole("dialog"));
      fireEvent.click(dialog.getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.objectContaining({
        snomed_attributes: { laterality: "Left", hierarchy: null } })));
      mockPost.mockClear();
      mockPost.mockResolvedValue({ data: suggestRun({ activity: [] }) });
      fireEvent.change(screen.getByLabelText("Source Code Value"), { target: { value: "CHANGED" } });
      fireEvent.click(dialog.getByRole("button", { name: "Suggest", exact: true }));
      await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/suggest-one/", expect.anything()));
      const body = mockPost.mock.calls.find(([url]) => url === "/v1/code-mappings/suggest-one/")![1];
      expect(body).not.toHaveProperty("snomed_attributes");
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
      fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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

    it("shows a populated source concept ID parenthetically beside the code", async () => {
      renderPage([{
        ...proposedRow,
        source_vocabulary_id: "ICD10CM",
        source_code: "C90.20",
        source_code_description: "Extramedullary plasmacytoma not having achieved remission",
        source_concept_id: 45542660,
      }]);
      const cell = await screen.findByText("C90.20", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      // Opening the dialog takes a lock first (#1440), so it appears a tick later.
      await screen.findByText("Edit Mapping");
      expect(screen.getByLabelText("Source Description")).toHaveValue(
        "Extramedullary plasmacytoma not having achieved remission",
      );
      expect(screen.getByTestId("source-code-metadata")).toHaveTextContent("(OMOP concept 45542660)");
      expect(screen.queryByLabelText("Source Concept ID")).not.toBeInTheDocument();
    });

    it("shows an example patient's Source Metadata for the code", async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string, config?: unknown) => url === "/v1/concept-to-code/source-examples/"
        ? Promise.resolve({ data: { examples: [{
          person_id: 21, omop_table: "measurement", occurrence_count: 3, source_unit: "g/dL",
          example_quantity: "1.2", source_metadata: { specimen: "serum" }, last_seen: null,
        }] } })
        : originalGet(url, config));
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(await screen.findByRole("combobox", { name: "Example patient" })).toHaveTextContent("Patient 21 · Seen 3");
      expect(screen.getByText(/"specimen": "serum"/)).toBeInTheDocument();
      expect(mockGet).toHaveBeenCalledWith("/v1/concept-to-code/source-examples/", {
        params: expect.objectContaining({ source_code: "M-PROTEIN, SERUM" }),
      });
    });

    it("expands the mapping's own Source Metadata when no patient row carries the code", async () => {
      renderPage([{ ...proposedRow, source_evidence: {
        organization: null, occurrence_count: 12, group_occurrence_count: null, units: [],
        metadata: { patients: 205, codings: 12, specimen: "serum" },
      } }]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      fireEvent.click(await screen.findByRole("button", { name: /Source Metadata/ }));
      expect(screen.getByText(/"specimen": "serum"/)).toBeInTheDocument();
      expect(await screen.findByTestId("source-examples-empty")).toHaveTextContent("aggregate source inventory");
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
        "Source Quantity",
        "Source Unit",
        "BasedOn",
      ]);
      expect((screen.getByLabelText("Domain") as HTMLSelectElement).value).toBe("Measurement");
    });

    it("removes the manual UMLS action from the dialog", async () => {
      await openDialog();
      expect(screen.queryByRole("button", { name: "Check UMLS" })).not.toBeInTheDocument();
      expect(mockPost).not.toHaveBeenCalledWith("/v1/code-mappings/check-umls/", expect.anything());
    });

    it("shows organization, percentiles, reference-range evidence, and suppression", async () => {
      renderPage([{
        ...proposedRow,
        organization_name: "Memorial Hospital",
        organization_slug: "memorial",
        source_evidence: {
          organization: { id: 4, slug: "memorial", name: "Memorial Hospital" },
          facilities: [{
            name: "Huntsman Cancer Institute", level: "attached", confidence: "medium",
            parent_name: "University of Utah Health", records: 12000, patients: 204,
          }],
          occurrence_count: 12398,
          group_occurrence_count: 13032,
          metadata: {
            patients: 205,
            codings: 12398,
            unit_coverage: { records: 12004, percent: 96.8 },
            reference_range: { records: 9000, percent: 72.6, low_p50: 0.6, high_p50: 1.17, unit: "mg/dL" },
            category: { top: "laboratory", mix: "laboratory (12398)" },
            value_types: { quantity: 0, coded: 0, text: 0 },
          },
          units: [
            { display: "mg/dL", code: "mg/dL", normalized: "mg/dL", verdict: "valid", verdict_reason: "exact release match", count: 12000, patients: 204, values: 11990, distribution: { min: 0.1, p25: 0.7, p50: 0.9, p75: 1.1, max: 8.2 } },
            { display: "mmol/L", code: "mmol/L", count: 4, suppressed: true },
          ],
        },
      }]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      const evidence = await screen.findByRole("region", { name: "Source evidence" });
      await waitFor(() => expect(evidence).toHaveTextContent("Memorial Hospital"));
      expect(screen.getByTestId("source-organization")).toHaveTextContent("Hospital / organization: Memorial Hospital");
      expect(evidence).toHaveTextContent("13,032");
      expect(within(evidence).getByRole("table", { name: "Observed source facilities" })).toHaveTextContent("Huntsman Cancer Institute");
      expect(evidence).toHaveTextContent("Parent: University of Utah Health");
      expect(evidence).toHaveTextContent("attached");
      expect(evidence).toHaveTextContent("medium");
      expect(evidence).toHaveTextContent("Records across same-label codes");
      expect(evidence).not.toHaveTextContent("Coding occurrences");
      expect(evidence).toHaveTextContent("Values use another FHIR type or are absent");
      expect(evidence).toHaveTextContent("96.8% of source records");
      expect(within(evidence).getByRole("img", {
        name: "Value distribution: Min 0.1; P25 0.7; Median 0.9; P75 1.1; Max 8.2",
      })).toBeInTheDocument();
      expect(within(evidence).getByRole("table", { name: "Observed source units" })).toHaveTextContent("Median 0.9");
      expect(evidence).toHaveTextContent("Normalized: mg/dL · valid");
      expect(evidence).toHaveTextContent("exact release match");
      expect(evidence).toHaveTextContent("204 patients · 11,990 numeric values");
      expect(evidence).toHaveTextContent("Suppressed for a small cohort");
      expect(evidence).toHaveTextContent("0.6–1.17 mg/dL");
      expect(evidence).toHaveTextContent("Recognition evidence only");
      expect(evidence).not.toHaveTextContent("Mean");
      expect(evidence).not.toHaveTextContent("standard deviation");
    });

    it("keeps source code and description adjacent and skips publisher lookup for hospital codes", async () => {
      renderPage([{ ...proposedRow,
        source_vocabulary_id: "http://open.epic.com/FHIR/StructureDefinition/observation-flowsheet-id",
      }]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      const codeField = (await screen.findByLabelText("Source Code Value")).closest("div");
      const descriptionField = screen.getByLabelText("Source Description").closest("div");
      expect(codeField?.nextElementSibling).toBe(descriptionField);
      expect(mockGet).not.toHaveBeenCalledWith(
        "/v1/code-mappings/source-catalog/", expect.anything(),
      );
      expect(screen.queryByText("Could not load source terminology.")).not.toBeInTheDocument();
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
        "Destination Unit",
        "Destination Table",
      ]);
    });

    it("shows the mapping's destination unit in the destination block, once", async () => {
      renderPage([{ ...proposedRow, destination_unit_concept_id: 8582, destination_unit_code: "cm", destination_unit_name: "centimeter" }]);
      fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
      await screen.findByText("Edit Mapping");
      const unit = within(screen.getByTestId("destination-fields")).getByTestId("destination-unit-field");
      expect(unit).toHaveTextContent("Destination Unit");
      expect(within(unit).getByText("cm")).toBeInTheDocument();
      expect(within(unit).getByText("centimeter")).toBeInTheDocument();
      expect(within(unit).getByRole("button", { name: "Clear" })).toBeInTheDocument();
      expect(within(screen.getByRole("dialog")).getAllByText("Destination Unit")).toHaveLength(1);
      // Clearing it offers the search in the same place.
      fireEvent.click(within(unit).getByRole("button", { name: "Clear" }));
      expect(within(screen.getByTestId("destination-fields")).getByRole("textbox", { name: "Search UCUM units" })).toBeInTheDocument();
    });

    describe("the stored destination unit", () => {
      const cm = { destination_unit_concept_id: 8582, destination_unit_code: "cm", destination_unit_name: "centimeter" };
      const openRow = async () => {
        fireEvent.click((await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' })).closest("tr")!);
        await screen.findByText("Edit Mapping");
        return within(screen.getByRole("dialog"));
      };
      const savedUnit = async (dialog: ReturnType<typeof within>) => {
        fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
        await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/7/", expect.any(Object)));
        return (mockPatch.mock.calls.at(-1) as [string, Record<string, unknown>])[1];
      };

      it("is sent back unchanged by a save that did not touch it", async () => {
        renderPage([{ ...proposedRow, ...cm }]);
        const payload = await savedUnit(await openRow());
        expect(payload).toHaveProperty("destination_unit_concept_id", 8582);
      });

      it("is sent as null when the mapping has none", async () => {
        renderPage([{ ...proposedRow, destination_unit_concept_id: null, destination_unit_code: "", destination_unit_name: "" }]);
        const dialog = await openRow();
        await waitFor(() => expect(dialog.getByRole("textbox", { name: "Search UCUM units" })).toBeInTheDocument());
        expect(await savedUnit(dialog)).toHaveProperty("destination_unit_concept_id", null);
      });

      it("is read from the mapping when the row the dialog opened from is stale", async () => {
        // The list row says no unit; another curator has set one since.
        renderPage([{ ...proposedRow, destination_unit_concept_id: null, destination_unit_code: "", destination_unit_name: "" }]);
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/"
          ? Promise.resolve({ data: { ...proposedRow, ...cm, destination_options: [] } }) : originalGet(url, ...rest));
        const dialog = await openRow();
        const unit = await dialog.findByTestId("destination-unit-field");
        await waitFor(() => expect(within(unit).getByText("cm")).toBeInTheDocument());
        expect(within(unit).getByText("centimeter")).toBeInTheDocument();
        expect(await savedUnit(dialog)).toHaveProperty("destination_unit_concept_id", 8582);
      });

      it("does not come back over a unit the curator cleared before the mapping loaded", async () => {
        renderPage([{ ...proposedRow, ...cm }]);
        const originalGet = mockGet.getMockImplementation()!;
        let answer: (value: unknown) => void = () => {};
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/"
          ? new Promise(resolve => { answer = resolve; }) : originalGet(url, ...rest));
        const dialog = await openRow();
        fireEvent.click(within(dialog.getByTestId("destination-unit-field")).getByRole("button", { name: "Clear" }));
        answer({ data: { ...proposedRow, ...cm, destination_options: [] } });
        await waitFor(() => expect(dialog.queryByText("Loading source destinations...")).not.toBeInTheDocument());
        expect(dialog.getByRole("textbox", { name: "Search UCUM units" })).toBeInTheDocument();
        expect(await savedUnit(dialog)).toHaveProperty("destination_unit_concept_id", null);
      });

      it("is left out of a save when the mapping could not be read and the row never said", async () => {
        // proposedRow carries no unit key, as a patient page row does not.
        renderPage();
        const originalGet = mockGet.getMockImplementation()!;
        mockGet.mockImplementation((url: string, ...rest: unknown[]) => url === "/v1/code-mappings/7/"
          ? Promise.reject(new Error("down")) : originalGet(url, ...rest));
        const dialog = await openRow();
        await dialog.findByText(/Could not load source evidence/);
        const payload = await savedUnit(dialog);
        // undefined is dropped from the request body: the server keeps its unit.
        expect(payload.destination_unit_concept_id).toBeUndefined();
        expect(JSON.parse(JSON.stringify(payload))).not.toHaveProperty("destination_unit_concept_id");
      });

      it("is shown, and can be cleared, on a row that is not a measurement and has no source unit", async () => {
        renderPage([{ ...proposedRow, ...cm, domain_id: "Condition", destination_omop_table: "condition", destination_domain_id: "Condition" }]);
        const dialog = await openRow();
        expect(dialog.getByTestId("destination-table")).toHaveValue("condition");
        const unit = within(dialog.getByTestId("destination-fields")).getByTestId("destination-unit-field");
        expect(within(unit).getByText("cm")).toBeInTheDocument();
        fireEvent.click(within(unit).getByRole("button", { name: "Clear" }));
        expect(await savedUnit(dialog)).toHaveProperty("destination_unit_concept_id", null);
      });
    });

    it("states an active destination as plain text beside Mint new concept, with no field name", async () => {
      await openDialog();
      const status = screen.getByTestId("destination-status");
      expect(status.tagName).toBe("P");
      expect(status).toHaveTextContent("Destination status: Active");
      expect(status).toHaveAttribute("title", "Active concepts can be used as destinations.");
      expect(screen.queryByText("Destination Status")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Destination Status")).not.toBeInTheDocument();
      // Same line: the text on the left, the button after it.
      const mint = screen.getByRole("button", { name: "Mint new concept" });
      expect(mint.parentElement).toBe(status.parentElement);
      expect(status.compareDocumentPosition(mint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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
      expect(screen.queryByLabelText("Source Concept ID")).not.toBeInTheDocument();
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
      const previousGet = mockGet.getMockImplementation();
      mockGet.mockImplementation((url: string, ...args: unknown[]) => url === "/v1/concepts/3046299/"
        ? Promise.resolve({ data: loincHit }) : previousGet?.(url, ...args));
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
      const status = screen.getByTestId("destination-status");
      expect(status).toHaveTextContent("Destination status: Retired (reason U)");
      // The reason is read on the page, not only on hover or by a screen reader.
      expect(within(status).getByText("(reason U)").className).not.toMatch(/sr-only/);
      expect(status).toHaveAttribute("title", expect.stringContaining("Retired / invalid (reason U)"));
      expect(screen.queryByText("Destination Status")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Mint new concept" }).parentElement).toBe(status.parentElement);
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
      expect(await screen.findByText("Quantitative · Suggested unit: mg/dL")).toBeInTheDocument();
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
      // Opening the dialog POSTs a lock (#1440) — asserted, because otherwise
      // nothing in this suite observes it — and the mint call is counted apart.
      expect(mockPost).toHaveBeenCalledWith("/v1/code-mappings/7/lock/");
      expect(mockPost.mock.calls.filter(([url]) => !String(url).endsWith("/lock/"))).toHaveLength(1);
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

    it("searches active, standard concepts unless the curator widens it", async () => {
      // #1465: retired and non-standard concepts are opt-in.
      await openDialog();
      const searches = () => mockGet.mock.calls.filter((c) => c[0] === "/v1/concepts/search/");
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      await waitFor(() => expect(searches()).toHaveLength(1));
      expect(searches()[0][1].params).not.toHaveProperty("include_retired");
      expect(searches()[0][1].params).not.toHaveProperty("include_non_standard");

      fireEvent.click(screen.getByLabelText("Include retired"));
      await waitFor(() => expect(searches()).toHaveLength(2));
      expect(searches()[1][1].params.include_retired).toBe("true");
      expect(searches()[1][1].params).not.toHaveProperty("include_non_standard");

      fireEvent.click(screen.getByLabelText("Include non-standard"));
      await waitFor(() => expect(searches()).toHaveLength(3));
      expect(searches()[2][1].params).toMatchObject({ include_retired: "true", include_non_standard: "true" });
    });

    it("labels each search result as standard, non-standard or retired", async () => {
      // #1465: curators asked where a code "came from" because nothing said a
      // result was a retired extension concept.
      await openDialog();
      const otherRequests = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string, ...rest: unknown[]) => {
        if (url !== "/v1/concepts/search/") return otherRequests(url, ...rest);
        {
          return Promise.resolve({ data: { results: [
            loincHit,
            { ...loincHit, concept_id: 3545451, concept_code: "833581000000104", standard_concept: null, invalid_reason: "U" },
            { ...loincHit, concept_id: 2000000001, concept_code: "HK-1", standard_concept: null, invalid_reason: null },
          ] } });
        }
      });
      fireEvent.change(screen.getByLabelText("Search destination concepts"), {
        target: { value: "monoclonal" },
      });
      const retired = await screen.findByText("833581000000104");
      expect(retired.closest("button")).toHaveTextContent("Retired");
      expect(screen.getByText("33358-3").closest("button")).toHaveTextContent("Standard");
      expect(screen.getByText("HK-1").closest("button")).toHaveTextContent("Non-standard");
    });

    it("sends one search for a typed word, not one per keystroke", async () => {
      // #1466: each keystroke used to start its own server query.
      await openDialog();
      const box = screen.getByLabelText("Search destination concepts");
      for (const value of ["mon", "mono", "monoc", "monocl", "monoclonal"]) {
        fireEvent.change(box, { target: { value } });
      }
      await waitFor(() => {
        expect(mockGet.mock.calls.filter((c) => c[0] === "/v1/concepts/search/")).toHaveLength(1);
      });
      const [call] = mockGet.mock.calls.filter((c) => c[0] === "/v1/concepts/search/");
      expect(call[1].params.q).toBe("monoclonal");
    });

    it("aborts the search in flight when the query changes", async () => {
      await openDialog();
      const box = screen.getByLabelText("Search destination concepts");
      const searches = () => mockGet.mock.calls.filter((c) => c[0] === "/v1/concepts/search/");
      fireEvent.change(box, { target: { value: "monoclonal" } });
      await waitFor(() => expect(searches()).toHaveLength(1));
      const first: AbortSignal = searches()[0][1].signal;
      expect(first.aborted).toBe(false);
      fireEvent.change(box, { target: { value: "monoclonal protein" } });
      expect(first.aborted).toBe(true);
      await waitFor(() => expect(searches()).toHaveLength(2));
      expect(searches()[1][1].signal.aborted).toBe(false);
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

    it("closes on save and says the patient records update in the background", async () => {
      // The save returns once the mapping is written; a job moves the stored
      // rows, which can take minutes on a common code (#1952).
      mockPatch.mockResolvedValue({
        data: { ...proposedRow, status: "approved", destination_concept_id: 3046299,
          repoint: { queued: true, from_concept_ids: [0, 2039000101] } },
      });

      await openDialog();
      fireEvent.change(screen.getByLabelText("Destination Concept ID"), {
        target: { value: "3046299" },
      });
      fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
      fireEvent.click(screen.getByRole("button", { name: "Update & Approve" }));

      expect(await screen.findByText(/being updated to the new concept in the background/)).toHaveTextContent("M-PROTEIN, SERUM");
      await waitFor(() => expect(screen.queryByText("Edit Mapping")).not.toBeInTheDocument());
      expect(mockPatch.mock.calls[0][1]).not.toHaveProperty("confirm_unit_mismatch");
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
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      await screen.findByText("MACHINE", { selector: '[data-testid=source-code]' });

      // The data rows carry role="button" (whole-row click), which overrides
      // their implicit "row" role — so query the code cells directly. Testing
      // Library returns them in DOM order.
      const codes = screen
        .getAllByText(/^(MACHINE|HUMAN-ADA|HUMAN-ZOE)$/, { selector: '[data-testid=source-code]' })
        .map((cell) => cell.textContent);
      // Zoe's 900 first, then the import's 5, then Ada's 1 -- who raised it no
      // longer changes the order.
      expect(codes).toEqual(["HUMAN-ZOE", "MACHINE", "HUMAN-ADA"]);
    });

    it("names the creating curator instead of an import system", async () => {
      renderPage([row({ origin: "curator", created_by: "ada@example.com", origin_system: "" })]);
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");
      expect(screen.getByText(/Created by ada@example.com/)).toBeInTheDocument();
      expect(screen.queryByText(/Proposed by import/)).not.toBeInTheDocument();
    });

    it("shows the current provenance to the left of Status in the footer", async () => {
      renderPage();
      const cell = await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(cell.closest("tr")!);
      await screen.findByText("Edit Mapping");

      const provenance = screen.getByTestId("mapping-provenance");
      expect(provenance).toHaveTextContent("Provenance hk-labs");
      // Provenance, then Status, then the actions: left, middle, right.
      const footer = provenance.parentElement!;
      expect(footer.children[0]).toBe(provenance);
      expect(within(footer.children[1] as HTMLElement).getByLabelText("Status")).toBeInTheDocument();
      expect(within(footer.children[2] as HTMLElement).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
    });

    it("defaults Status to Proposed on a new mapping but allows changing it", async () => {
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(screen.getByRole("button", { name: /New Mapping/ }));
      await screen.findByText("New Mapping", { selector: "h2" });

      const statusSelect = screen.getByLabelText("Status") as HTMLSelectElement;
      expect(statusSelect.value).toBe("proposed");
      expect(statusSelect.disabled).toBe(false);
    });
  });

  describe("Sign-off on the provenance line", () => {
    /** Open the dialog for a row that lives under the collapsed Mapped section. */
    const openApproved = async (code: string) => {
      const tabs = within(await screen.findByRole("tablist", { name: "Source vocabularies" }));
      fireEvent.click(tabs.getByRole("tab", { name: /ICD-10-CM/ }));
      fireEvent.click(screen.getByRole("button", { name: /^Approved\(/ }));
      const cell = await screen.findByText(code, { selector: '[data-testid=source-code]' });
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
      const cell = await screen.findByText("UNAPPROVED", { selector: '[data-testid=source-code]' });
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      expect(screen.getByRole("button", { name: /Suggest/ })).toBeEnabled();

      const tabs = within(screen.getByRole("tablist", { name: "Source vocabularies" }));
      fireEvent.click(tabs.getByRole("tab", { name: /ICD-10-CM/ }));
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));

      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress")).toHaveTextContent(
          "Done — wrote 3 new destination(s) across 5 code(s).",
        ));
      expect(screen.getByTestId("suggest-progress")).toHaveTextContent("5/5");
    });

    it("says so when nothing on the tab is awaiting a suggestion", async () => {
      // Importer rows are deliberately left alone, so an empty result is a
      // normal state and not a failure.
      mockPost.mockResolvedValue({ data: suggestRun({ updated: 0, total: 0, ranked: 0 }) });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() =>
        expect(screen.getByTestId("suggest-progress"))
          .toHaveTextContent("nothing on this tab was awaiting a suggestion"));
    });

    it("puts Suggest first directly below the tabs, followed by the enabled strategies", async () => {
      mockPost.mockResolvedValue({ data: suggestRun() });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      const toolbar = screen.getByRole("group", { name: "Suggest controls" });
      expect(screen.getByRole("tablist").nextElementSibling).toBe(toolbar);
      const controls = toolbar.querySelectorAll("button, input");
      expect(controls[0]).toHaveTextContent("Suggest");
      for (const [index, name] of ["UMLS", "Lexical", "Vectors"].entries()) {
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
        limit: 25, strategies: ["umls", "lexical", "vectors"],
      });
      expect(mockPost.mock.calls[0][1]).not.toHaveProperty("lexical_limit");
      expect(mockPost.mock.calls[0][1]).not.toHaveProperty("min_occurrences");
    });

    it("can suggest with semantic retrieval alone and requires at least one retriever", async () => {
      mockPost.mockResolvedValue({ data: suggestRun() });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
      const toolbar = screen.getByRole("group", { name: "Suggest controls" });
      for (const name of ["UMLS", "Lexical"]) {
        fireEvent.click(within(toolbar).getByRole("checkbox", { name }));
      }
      const button = within(toolbar).getByRole("button", { name: "Suggest" });
      const vectors = within(toolbar).getByRole("checkbox", { name: "Vectors" });
      expect(button).toBeEnabled();
      fireEvent.click(vectors);
      expect(button).toBeDisabled();
      fireEvent.click(vectors);
      fireEvent.click(button);
      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      expect(mockPost.mock.calls[0][1]).toMatchObject({ strategies: ["vectors"] });
    });

    it("uses the active server batch cap as the default", async () => {
      const page = renderPage();
      const originalGet = mockGet.getMockImplementation()!;
      mockGet.mockImplementation((url: string) => url === "/v1/code-mappings/reference/"
        ? Promise.resolve({ data: { ...reference, suggest_max_per_run: 3 } }) : originalGet(url));
      // Refresh the reference by switching away and remounting with the capped response.
      page.unmount();
      render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
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
        if (url.includes("/latest/")) return Promise.resolve({ data: { run_id: null } });
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
        <MemoryRouter initialEntries={["/code-mappings?direction=forward"]}>
          <CodeMappingPage />
        </MemoryRouter>,
      );
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      expect(screen.getByText("0.250")).toBeInTheDocument();
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
        if (url.includes("/latest/")) return Promise.resolve({ data: { run_id: null } });
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
        <MemoryRouter initialEntries={["/code-mappings?direction=forward"]}>
          <CodeMappingPage />
        </MemoryRouter>,
      );
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      await screen.findByText("M-PROTEIN, SERUM", { selector: '[data-testid=source-code]' });
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
      expect(await screen.findByText("99999-9", { selector: '[data-testid=source-code]' })).toBeInTheDocument();
    });
  });
});

describe("mapping dialog request isolation", () => {
  const first = { ...proposedRow, source_code: "Z94.81", source_code_description: "Bone marrow transplant status" };
  const second = { ...proposedRow, mapping_id: 99, source_code: "Z12.11", source_code_description: "Screening encounter" };

  it("clears a previous code's no-match message when opening another code", async () => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    mockPost.mockResolvedValueOnce({ data: suggestRun({ activity: [{ stage: "result", suggested: null, note: "No suitable concept: Z94.81" }] }) });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    expect(await screen.findByText("No suitable concept: Z94.81")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByText("Z12.11"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    expect(within(screen.getByRole("dialog")).queryByText("No suitable concept: Z94.81")).not.toBeInTheDocument();
  });

  it("shows the suggestion method only in the dialog and clears it for another code", async () => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    mockPost.mockResolvedValueOnce({ data: suggestRun({ activity: [{ stage: "result", suggested: loincHit, strategy_used: "lexical" }] }) });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    const message = await screen.findByText("Winner filled in. You can choose another candidate before saving.");
    expect(screen.getByRole("dialog")).toContainElement(message);
    expect(screen.getAllByText("Winner filled in. You can choose another candidate before saving.")).toHaveLength(1);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Winner filled in. You can choose another candidate before saving.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Z12.11"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    expect(screen.queryByText("Winner filled in. You can choose another candidate before saving.")).not.toBeInTheDocument();
  });

  it.each([false, true])("ignores a late suggestion for a closed dialog (destination=%s)", async (found) => {
    renderPage([first, second]);
    fireEvent.click(await screen.findByText("Z94.81"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    let resolve!: (value: unknown) => void;
    mockPost.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" }));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByText("Z12.11"));
    await screen.findByRole("dialog");  // opening takes an edit lock first (#1440)
    resolve({ data: { suggested: found ? loincHit : null, note: "No suitable concept: Z94.81" } });
    await waitFor(() => expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Suggest" })).toBeEnabled());
    expect(screen.queryByText("No suitable concept: Z94.81")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Z12.11")).toBeInTheDocument();
    expect(screen.getByLabelText("Destination Concept ID")).toHaveValue(second.destination_concept_id);
  });
});

describe("server mapping pages", () => {
  // This block held one test and needed no reset; it holds several now, and an
  // unreset mockGet implementation leaks into the tests that follow.
  beforeEach(() => { mockGet.mockReset(); mockPost.mockReset(); mockPatch.mockReset(); });

  // #1575: Provenance became a filter instead of the default sort. A filter
  // finds curator-edited rows however many there are, and leaves the queue in
  // Seen order.
  const browseData = (overrides: Record<string, unknown> = {}) => ({
    results: [proposedRow], duplicates: [], selected_source: "",
    tabs: [{ vocabulary_id: "", label: "Uncoded", is_standard: false, proposed: 3, approved: 0, athena: 0 }],
    pages: { Proposed: { page: 1, page_size: 100, total: 3 }, Unmapped: { page: 1, page_size: 100, total: 0 }, Approved: { page: 1, page_size: 100, total: 0 }, Rejected: { page: 1, page_size: 100, total: 0 } },
    rejected_count: 0,
    provenances: [{ origin_system: "curator", count: 2 }, { origin_system: "", count: 1 }],
    selected_provenance: "",
    organizations: [],
    selected_organization: "",
    groups: {},
    rollup: false,
    ...overrides,
  });

  const entry = (over: Record<string, unknown> = {}) => ({
    label: "albumin", description: "Albumin", members: 2557, seen: 141875, proposed: 2557,
    destination_concept_id: null, destination_concept_name: null, destination_concept_code: null,
    destination_vocabulary_id: null, mixed_destinations: false, status: "proposed",
    mixed_statuses: false, mapping_id: null, ...over,
  });

  const renderBrowse = (overrides: Record<string, unknown> = {}) => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData(overrides) });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
  };

  it("shows the page shell while all initial requests are pending", () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    expect(screen.getByRole("heading", { name: "Code Mapping" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search mappings")).toBeInTheDocument();
    expect(screen.getByText("Loading mappings…")).toBeInTheDocument();
  });

  it("finishes an empty mapping load without waiting for reference or accuracy", async () => {
    mockGet.mockImplementation((url: string) => url === "/v1/code-mappings/"
      ? Promise.resolve({ data: [] }) : new Promise(() => {}));
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await waitFor(() => expect(screen.queryByText("Loading mappings…")).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Code Mapping" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps mapping rows usable when ancillary requests fail", async () => {
    mockGet.mockImplementation((url: string) => url === "/v1/code-mappings/"
      ? Promise.resolve({ data: browseData() }) : Promise.reject(new Error("ancillary unavailable")));
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByRole("button", { name: "Approve M-PROTEIN, SERUM" })).toBeEnabled();
    expect(screen.queryByText("Failed to load code mappings.")).not.toBeInTheDocument();
    expect(screen.getByText("Could not load mapping options. Refresh to retry.")).toBeInTheDocument();
  });

  it("checks compact duplicate warnings after rows arrive and loads full details on click", async () => {
    let finishAudit: (value: unknown) => void = () => {};
    const summaries = [proposedRow, { ...proposedRow, mapping_id: 999, status: "rejected" }].map((row) => ({
      mapping_id: row.mapping_id, source_code: row.source_code, source_vocabulary_id: row.source_vocabulary_id,
      destination_concept_id: row.destination_concept_id, status: row.status, summary: true,
    }));
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (config?.params?.duplicates_only) return new Promise((resolve) => { finishAudit = resolve; });
      if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({ duplicates_included: false }) });
      if (url === "/v1/code-mappings/999/") return Promise.resolve({ data: { ...proposedRow, mapping_id: 999, status: "rejected", notes: "Full notes" } });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByRole("button", { name: "Approve M-PROTEIN, SERUM" });
    expect(screen.queryByText("Loading mappings…")).not.toBeInTheDocument();
    expect(await screen.findByText("Checking duplicate source codes…")).toBeInTheDocument();
    finishAudit({ data: { duplicate_summaries: summaries } });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("1 duplicate source code on this tab");
    fireEvent.click(within(alert).getByRole("link", { name: /rejected.*#999/ }));
    await waitFor(() => expect(screen.getByLabelText("Notes")).toHaveValue("Full notes"));
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/999/");
  });

  it("requests rows for collapsed sections only when they are expanded", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url !== "/v1/code-mappings/") return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      const mapped = String(config?.params?.sections).includes("Approved");
      return Promise.resolve({ data: browseData({
        results: mapped ? [proposedRow, approvedRow] : [proposedRow],
        pages: { ...browseData().pages, Approved: { page: 1, page_size: 100, total: 1 } },
      }) });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByRole("button", { name: "Approve M-PROTEIN, SERUM" });
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ sections: "Unmapped,Proposed", include_duplicates: 0 }) });
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ sections: "Unmapped,Proposed,Approved" }) }));
    expect(await screen.findByText("Protein.monoclonal [Mass/volume] in Serum")).toBeInTheDocument();
  });

  it("defaults to Seen > 0 and resets pages when toggled", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url !== "/v1/code-mappings/") return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      const onlySeen = config?.params?.seen_only === "1";
      const page = Number(config?.params?.page_0 || 1);
      return Promise.resolve({ data: browseData({
        results: [{ ...proposedRow, source_code: onlySeen ? `SEEN PAGE ${page}` : "ZERO CODE", occurrence_count: onlySeen ? 1 : 0 }],
        total: onlySeen ? 101 : 500,
        pages: { ...browseData().pages, Unmapped: { page, page_size: 100, total: onlySeen ? 101 : 500 } },
      }) });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByText("SEEN PAGE 1");
    const filter = screen.getByRole("checkbox", { name: "Unmapped: only codes with Seen greater than zero" });
    expect(filter).toBeChecked();
    expect(filter.closest("th")).toBe(screen.getByTitle("Sort Unmapped by Seen").closest("th"));
    expect(screen.getByTestId("all-mappings-count")).toHaveTextContent("(101)");
    fireEvent.click(filter);
    await screen.findByText("ZERO CODE");
    expect(screen.getByTestId("all-mappings-count")).toHaveTextContent("(500)");
    fireEvent.click(filter);
    await screen.findByText("SEEN PAGE 1");
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ seen_only: "1", page_0: 1 }) });
  });

  it("filters to codes with a curator note, off by default, and marks them (#2423)", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url !== "/v1/code-mappings/") return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      const noted = { ...proposedRow, source_code: "NOTED CODE", notes: "Ask the lab about the specimen.", has_curator_note: true };
      const machine = { ...proposedRow, mapping_id: 9901, source_code: "MACHINE CODE", notes: "high confidence: same test.", has_curator_note: false };
      const results = config?.params?.with_notes === "1" ? [noted] : [noted, machine];
      return Promise.resolve({ data: browseData({
        results, total: results.length,
        pages: { ...browseData().pages, Unmapped: { page: 1, page_size: 100, total: results.length } },
      }) });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByText("MACHINE CODE");
    const filter = screen.getByRole("checkbox", { name: "Unmapped: only codes with a curator note" });
    expect(filter).not.toBeChecked();
    expect(mockGet).not.toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ with_notes: "1" }) });
    // Suggest's rationale sits in the same field and gets no sticky note.
    const marks = screen.getAllByTestId("curator-note");
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveAttribute("title", "Ask the lab about the specimen.");
    expect(marks[0].closest("tr")).toHaveTextContent("NOTED CODE");
    fireEvent.click(filter);
    await waitFor(() => expect(screen.queryByText("MACHINE CODE")).not.toBeInTheDocument());
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({ with_notes: "1", page_0: 1 }) });
  });

  it("shows and filters hospital organization on vendor tabs", async () => {
    const epicRow = {
      ...proposedRow,
      source_vocabulary_id: "http://open.epic.com/FHIR/StructureDefinition/observation-flowsheet-id",
      source_code: "10627",
      organization_id: 41,
      organization_slug: "hospital-a",
      organization_name: "Hospital A",
    };
    mockGet.mockImplementation((url: string) => {
      if (url !== "/v1/code-mappings/") {
        return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      }
      return Promise.resolve({ data: browseData({
        results: [epicRow], selected_source: "EPIC",
        tabs: [{ vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 1, approved: 0, athena: 0 }],
        organizations: [
          { organization_id: 41, slug: "hospital-a", name: "Hospital A", count: 1 },
          { organization_id: 42, slug: "hospital-b", name: "Hospital B", count: 1 },
        ],
      }) });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);

    await screen.findByText("10627");
    fireEvent.change(screen.getByRole("combobox", { name: "Filter by organization" }), {
      target: { value: "hospital-a" },
    });
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/code-mappings/",
      { params: expect.objectContaining({ organization: "hospital-a" }) },
    ));
  });

  it("shows observed hospital context while retaining the exact Epic system URI", async () => {
    // The system link opens Source Systems management, a curation page (#2398).
    mockUseAuth.mockReturnValue({ currentUser: { id: 1, is_staff: true } });
    const oid = "urn:oid:1.2.840.114350.1.13.88.2.7.5.737384.2050";
    const context: SourceSystemContext = {
      kind: "observed", label: "UPMC", names: ["UPMC"], name_count: 1,
    };
    renderPage([{
      ...proposedRow,
      source_vocabulary_id: oid,
      source_code: "LOCAL-1826",
      source_system_context: context,
      source_evidence: {
        organization: null, occurrence_count: 14, facilities: [{ name: "UPMC", level: "brand", confidence: "high" }],
        metadata: {}, units: [], system_context: context,
      },
    }]);
    const code = await screen.findByText("LOCAL-1826", { selector: '[data-testid=source-code]' });
    fireEvent.click(code.closest("tr")!);
    const picker = await screen.findByLabelText("Source Code System") as HTMLSelectElement;
    expect(picker.value).toBe(oid);
    expect(picker.selectedOptions[0].textContent).toContain("UPMC");
    expect(screen.getByText(oid, { selector: "code" })).toBeInTheDocument();
    const systemLink = screen.getByRole("link", { name: oid });
    expect(systemLink).toHaveAttribute("href", `/code-mappings/source-systems?system=${encodeURIComponent(oid)}`);
    expect(systemLink).toHaveAttribute("title", expect.stringContaining("review and manage facilities"));
    expect(screen.queryByRole("link", { name: "Manage hospitals for this source code system" })).not.toBeInTheDocument();
    const facilityLink = screen.getByRole("link", { name: "UPMC" });
    expect(facilityLink).toHaveAttribute("href", `/code-mappings/source-systems?system=${encodeURIComponent(oid)}&facility=UPMC`);
  });

  it("counts all matching sources even when the page contains one label group", async () => {
    renderBrowse({ total: 2557, results: [], rollup: true, groups: { Unmapped: [entry()] },
      pages: { ...browseData().pages, Unmapped: { page: 1, page_size: 100, total: 1 } } });
    await screen.findByText("Albumin");
    expect(screen.getByTestId("all-mappings-count")).toHaveTextContent("(2557)");
    expect(screen.getByRole("button", { name: "Unmapped(1 groups)" })).toBeInTheDocument();
    expect(screen.getByText("Downloads include loaded rows only.")).toBeInTheDocument();
  });

  it("passes Seen filtering to group expansion and clears cached members on a toggle", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url.includes("/group/")) return Promise.resolve({ data: {
        results: [{ ...proposedRow, source_code: config?.params?.seen_only === "1" ? "SEEN MEMBER" : "ALL MEMBER" }],
      } });
      if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({ rollup: true, groups: { Unmapped: [entry()] } }) });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Expand Albumin" }));
    await screen.findByText("SEEN MEMBER");
    fireEvent.click(screen.getByRole("checkbox", { name: "Unmapped: only codes with Seen greater than zero" }));
    await waitFor(() => expect(screen.queryByText("SEEN MEMBER")).not.toBeInTheDocument());
    // The toggle reloads the list, which can re-render the group collapsed
    // after the first click lands; expand again until the members show.
    await waitFor(() => {
      const expand = screen.queryByRole("button", { name: "Expand Albumin" });
      if (expand) fireEvent.click(expand);
      expect(screen.getByText("ALL MEMBER")).toBeInTheDocument();
    }, { timeout: 5000 });
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/group/", { params: expect.objectContaining({ seen_only: "0" }) });
  });

  it("filters by provenance, labelling the blank value and showing counts", async () => {
    renderBrowse();
    const filter = await screen.findByLabelText("Filter by provenance");
    expect(Array.from(filter.querySelectorAll("option")).map((o) => o.textContent)).toEqual([
      "All provenance", "curator (2)", "No provenance (1)",
    ]);
    fireEvent.change(filter, { target: { value: "curator" } });
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.objectContaining({ provenance: "curator" }),
    }));
  });

  // Blank is not a corner case: enqueue_unmapped_source_codes writes
  // origin_system='' for every newly queued code, so it is the largest cohort
  // on a freshly enqueued tab. "" is already the select's "no filter" value,
  // so it needs a sentinel of its own or the option is unselectable and filters
  // nothing.
  it("filters to the blank provenance through a distinct sentinel value", async () => {
    renderBrowse();
    const filter = await screen.findByLabelText("Filter by provenance") as HTMLSelectElement;
    const values = Array.from(filter.querySelectorAll("option")).map((o) => o.getAttribute("value"));
    expect(values).toEqual(["", "curator", "__blank__"]);
    expect(new Set(values).size).toBe(values.length);
    fireEvent.change(filter, { target: { value: "__blank__" } });
    expect(filter.value).toBe("__blank__");
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.objectContaining({ provenance: "__blank__" }),
    }));
  });

  it("keeps a filter clearable after the rows carrying it are gone", async () => {
    // The curator filters to a value, works through those rows, and the next
    // refresh no longer offers it. Without an escape hatch the queue reads as
    // empty with no control left to clear.
    let payload = browseData();
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") return Promise.resolve({ data: payload });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const filter = await screen.findByLabelText("Filter by provenance") as HTMLSelectElement;
    // Those rows are worked off, so the next refresh stops offering the value.
    payload = browseData({ provenances: [{ origin_system: "HT-One", count: 9 }] });
    fireEvent.change(filter, { target: { value: "curator" } });
    await waitFor(() => expect(
      Array.from(screen.getByLabelText("Filter by provenance").querySelectorAll("option")).map((o) => o.textContent),
    ).toContain("curator (0)"));
    const after = screen.getByLabelText("Filter by provenance") as HTMLSelectElement;
    expect(after.value).toBe("curator");
    // Clearing it works, and the control then retires on its own: one
    // provenance left and nothing filtered means nothing to offer.
    fireEvent.change(after, { target: { value: "" } });
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.not.objectContaining({ provenance: expect.anything() }),
    }));
    expect(screen.queryByLabelText("Filter by provenance")).not.toBeInTheDocument();
  });

  it("omits the filter when a tab has nothing to filter by and nothing is set", async () => {
    renderBrowse({ provenances: [{ origin_system: "HT-One", count: 9 }] });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(screen.queryByLabelText("Filter by provenance")).not.toBeInTheDocument();
  });

  it("sends no provenance param until one is chosen", async () => {
    renderBrowse();
    await screen.findByLabelText("Filter by provenance");
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.not.objectContaining({ provenance: expect.anything() }),
    });
  });

  // #1571: one review row per label. albumin arrives under 2,557 vendor codes,
  // so a per-code queue asks the same question 2,557 times and scatters the
  // volume so the queue cannot be ordered usefully.
  it("groups by default", async () => {
    renderBrowse();
    await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.objectContaining({ rollup: 1 }),
    });
  });

  // A wearable metric is one code per label, so the Wearables tab opens
  // ungrouped; every other tab still opens grouped.
  const tabsWithWearables = [
    { vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 3, approved: 0, athena: 0 },
    { vocabulary_id: "OpenWearables", label: "Wearables", is_standard: false, proposed: 3, approved: 0, athena: 0 },
  ];
  const browseCalls = (source: string) => mockGet.mock.calls
    .filter(([url, config]) => url === "/v1/code-mappings/" && config?.params?.source === source)
    .map(([, config]) => config.params as Record<string, unknown>);

  it("treats a server-sent HL7-PHR row as the Wearables tab's own, in search and in its counts", async () => {
    // Browse mode, as production runs: the server files the row under
    // OpenWearables, and the page must agree which tab that is.
    mockUseAuth.mockReturnValue({ currentUser: { id: 1, is_staff: true } });
    const phr = { ...proposedRow, mapping_id: 303, source_vocabulary_id: "HL7-PHR", source_code: "phr_steps", source_code_description: "Steps" };
    let saved = false;
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        // After the save the refresh stays pending, so the counts on screen
        // are the page's own arithmetic.
        return saved ? new Promise(() => {}) : Promise.resolve({ data: browseData({
          tabs: tabsWithWearables, selected_source: "OpenWearables", results: [phr],
          pages: { ...browseData().pages, Proposed: { page: 1, page_size: 100, total: 1 } },
        }) });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    mockPatch.mockImplementation(() => { saved = true; return Promise.resolve({ data: { ...phr, status: "approved" } }); });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const code = await screen.findByText("phr_steps", { selector: '[data-testid=source-code]' });
    const wearables = screen.getByRole("tab", { name: /Wearables/ });
    expect(wearables).toHaveAttribute("aria-selected", "true");
    expect(wearables).toHaveTextContent("Wearables3");
    // Its system is named as the tab, not as a vocabulary of its own.
    expect(code.closest("td")).toHaveTextContent("Wearables");

    fireEvent.change(screen.getByRole("textbox", { name: "Search mappings" }), { target: { value: "steps" } });
    expect(await screen.findByText(/Searching all coding systems — every match is on this tab\./)).toBeInTheDocument();
    expect(screen.queryByText(/from other tabs/)).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Approve phr_steps" }));
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/303/", expect.objectContaining({ status: "approved" })));
    await waitFor(() => expect(wearables).toHaveTextContent("Wearables2"));
    expect(screen.getByRole("tab", { name: /Epic/ })).toHaveTextContent("Epic3");
  });

  it("opens the Wearables tab ungrouped and the other tabs grouped", async () => {
    renderBrowse({ tabs: tabsWithWearables, selected_source: "EPIC" });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(screen.getByLabelText("Group by label")).toBeChecked();

    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(browseCalls("OpenWearables")).not.toHaveLength(0));
    browseCalls("OpenWearables").forEach((params) => expect(params).not.toHaveProperty("rollup"));
    expect(screen.getByLabelText("Group by label")).not.toBeChecked();

    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(browseCalls("EPIC")).not.toHaveLength(0));
    browseCalls("EPIC").forEach((params) => expect(params).toHaveProperty("rollup", 1));
    expect(screen.getByLabelText("Group by label")).toBeChecked();
  });

  it("groups the Wearables tab once the curator ticks the box, and keeps the choice", async () => {
    renderBrowse({ tabs: tabsWithWearables, selected_source: "EPIC" });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(screen.getByLabelText("Group by label")).not.toBeChecked());

    fireEvent.click(screen.getByLabelText("Group by label"));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.objectContaining({ source: "OpenWearables", rollup: 1 }),
    }));

    // An explicit choice is the curator's, on every tab, as it was before.
    fireEvent.click(screen.getByLabelText("Group by label"));
    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(browseCalls("EPIC")).not.toHaveLength(0));
    browseCalls("EPIC").forEach((params) => expect(params).not.toHaveProperty("rollup"));
  });

  it("opens ungrouped when the server opens the page on the Wearables tab", async () => {
    renderBrowse({ tabs: tabsWithWearables, selected_source: "OpenWearables" });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    await waitFor(() => expect(screen.getByLabelText("Group by label")).not.toBeChecked());
    await waitFor(() => {
      const last = mockGet.mock.calls.filter(([url]) => url === "/v1/code-mappings/").at(-1);
      expect(last?.[1].params).not.toHaveProperty("rollup");
    });
  });

  it("drops a sort a grouped queue cannot do when a tab's default groups it again", async () => {
    // Sorting by Source exists only ungrouped; sent with rollup=1 it is a 400.
    renderBrowse({ tabs: tabsWithWearables, selected_source: "EPIC" });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(browseCalls("OpenWearables")).not.toHaveLength(0));
    const table = await screen.findByRole("table", { name: "Proposed mappings" });
    fireEvent.click(within(table).getByRole("button", { name: /^Source/ }));
    await waitFor(() => expect(browseCalls("OpenWearables").at(-1)?.order_1).toMatch(/source_code$/));

    mockGet.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(browseCalls("EPIC")).not.toHaveLength(0));
    browseCalls("EPIC").forEach((params) => {
      expect(params).toHaveProperty("rollup", 1);
      expect(params.order_1).toBe("-occurrence_count");
    });
  });

  it("sends a grouped queue's sort when the server moves the page onto a grouped tab", async () => {
    // No tab is clicked, so the server names the tab on every response. When a
    // refresh comes back on another tab the queue groups again by default.
    let selected = "OpenWearables";
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ tabs: tabsWithWearables, selected_source: selected }) });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const lastBrowse = () => mockGet.mock.calls.filter(([url]) => url === "/v1/code-mappings/").at(-1)?.[1].params;
    await waitFor(() => expect(screen.getByLabelText("Group by label")).not.toBeChecked());
    await waitFor(() => expect(lastBrowse()).not.toHaveProperty("rollup"));

    selected = "EPIC";
    const table = await screen.findByRole("table", { name: "Proposed mappings" });
    fireEvent.click(within(table).getByRole("button", { name: /^Source/ }));
    await waitFor(() => expect(lastBrowse()).toHaveProperty("rollup", 1));
    expect(lastBrowse().order_1).toBe("-occurrence_count");
  });

  it("shows a Suggest run's progress on the Wearables tab, and not its candidates", async () => {
    const activity = [{ stage: "candidates", mapping_id: proposedRow.mapping_id, source_code: proposedRow.source_code, strategy: "umls",
      candidates: [{ concept_id: 1, concept_name: "Live candidate", concept_code: "A", vocabulary_id: "SNOMED" }] }];
    mockPost.mockResolvedValue({ data: { ...suggestRun({ state: "running", total: 4, retrieved: 1 }), activity } });
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/latest/")) return Promise.resolve({ data: { run_id: null } });
      if (url.startsWith("/v1/code-mappings/suggest-runs/")) {
        return Promise.resolve({ data: { ...suggestRun({ state: "running", total: 4, retrieved: 2 }), activity } });
      }
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ tabs: tabsWithWearables, selected_source: "EPIC" }) });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(browseCalls("OpenWearables")).not.toHaveLength(0));

    await waitFor(() => expect(screen.getByRole("button", { name: "Suggest" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Suggest" }));
    await waitFor(() => expect(screen.getByTestId("suggest-progress")).toHaveTextContent("of 4"));
    expect(screen.queryByRole("region", { name: "Suggestion candidates" })).not.toBeInTheDocument();
    expect(screen.queryByText("Live candidate")).not.toBeInTheDocument();
  });

  it("shows no Suggestion candidates panel on the Wearables tab", async () => {
    const latest = {
      run_id: "restored", state: "success",
      activity: [{ stage: "candidates", mapping_id: proposedRow.mapping_id, source_code: proposedRow.source_code, strategy: "umls",
        candidates: [{ concept_id: 1, concept_name: "Restored candidate", concept_code: "A", vocabulary_id: "SNOMED" }] }],
    };
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/latest/")) return Promise.resolve({ data: latest });
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ tabs: tabsWithWearables, selected_source: "EPIC" }) });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByRole("region", { name: "Suggestion candidates" })).toBeInTheDocument();

    mockGet.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(browseCalls("OpenWearables")).not.toHaveLength(0));
    expect(screen.queryByRole("region", { name: "Suggestion candidates" })).not.toBeInTheDocument();
    // Nothing to restore into, so the last run is not asked for either.
    expect(mockGet.mock.calls.some(([url]) => String(url).includes("/latest/"))).toBe(false);

    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    expect(await screen.findByRole("region", { name: "Suggestion candidates" })).toBeInTheDocument();
  });

  it("asks the server to group and shows one row for the label", async () => {
    let payload = browseData();
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") return Promise.resolve({ data: payload });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByRole("table", { name: "Unmapped mappings" });
    payload = browseData({ rollup: true, groups: { Unmapped: [entry()] } });
    fireEvent.click(screen.getByLabelText("Group by label"));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", {
      params: expect.objectContaining({ rollup: 1 }),
    }));
    const table = await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(within(table).getByText("2,557 codes")).toBeInTheDocument();
    expect(within(table).getByText("141,875")).toBeInTheDocument();
    expect(within(table).getByText("Albumin")).toBeInTheDocument();
  });

  describe("destination unit column", () => {
    const unitCell = async (section = "Proposed") =>
      within(await screen.findByRole("table", { name: `${section} mappings` })).getByTestId("destination-unit");

    it("has its own header, told apart from the source Units", async () => {
      renderBrowse();
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      const header = within(table).getByRole("columnheader", { name: "Destination unit" });
      expect(header).toHaveTextContent("Dest. unit");
      // The short form is spelled out for a mouse and for a screen reader.
      expect(header.querySelector("abbr")).toHaveAttribute("title", "Destination unit");
      expect(within(table).getByRole("columnheader", { name: "Units" })).toBeInTheDocument();
    });

    it("says the suggested unit once: in its column, not again under the destination", async () => {
      renderBrowse({ results: [{ ...proposedRow, measurement_type: "quantitative", suggested_unit: "mg/dL", example_units: ["mg/dL", "g/L"] }] });
      const cell = await unitCell();
      expect(cell).toHaveTextContent("mg/dL suggested");
      const row = cell.closest("tr")!;
      // Measurement type and example units stay with the destination.
      expect(within(row).getByText("Quantitative · Example units: mg/dL, g/L")).toBeInTheDocument();
      expect(row).not.toHaveTextContent("Suggested unit:");
    });

    it("scrolls sideways rather than clip its last column on a narrow page", async () => {
      renderBrowse();
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      expect(table.parentElement!.className).toMatch(/overflow-x-auto/);
      expect(table.parentElement!.className).not.toMatch(/overflow-hidden/);
    });

    it("shows the mapping's curated unit, named on hover, over a suggested one", async () => {
      renderBrowse({ results: [{ ...proposedRow, destination_unit_concept_id: 9546, destination_unit_code: "m",
        destination_unit_name: "meter", suggested_unit: "cm" }] });
      const cell = await unitCell();
      expect(cell).toHaveTextContent(/^m$/);
      expect(within(cell).getByText("m")).toHaveAttribute("title", "meter");
    });

    it("falls back to the destination's suggested unit, marked as suggested", async () => {
      renderBrowse({ results: [{ ...proposedRow, destination_unit_concept_id: null, destination_unit_code: "",
        destination_unit_name: "", suggested_unit: "mg/dL" }] });
      const cell = await unitCell();
      expect(cell).toHaveTextContent("mg/dL suggested");
      expect(within(cell).getByText("mg/dL").parentElement).toHaveAttribute("title",
        "LOINC example unit for the destination concept. No destination unit is set on this mapping.");
      expect(within(cell).getByText("mg/dL").parentElement?.className).toMatch(/text-slate-500/);
    });

    it("shows a dash when the mapping has no unit and none is suggested", async () => {
      renderBrowse();
      expect(await unitCell()).toHaveTextContent(/^—$/);
    });

    it("says Mixed for a group whose members disagree on the unit", async () => {
      renderBrowse({ results: [], rollup: true, groups: { Unmapped: [entry({
        mixed_destination_units: true, destination_unit_code: "", suggested_unit: "g/dL",
      })] } });
      expect(await unitCell("Unmapped")).toHaveTextContent(/^Mixed$/);
    });

    it("shows a group's unit when every member agrees, else its suggested one", async () => {
      renderBrowse({ results: [], rollup: true, groups: { Unmapped: [
        entry({ destination_unit_code: "g/dL", destination_unit_name: "gram per deciliter", mixed_destination_units: false }),
        entry({ label: "ferritin", description: "Ferritin", destination_unit_code: "", suggested_unit: "ng/mL" }),
        entry({ label: "sodium", description: "Sodium" }),
      ] } });
      const table = await screen.findByRole("table", { name: "Unmapped mappings" });
      const cells = within(table).getAllByTestId("destination-unit");
      expect(cells.map(cell => cell.textContent)).toEqual(["g/dL", "ng/mL suggested", "—"]);
      expect(within(cells[0]).getByText("g/dL")).toHaveAttribute("title", "gram per deciliter");
    });

    it("shows the unit on a group's expanded member rows", async () => {
      mockGet.mockImplementation((url: string) => {
        if (url.includes("/group/")) return Promise.resolve({ data: {
          results: [{ ...proposedRow, source_code: "MEMBER-1", destination_unit_code: "g/L", destination_unit_name: "gram per liter" }],
        } });
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({ results: [], rollup: true, groups: { Unmapped: [entry()] } }) });
        return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      });
      render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
      fireEvent.click(await screen.findByRole("button", { name: "Expand Albumin" }));
      const member = (await screen.findByText("MEMBER-1", { selector: '[data-testid=source-code]' })).closest("tr")!;
      expect(within(member).getByTestId("destination-unit")).toHaveTextContent(/^g\/L$/);
      // The member row lines up under the group row, cell for cell.
      const group = screen.getByRole("button", { name: "Collapse Albumin" }).closest("tr")!;
      expect(member.querySelectorAll("td")).toHaveLength(group.querySelectorAll("td").length);
    });
  });

  describe("Category and Metric columns", () => {
    const wearableRow = (over: Record<string, unknown> = {}) => ({
      ...proposedRow, mapping_id: 501, source_vocabulary_id: "OpenWearables",
      source_code: "resting_heart_rate", source_code_description: "Resting heart rate (bpm)",
      category: "Heart & Cardiovascular", measure_class: "Aggregate over interval",
      metric_key: "resting_hr", ...over,
    });
    const onWearables = (overrides: Record<string, unknown> = {}) => renderBrowse({
      tabs: tabsWithWearables, selected_source: "OpenWearables", results: [wearableRow()], ...overrides,
    });
    const headerNames = (table: HTMLElement) => within(table).getAllByRole("columnheader")
      .map((header) => header.getAttribute("aria-label") ?? header.textContent?.replace(/[↕↑↓]/g, "").trim());
    // The server opened the page on the tab, so no request names a source.
    const lastBrowse = () => mockGet.mock.calls
      .filter(([url]) => url === "/v1/code-mappings/").at(-1)?.[1].params as Record<string, unknown>;
    // Columns a row spans: Qty and Units share one cell.
    const columnsSpanned = (row: Element) => Array.from(row.querySelectorAll("td"))
      .reduce((total, cell) => total + (cell as HTMLTableCellElement).colSpan, 0);

    it("leads the Wearables tab with Category, then Metric", async () => {
      onWearables();
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      await waitFor(() => expect(headerNames(table).slice(0, 3)).toEqual(["Category", "Metric", "Source"]));
      expect(headerNames(table)).toHaveLength(10);

      const row = within(table).getByText("resting_heart_rate", { selector: "[data-testid=source-code]" }).closest("tr")!;
      const cells = within(row).getAllByRole("cell");
      expect(columnsSpanned(row)).toBe(10);
      // First cell: the category, and the measure class as its muted second line.
      expect(cells[0]).toHaveAttribute("data-testid", "category");
      expect(within(cells[0]).getByText("Heart & Cardiovascular")).toBeInTheDocument();
      const measureClass = within(cells[0]).getByTestId("measure-class");
      expect(measureClass).toHaveTextContent(/^Aggregate over interval$/);
      expect(measureClass.className).toMatch(/text-slate-500/);
      expect(cells[0].firstElementChild).toHaveTextContent(/^Heart & Cardiovascular$/);
      expect(cells[0].lastElementChild).toBe(measureClass);
      // Second cell: the key, in the mono style the source code uses.
      expect(cells[1]).toHaveAttribute("data-testid", "metric-key");
      expect(cells[1]).toHaveTextContent(/^resting_hr$/);
      expect(within(cells[1]).getByText("resting_hr").className).toMatch(/font-mono/);
      // The source code is still where it was, one column further along.
      expect(cellUnder(row, "Source")).toHaveTextContent("resting_heart_rate");
      // Measure Class has no column of its own.
      expect(headerNames(table)).not.toContain("Measure Class");
    });

    it("shows a dash for a blank category or metric key, and no second line", async () => {
      onWearables({ results: [wearableRow({ category: "", measure_class: "", metric_key: "" })] });
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      const category = await within(table).findByTestId("category");
      expect(category).toHaveTextContent(/^—$/);
      expect(within(category).queryByTestId("measure-class")).not.toBeInTheDocument();
      expect(within(table).getByTestId("metric-key")).toHaveTextContent(/^—$/);
    });

    it("shows the measure class under a blank category", async () => {
      onWearables({ results: [wearableRow({ category: "" })] });
      const category = await screen.findByTestId("category");
      expect(category).toHaveTextContent("—Aggregate over interval");
    });

    it("has neither column on any other tab", async () => {
      renderBrowse({ results: [{ ...proposedRow, category: "Sleep", measure_class: "Vendor score", metric_key: "sleep" }] });
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      await screen.findByText("M-PROTEIN, SERUM", { selector: "[data-testid=source-code]" });
      expect(headerNames(table)).toEqual(
        ["Source", "Domain", "Seen", "Qty", "Units", "Destination", "Destination unit", "Actions"]);
      expect(screen.queryByTestId("category")).not.toBeInTheDocument();
      expect(screen.queryByTestId("metric-key")).not.toBeInTheDocument();
      const row = within(table).getByText("M-PROTEIN, SERUM", { selector: "[data-testid=source-code]" }).closest("tr")!;
      expect(columnsSpanned(row)).toBe(8);
      expect(row.querySelector("td")).toContainElement(within(row).getByTestId("source-code"));
      expect(within(table).queryByText("Sleep")).not.toBeInTheDocument();
      // An empty section's placeholder still spans the eight columns.
      const empty = within(await screen.findByRole("table", { name: "Unmapped mappings" })).getByRole("cell");
      expect(empty).toHaveAttribute("colspan", "8");
    });

    it("spans both extra columns in an empty section on the Wearables tab", async () => {
      onWearables();
      await screen.findByTestId("category");
      const empty = within(screen.getByRole("table", { name: "Unmapped mappings" })).getByRole("cell");
      expect(empty).toHaveAttribute("colspan", "10");
    });

    it("sorts by Category and by Metric, and drops the sort on leaving the tab", async () => {
      onWearables();
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      fireEvent.click(await within(table).findByRole("button", { name: /^Category/ }));
      await waitFor(() => expect(lastBrowse().order_1).toBe("category"));
      expect(within(table).getByRole("columnheader", { name: /Category/ })).toHaveAttribute("aria-sort", "ascending");
      fireEvent.click(within(table).getByRole("button", { name: /^Category/ }));
      await waitFor(() => expect(lastBrowse().order_1).toBe("-category"));
      fireEvent.click(within(table).getByRole("button", { name: /^Metric/ }));
      await waitFor(() => expect(lastBrowse().order_1).toBe("metric_key"));
      expect(within(table).getByRole("columnheader", { name: /Metric/ })).toHaveAttribute("aria-sort", "ascending");

      // No other tab has the column, so the sort does not follow the curator there.
      mockGet.mockClear();
      fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
      await waitFor(() => expect(browseCalls("EPIC")).not.toHaveLength(0));
      browseCalls("EPIC").forEach((params) => expect(params.order_1).toBe("-occurrence_count"));
    });

    it("stops sending a Category sort when the server moves the page off the Wearables tab", async () => {
      // No tab was clicked: the server names the tab on every response, and
      // its default can change under a curator who sorted by Category.
      let selected = "OpenWearables";
      mockGet.mockImplementation((url: string) => {
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({
          tabs: tabsWithWearables, selected_source: selected, results: [wearableRow()] }) });
        return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      });
      render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      fireEvent.click(await within(table).findByRole("button", { name: /^Metric/ }));
      await waitFor(() => expect(lastBrowse().order_1).toBe("metric_key"));
      expect(lastBrowse()).not.toHaveProperty("source");

      selected = "EPIC";
      // Any refresh will do; this one touches no sort.
      fireEvent.click(within(table).getByLabelText("Proposed: only codes with a curator note"));
      // The tab the answer names has no Metric column, so the sort goes with it.
      await waitFor(() => expect(headerNames(screen.getByRole("table", { name: "Proposed mappings" }))).not.toContain("Metric"));
      await waitFor(() => expect(lastBrowse().order_1).toBe("-occurrence_count"));
      expect(lastBrowse().order_0).toBe("-occurrence_count");
      const proposed = screen.getByRole("table", { name: "Proposed mappings" });
      expect(headerNames(proposed)).not.toContain("Category");
      // The header that is sorted is one the table has.
      expect(within(proposed).getByRole("columnheader", { name: "Seen" })).toHaveAttribute("aria-sort", "descending");
    });

    it("puts the three fields in the CSV download", async () => {
      let saved: Blob | null = null;
      const urls = URL as unknown as { createObjectURL: unknown; revokeObjectURL: unknown };
      const original = { create: urls.createObjectURL, revoke: urls.revokeObjectURL };
      urls.createObjectURL = vi.fn((blob: Blob) => { saved = blob; return "blob:code-mappings"; });
      urls.revokeObjectURL = vi.fn();
      const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
      try {
        onWearables({ results: [wearableRow(), wearableRow({
          mapping_id: 502, source_code: "hydration", category: "", measure_class: "", metric_key: "" })] });
        await screen.findAllByTestId("category");
        fireEvent.click(screen.getByTitle("Download Proposed"));
        fireEvent.click(screen.getByRole("button", { name: "CSV" }));
        expect(click).toHaveBeenCalledTimes(1);
        expect(saved).not.toBeNull();
        const text = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.readAsText(saved!);
        });
        const [header, first, second] = text.split("\n").map((line) => line.split(","));
        for (const column of ["category", "measure_class", "metric_key"]) expect(header).toContain(column);
        const cell = (line: string[], column: string) => line[header.indexOf(column)];
        expect(cell(first, "source_code")).toBe("resting_heart_rate");
        expect(cell(first, "category")).toBe("Heart & Cardiovascular");
        expect(cell(first, "measure_class")).toBe("Aggregate over interval");
        expect(cell(first, "metric_key")).toBe("resting_hr");
        expect([cell(second, "category"), cell(second, "measure_class"), cell(second, "metric_key")]).toEqual(["", "", ""]);
      } finally {
        click.mockRestore();
        urls.createObjectURL = original.create;
        urls.revokeObjectURL = original.revoke;
      }
    });

    it("keeps a Category sort when the Wearables tab is grouped", async () => {
      onWearables();
      const table = await screen.findByRole("table", { name: "Proposed mappings" });
      fireEvent.click(await within(table).findByRole("button", { name: /^Category/ }));
      await waitFor(() => expect(lastBrowse().order_1).toBe("category"));
      fireEvent.click(screen.getByLabelText("Group by label"));
      await waitFor(() => expect(lastBrowse()).toMatchObject({ rollup: 1, order_1: "category" }));
    });

    it("shows a group's category, measure class and metric key, and lines members up under it", async () => {
      mockGet.mockImplementation((url: string) => {
        if (url.includes("/group/")) return Promise.resolve({ data: {
          results: [wearableRow({ source_code: "MEMBER-1" })],
        } });
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({
          tabs: tabsWithWearables, selected_source: "OpenWearables", results: [], rollup: true,
          groups: { Unmapped: [
            entry({ label: "restingheartrate", description: "Resting heart rate", members: 2,
              category: "Heart & Cardiovascular", measure_class: "Aggregate over interval", metric_key: "resting_hr" }),
            // Members that disagree name none of the three.
            entry({ label: "steps", description: "Steps", category: "", measure_class: "", metric_key: "" }),
          ] },
        }) });
        return Promise.resolve({ data: url.includes("reference") ? reference : {} });
      });
      render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
      const table = await screen.findByRole("table", { name: "Unmapped mappings" });
      const categories = await within(table).findAllByTestId("category");
      expect(categories.map((cell) => cell.textContent)).toEqual(
        ["Heart & CardiovascularAggregate over interval", "—"]);
      expect(within(table).getAllByTestId("metric-key").map((cell) => cell.textContent)).toEqual(["resting_hr", "—"]);
      // A grouped queue can sort by both.
      expect(within(table).getByRole("button", { name: /^Category/ })).toBeInTheDocument();
      expect(within(table).getByRole("button", { name: /^Metric/ })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Expand Resting heart rate" }));
      const member = (await screen.findByText("MEMBER-1", { selector: "[data-testid=source-code]" })).closest("tr")!;
      const group = screen.getByRole("button", { name: "Collapse Resting heart rate" }).closest("tr")!;
      expect(columnsSpanned(member)).toBe(10);
      expect(columnsSpanned(group)).toBe(10);
      expect(member.querySelectorAll("td")).toHaveLength(group.querySelectorAll("td").length);
      expect(within(member).getByTestId("category")).toHaveTextContent("Heart & CardiovascularAggregate over interval");
      expect(within(member).getByTestId("metric-key")).toHaveTextContent(/^resting_hr$/);
      expect(member.querySelector("td")).toHaveAttribute("data-testid", "category");
    });

    it("opens the dialog with the row's three fields and saves a changed category", async () => {
      // What the server holds: the list is re-read after a save.
      let held = wearableRow();
      mockPatch.mockImplementation((_url: string, payload: Record<string, unknown>) => {
        held = wearableRow({ category: payload.category });
        return Promise.resolve({ data: held });
      });
      mockGet.mockImplementation((url: string) => {
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: browseData({
          tabs: tabsWithWearables, selected_source: "OpenWearables", results: [held] }) });
        if (url === "/v1/code-mappings/501/") return Promise.resolve({ data: { ...held, destination_options: [] } });
        return Promise.resolve({ data: url.includes("reference") ? {
          ...reference,
          wearable_metric_categories: ["Activity", "Heart & Cardiovascular", "Sleep"],
          wearable_measure_classes: ["Direct measurement", "Aggregate over interval"],
          wearable_source_vocabularies: ["Apple", "Garmin", "HL7-PHR", "HealthConnect", "OpenWearables"],
        } : {} });
      });
      render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
      fireEvent.click((await screen.findByText("resting_heart_rate", { selector: "[data-testid=source-code]" })).closest("tr")!);
      const dialog = within(await screen.findByRole("dialog"));
      const category = await dialog.findByLabelText("Category");
      expect(category).toHaveValue("Heart & Cardiovascular");
      expect(dialog.getByLabelText("Measure Class")).toHaveValue("Aggregate over interval");
      expect(dialog.getByLabelText("Metric Key")).toHaveValue("resting_hr");
      fireEvent.change(category, { target: { value: "Sleep" } });
      fireEvent.click(dialog.getByRole("button", { name: "Update Mapping" }));
      await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/501/",
        expect.objectContaining({ category: "Sleep", decision_surface: "code2concept" })));
      expect(mockPatch.mock.calls.at(-1)![1]).not.toHaveProperty("measure_class");
      expect(mockPatch.mock.calls.at(-1)![1]).not.toHaveProperty("metric_key");
      // The list shows what was saved.
      await waitFor(() => expect(screen.getByTestId("category")).toHaveTextContent("Sleep"));
    });
  });

  it("names no concept for a group whose members disagree", async () => {
    renderBrowse({ rollup: true, groups: { Unmapped: [entry({
      mixed_destinations: true, destination_concept_id: null, destination_concept_name: null,
    })] } });
    const table = await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(within(table).getByText("Mixed destinations")).toBeInTheDocument();
  });

  it("names the concept when every member agrees", async () => {
    renderBrowse({ rollup: true, groups: { Unmapped: [entry({
      destination_concept_id: 3024561, destination_concept_name: "Albumin [Mass/volume] in Serum or Plasma",
      destination_concept_code: "1751-7", destination_vocabulary_id: "LOINC",
    })] } });
    const table = await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(within(table).getByText("Albumin [Mass/volume] in Serum or Plasma")).toBeInTheDocument();
    expect(within(table).getByText("1751-7")).toBeInTheDocument();
    expect(within(table).getByText("LOINC")).toBeInTheDocument();
  });

  it("suggests once for a label group and sends the active vendor/filter scope", async () => {
    mockPost.mockResolvedValue({ data: suggestRun({ total: 2557, done: 2557, destinations: 2557 }) });
    renderBrowse({
      rollup: true, selected_source: "EPIC",
      tabs: [{ vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 2557, approved: 0, athena: 0 }],
      groups: { Unmapped: [entry()] },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Suggest group" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
      "/v1/code-mappings/group/suggest/",
      expect.objectContaining({ label: "albumin", source: "EPIC", seen_only: "1" }),
    ));
    expect(await screen.findByText(/Suggested once and applied the destination/)).toBeInTheDocument();
  });

  it("does not offer Reject Group or a Status column", async () => {
    renderBrowse({ rollup: true, groups: { Unmapped: [entry({ suggested_action: "reject" })] } });
    const table = await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(within(table).queryByRole("columnheader", { name: "Status" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reject group|Confirm reject/ })).not.toBeInTheDocument();
  });

  it("expands a group into its vendor codes, and collapses again", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ rollup: true, groups: { Unmapped: [entry()] } }) });
      }
      if (url === "/v1/code-mappings/group/") {
        return Promise.resolve({ data: { results: [
          { ...proposedRow, mapping_id: 91, source_code: "EPIC#aaa" },
          { ...proposedRow, mapping_id: 92, source_code: "674310" },
        ] } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const toggle = await screen.findByRole("button", { name: /Expand Albumin/ });
    fireEvent.click(toggle);
    expect(await screen.findByText("EPIC#aaa")).toBeInTheDocument();
    expect(screen.getByText("674310")).toBeInTheDocument();
    // The members endpoint is asked for this label, in this section.
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/group/",
      { params: expect.objectContaining({ label: "albumin", section: "Unmapped" }) });
    fireEvent.click(screen.getByRole("button", { name: /Collapse Albumin/ }));
    expect(screen.queryByText("EPIC#aaa")).not.toBeInTheDocument();
  });

  it("asks for an unlabelled row by its synthetic key", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ rollup: true, groups: { Unmapped: [
          entry({ label: null, description: "", members: 1, mapping_id: 77 })] } }) });
      }
      if (url === "/v1/code-mappings/group/") return Promise.resolve({ data: { results: [] } });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /Expand unlabelled/ }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/group/",
      { params: expect.objectContaining({ label: ":77" }) }));
  });

  it("keeps a label's two sections apart when expanding", async () => {
    // albumin with some codes proposed and some approved is an entry in both
    // sections. Keyed on the label alone, expanding one would show the other's
    // rows, and its chevron would collapse the wrong group.
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({
          rollup: true,
          groups: { Unmapped: [entry({ members: 2 })], Approved: [entry({ members: 1, status: "approved" })] },
          pages: { Unmapped: { page: 1, page_size: 100, total: 1 }, Approved: { page: 1, page_size: 100, total: 1 },
                   Rejected: { page: 1, page_size: 100, total: 0 } },
        }) });
      }
      if (url === "/v1/code-mappings/group/") {
        const section = config?.params?.section;
        return Promise.resolve({ data: { results: [
          { ...proposedRow, mapping_id: section === "Approved" ? 82 : 81,
            source_code: section === "Approved" ? "MAPPED-CODE" : "UNMAPPED-CODE" }] } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const unmapped = await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.click(within(unmapped).getByRole("button", { name: /Expand Albumin/ }));
    expect(await screen.findByText("UNMAPPED-CODE")).toBeInTheDocument();
    // The Mapped section starts collapsed.
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));
    // Its entry is still collapsed too, not showing the Unmapped rows.
    const mapped = screen.getByRole("table", { name: "Approved mappings" });
    expect(within(mapped).getByRole("button", { name: /Expand Albumin/ })).toBeInTheDocument();
    fireEvent.click(within(mapped).getByRole("button", { name: /Expand Albumin/ }));
    expect(await screen.findByText("MAPPED-CODE")).toBeInTheDocument();
    expect(screen.getByText("UNMAPPED-CODE")).toBeInTheDocument();
  });

  it("scopes the members request to the tab the server chose, before any tab is clicked", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({
          rollup: true, selected_source: "ICD10", groups: { Unmapped: [entry()] },
          tabs: [{ vocabulary_id: "ICD10", label: "ICD-10", is_standard: false, proposed: 3, approved: 0, athena: 0 }],
        }) });
      }
      if (url === "/v1/code-mappings/group/") return Promise.resolve({ data: { results: [] } });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /Expand Albumin/ }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/group/",
      { params: expect.objectContaining({ source: "ICD10" }) }));
  });

  it("carries the active search and provenance filter into the members request", async () => {
    // The entry's counts describe the filtered set, so expanding it must not
    // open into the rows the filter existed to hide.
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/code-mappings/") {
        return Promise.resolve({ data: browseData({ rollup: true, groups: { Unmapped: [entry()] } }) });
      }
      if (url === "/v1/code-mappings/group/") return Promise.resolve({ data: { results: [] } });
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.change(screen.getByLabelText("Filter by provenance"), { target: { value: "curator" } });
    fireEvent.click(await screen.findByRole("button", { name: /Expand Albumin/ }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/group/",
      { params: expect.objectContaining({ provenance: "curator" }) }));
  });

  it("still shows the Athena section when grouped", async () => {
    // It is gated on rows the server no longer sends under rollup, so it
    // vanished entirely along with its export menu.
    renderBrowse({
      rollup: true,
      groups: { "Approved": [entry({ label: "ferritin", description: "Ferritin" })] },
      pages: { Unmapped: { page: 1, page_size: 100, total: 0 }, Approved: { page: 1, page_size: 100, total: 0 },
               Rejected: { page: 1, page_size: 100, total: 0 }, "Approved": { page: 1, page_size: 100, total: 1 } },
    });
    // The section renders at all -- it was gated on rows the server no longer
    // sends under rollup, so it disappeared along with its export menu.
    const heading = await screen.findByRole("button", { name: /^Approved/ });
    fireEvent.click(heading);
    expect(await screen.findByRole("table", { name: "Approved mappings" })).toBeInTheDocument();
  });

  it("offers Seen sorting while grouped", async () => {
    renderBrowse({ rollup: true, groups: { Unmapped: [entry()] } });
    const table = await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(within(table).getByTitle("Sort Unmapped by Seen").closest("th")).toHaveAttribute("aria-sort", "descending");
    // Source is not sortable in grouped mode (only Seen is).
    expect(within(table).queryByTitle("Sort Unmapped by Source")).not.toBeInTheDocument();
  });

  it("shows Qty and Units with BasedOn below spanning both columns only when present", async () => {
    renderBrowse({ pages: { ...browseData().pages, Approved: { page: 1, page_size: 100, total: 1 } }, results: [
      { ...proposedRow, example_quantity: "0", source_units: ["mg/dL"], based_on: ["Complete blood count", "ServiceRequest/456"] },
      { ...approvedRow, based_on: [] },
      { ...proposedRow, mapping_id: 99, source_code: "NO-BASED-ON" },
    ] });
    const unmapped = await screen.findByRole("table", { name: "Unmapped mappings" });
    fireEvent.click(screen.getByRole("button", { name: /^Approved\(/ }));
    const mapped = screen.getByRole("table", { name: "Approved mappings" });
    expect(within(unmapped).getByRole("columnheader", { name: "Qty" })).toBeInTheDocument();
    expect(within(unmapped).queryByRole("columnheader", { name: "BasedOn" })).not.toBeInTheDocument();
    const cells = (id: number) => within(document.getElementById(`code-mapping-${id}`)!).getAllByRole("cell");
    expect(cells(7)[3]).toHaveAttribute("colspan", "2");
    expect(within(cells(7)[3]).getByText("0")).toBeInTheDocument();
    expect(within(cells(7)[3]).getByText("mg/dL")).toBeInTheDocument();
    expect(cells(7)[3]).toHaveTextContent("Complete blood count; ServiceRequest/456");
    expect(within(cells(8)[3]).queryByText("BasedOn:")).not.toBeInTheDocument();
    expect(within(cells(99)[3]).queryByText("BasedOn:")).not.toBeInTheDocument();
    expect(within(unmapped).queryByText("import")).not.toBeInTheDocument();
    expect(within(mapped).queryByText("curator")).not.toBeInTheDocument();
  });

  it("shows BasedOn at the bottom right of the dialog source fields and clears stale context", async () => {
    renderBrowse({ results: [{ ...proposedRow, based_on: ["Blood test", "ServiceRequest/456"] }] });
    fireEvent.click(await screen.findByTestId("source-code"));
    const basedOn = await screen.findByLabelText("BasedOn");
    expect(basedOn).toHaveValue("Blood test; ServiceRequest/456");
    expect(basedOn).toHaveAttribute("readonly");
    const fields = screen.getByTestId("source-fields");
    expect(fields.lastElementChild).toContainElement(basedOn);
    expect(fields.lastElementChild).toHaveClass("sm:grid-cols-2", "items-end");
    fireEvent.change(screen.getByLabelText("Source Code Value"), { target: { value: "DIFFERENT" } });
    expect(basedOn).toHaveValue("");
  });

  it("displays Units column with joined values in flat rows", async () => {
    renderBrowse({ results: [{ ...proposedRow, source_units: ["mg/dL", "mmol/L"] }] });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(screen.getByText("mg/dL; mmol/L")).toBeInTheDocument();
  });

  it("displays dash when source_units is empty", async () => {
    renderBrowse({ results: [{ ...proposedRow, source_units: [] }] });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    // Units column shows "—" for codes without unit evidence (conditions, procedures, etc.)
    const cells = screen.getAllByText("—");
    expect(cells.length).toBeGreaterThanOrEqual(1);
  });

  it("uses plain label as group key (no units)", async () => {
    renderBrowse({
      rollup: true,
      groups: { Unmapped: [entry({ group_key: "albumin" })] },
    });
    await screen.findByRole("table", { name: "Unmapped mappings" });
    expect(screen.getByRole("button", { name: /Expand Albumin/ })).toBeInTheDocument();
  });

  it("requests the next page and sorts the full section on the server", async () => {
    mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/v1/code-mappings/") {
        const page = Number(config?.params?.page_0 || 1);
        return Promise.resolve({ data: {
          results: [{ ...proposedRow, destination_concept_id: null, source_code: page === 1 ? "FIRST PAGE" : "SECOND PAGE" }],
          duplicates: [], selected_source: "",
          tabs: [{ vocabulary_id: "", label: "Uncoded", is_standard: false, proposed: 101, approved: 0, athena: 0 }],
          pages: { Unmapped: { page, page_size: 100, total: 101 }, Approved: { page: 1, page_size: 100, total: 0 }, Rejected: { page: 1, page_size: 100, total: 0 } },
          rejected_count: 0,
        } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    expect(await screen.findByText("FIRST PAGE")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({
      order_0: "-occurrence_count", order_1: "-occurrence_count", order_2: "-occurrence_count",
    }) });
    expect(screen.getByTitle("Sort Unmapped by Seen").closest("th")).toHaveAttribute("aria-sort", "descending");
    expect(screen.getByText("Page 1 of 2 · 101 mappings")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("SECOND PAGE")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/", { params: expect.objectContaining({
      page_0: 2, order_0: "-occurrence_count",
    }) });
    // Reversing the active sort returns to page 1.
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
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("6");
    expect(within(section).getByText("Rejected").parentElement).toHaveTextContent("2");
    expect(within(section).getByText("Other destination").parentElement).toHaveTextContent("3");
    expect(within(section).queryByText(/^Metrics:/)).not.toBeInTheDocument();
    expect(screen.queryByText("Review counts: all models")).not.toBeInTheDocument();
    const controls = screen.getByRole("group", { name: "Suggest controls" });
    expect(within(controls).getByRole("checkbox", { name: "Vectors" })).toBeInTheDocument();
    expect(within(controls).getByRole("checkbox", { name: "Replace Current Suggestions" })).toBeInTheDocument();
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
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
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
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
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
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
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
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    const section = await screen.findByRole("region", { name: "Suggestion accuracy" });
    expect(within(section).getByText("Approved").parentElement).toHaveTextContent("99");
    // Older API responses still cannot supply cross-version scores.
    expect(within(section).getAllByText("—")).toHaveLength(3);
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
          pages: { Unmapped: { page: 1, page_size: 100, total: 0 }, Proposed: { page: 1, page_size: 100, total: 1 }, Approved: { page: 1, page_size: 100, total: 0 }, Rejected: { page: 1, page_size: 100, total: 0 } },
        } });
      }
      return Promise.resolve({ data: url.includes("reference") ? reference : {} });
    });
    mockPatch.mockResolvedValue({ data: { ...mapping, status } });
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    fireEvent.click(await screen.findByText("Z12.11", { selector: '[data-testid=source-code]' }));
    // The dialog takes an edit lock before opening (#1440), so it is a tick late.
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: status } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Update Mapping" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: `Proposed(${status === "approved" ? 0 : 1})` })).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Proposed mappings" })).queryByText("Z12.11") !== null).toBe(status !== "approved");
    expect(screen.getByRole("button", { name: `Approved(${status === "approved" ? 1 : 0})` })).toBeInTheDocument();
  });
});

describe("CodeMappingPage links", () => {
  it("opens Concept → Source Code on the concept in the URL", async () => {
    const linked = { concept_id: 4567, concept_name: "Hemoglobin", concept_code: "718-7", vocabulary_id: "LOINC", domain_id: "Measurement" };
    // The forward view's own mocks, for the direction switch below.
    renderPage().unmount();
    const forward = mockGet.getMockImplementation()!;
    mockGet.mockImplementation((url: string, ...rest: unknown[]) => url.startsWith("/v1/concept-to-code/")
      ? Promise.resolve({ data: { results: [], total: 0, page: 1, page_size: 50, zero_seen: 0, concept: linked } })
      : forward(url, ...rest));
    render(
      <MemoryRouter initialEntries={["/code-mappings?direction=reverse&concept=4567"]}>
        <CodeMappingPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("button", { name: "Concept → Source Code" })).toHaveAttribute("aria-pressed", "true");
    const dialog = await screen.findByRole("dialog", { name: "Source codes for Hemoglobin" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // Switching direction remounts the reverse view; the link was used once.
    fireEvent.click(screen.getByRole("button", { name: "Source Code → Concept" }));
    fireEvent.click(await screen.findByRole("button", { name: "Concept → Source Code" }));
    await screen.findByRole("table", { name: "Standard concept coverage" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockGet.mock.calls.filter(([url]) => url === "/v1/concept-to-code/4567/")).toHaveLength(1);
  });
});

describe("write controls follow the role (#2416)", () => {
  const grant = (role: string) => ({
    currentUser: { id: 9, org_accesses: [{ org_slug: "acme", org_name: "Acme", role, expires_at: null }] },
  });
  afterEach(() => { mockUseAuth.mockReturnValue({ currentUser: ANALYST_USER }); });

  it("shows an analyst Upload, New Mapping, Suggest, Source systems and the edit controls", async () => {
    mockUseAuth.mockReturnValue(grant("analyst"));
    renderPage([proposedRow]);
    await screen.findByText("Code Mapping");
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New Mapping" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Suggest controls" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source systems / hospitals" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: `Edit ${proposedRow.source_code}` })).toBeInTheDocument();
  });

  it("gives a doctor without the analyst grant a read-only page", async () => {
    mockUseAuth.mockReturnValue(grant("doctor"));
    renderPage([proposedRow]);
    await screen.findByText("Code Mapping");
    const cells = await screen.findAllByText(proposedRow.source_code);
    expect(cells.length).toBeGreaterThan(0);
    expect(screen.getByText("Read-only: mapping codes needs the analyst role.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source systems / hospitals" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New Mapping" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Suggest controls" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Edit ${proposedRow.source_code}` })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Approve ${proposedRow.source_code}` })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Choose destination for ${proposedRow.source_code}` })).not.toBeInTheDocument();
    fireEvent.click(cells[0]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("restores the last Suggest run's candidates for a writer and not for a reader", async () => {
    const latest = {
      run_id: "restored", state: "success",
      activity: [{ stage: "candidates", mapping_id: proposedRow.mapping_id, source_code: proposedRow.source_code, strategy: "umls",
        candidates: [{ concept_id: 1, concept_name: "Restored candidate", concept_code: "A", vocabulary_id: "SNOMED" }] }],
    };
    const renderWithLatest = () => {
      mockGet.mockReset();
      mockGet.mockImplementation((url: string) => {
        if (url.includes("/latest/")) return Promise.resolve({ data: latest });
        if (url === "/v1/code-mappings/") return Promise.resolve({ data: [proposedRow] });
        if (url === "/v1/code-mappings/reference/") return Promise.resolve({ data: reference });
        return Promise.resolve({ data: {} });
      });
      return render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    };
    mockUseAuth.mockReturnValue(grant("analyst"));
    renderWithLatest();
    expect(await screen.findByRole("region", { name: "Suggestion candidates" })).toBeInTheDocument();
    cleanup();

    mockUseAuth.mockReturnValue(grant("doctor"));
    renderWithLatest();
    await screen.findAllByText(proposedRow.source_code);
    expect(screen.getByText("Read-only: mapping codes needs the analyst role.")).toBeInTheDocument();
    expect(mockGet.mock.calls.some(([url]) => String(url).includes("/latest/"))).toBe(false);
    expect(screen.queryByRole("region", { name: "Suggestion candidates" })).not.toBeInTheDocument();
  });

  it("shows Upload and Source systems to an org admin", async () => {
    mockUseAuth.mockReturnValue({ currentUser: { id: 9, is_org_admin: true, org_accesses: [] } });
    renderPage([proposedRow]);
    await screen.findByText("Code Mapping");
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Source systems / hospitals" })).toBeInTheDocument();
  });
});
