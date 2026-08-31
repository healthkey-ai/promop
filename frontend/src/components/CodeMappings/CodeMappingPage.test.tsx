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
};

const loincHit = {
  concept_id: 3046299,
  concept_name: "Protein.monoclonal [Mass/volume] in Serum",
  concept_code: "33358-3",
  vocabulary_id: "LOINC",
  domain_id: "Measurement",
  concept_class_id: "Lab Test",
  standard_concept: "S",
};

function renderPage(rows = [proposedRow, approvedRow]) {
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/code-mappings/") return Promise.resolve({ data: rows });
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

    it("gives every control a tooltip carrying the field's meaning", async () => {
      await openDialog();
      const dialog = screen.getByRole("dialog");
      const controls = Array.from(dialog.querySelectorAll("input, select, textarea"));
      const untipped = controls.filter((el) => !(el.getAttribute("title") || "").trim());
      expect(untipped.map((el) => accessibleName(el))).toEqual([]);
      // And the tooltip is visibly advertised next to the label.
      expect(within(dialog).getAllByText("ⓘ").length).toBe(controls.length);
    });

    it("labels the source code value field, and never calls it a concept code", async () => {
      await openDialog();
      const input = screen.getByLabelText("Source Code Value") as HTMLInputElement;
      expect(input.value).toBe("M-PROTEIN, SERUM");
      expect(screen.queryByText("Source concept code")).not.toBeInTheDocument();
      // The word "concept" never appears on the source side except on the
      // read-only Source Concept ID, which is about the source code itself.
      expect(screen.getByLabelText("Source Description")).toBeInTheDocument();
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

    it("puts import proposals above hand-written ones, then sorts humans by name", async () => {
      // An import's proposal is nobody's decision yet — it is the work the
      // queue exists for. Human drafts then group by author so one curator's
      // in-progress work stays together.
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
      // Machine first despite the lowest count; then Ada before Zoe despite
      // Zoe's row being seen 900 times.
      expect(codes).toEqual(["MACHINE", "HUMAN-ADA", "HUMAN-ZOE"]);
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
          `Created by zoe@example.com · approved by ada@example.com on ${when} · seen 3 time(s)`,
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
          `Proposed by import (fhir-sync) · approved by ada@example.com on ${when} · seen 3 time(s)`,
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
    it("enables Suggest on an HK-* tab and disables it on a standard one", async () => {
      // HK-* tabs hold locally minted destinations for unmapped codes. A
      // standard vocabulary is somewhere a curator re-points *into* —
      // enumerating SNOMED's 1.09M concepts would not be a queue. Disabled
      // with a reason rather than hidden: a button that vanishes reads as a bug.
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      expect(screen.getByRole("button", { name: /Suggest/ })).toBeEnabled();

      const tabs = within(screen.getByRole("tablist", { name: "Destination vocabularies" }));
      fireEvent.click(tabs.getByRole("button", { name: /LOINC/ }));
      const button = screen.getByRole("button", { name: /Suggest/ });
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", expect.stringContaining("HK-*") as unknown as string);
    });

    it("opens on an HK-* tab when nothing is mapped anywhere", async () => {
      // The state Suggest exists for. Standard vocabularies come first in the
      // tab strip, so an empty queue opened on SNOMED — where the button is
      // disabled — making it unreachable exactly when it is needed.
      renderPage([]);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /Suggest/ })).toBeEnabled());
      const tabs = within(screen.getByRole("tablist", { name: "Destination vocabularies" }));
      const selected = tabs.getAllByRole("button")
        .find((b) => b.className.includes("border-slate-950"));
      expect(selected?.textContent).toMatch(/^HK-/);
    });

    it("defaults the threshold to 10 and sends it", async () => {
      // 43% of staging's unmapped codes appear exactly once; proposing for them
      // buries the 512 that carry the traffic.
      mockPost.mockResolvedValue({ data: { created: 3, considered: 5, ranked: 2 } });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      expect(screen.getByLabelText(/seen at least/i)).toHaveValue(10);

      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      expect(mockPost.mock.calls[0][0]).toBe("/v1/code-mappings/suggest/");
      expect(mockPost.mock.calls[0][1]).toMatchObject({
        destination_vocabulary_id: "HK-Labs",
        min_occurrences: 10,
      });
    });

    it("reports what it proposed", async () => {
      mockPost.mockResolvedValue({
        data: { created: 3, considered: 5, ranked: 2, truncated: true },
      });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));

      const status = await screen.findByRole("status");
      expect(status).toHaveTextContent("Proposed 3 mapping(s) from 5 unmapped code(s)");
      expect(status).toHaveTextContent("More remain");
    });

    it("says so when nothing meets the threshold", async () => {
      mockPost.mockResolvedValue({ data: { created: 0, considered: 0, ranked: 0 } });
      renderPage();
      await screen.findByText("M-PROTEIN, SERUM", { selector: "td" });
      fireEvent.click(screen.getByRole("button", { name: /Suggest/ }));
      expect(await screen.findByRole("status")).toHaveTextContent("No unmapped codes seen 10+ times");
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
