import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CoOccurringLabs, { type CoOccurringCode, type CoOccurringLabsPayload } from "./CoOccurringLabs";
import SourceEvidencePanel from "./SourceEvidencePanel";
import { editMappingUrl, linkedMappingId } from "./mappingLinks";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: { get } }));

const code = (overrides: Partial<CoOccurringCode> = {}): CoOccurringCode => ({
  source_code: "ALBE", source_vocabulary_id: "EPIC", omop_table: "measurement",
  description: "Albumin, electrophoresis", units: ["g/dL"], system: "Ser/Plas", method: "Electrophoresis",
  axes_basis: "mapping", axes_loinc: "2862-1", mapping_id: 11, mapping_status: "approved", orders: 12,
  ...overrides,
});

const payload = (overrides: Partial<CoOccurringLabsPayload> = {}): CoOccurringLabsPayload => ({
  timed_out: false, records: 160, records_read: 30, records_with_order: 148, orders: 31,
  order_names: [{ name: "PROTEIN ELECTROPHORESIS SERUM", count: 20 }, { name: "SPEP", count: 4 }],
  order_names_total: 2,
  codes: [
    code(),
    code({ source_code: "BGE", description: "Beta globulin, electrophoresis", axes_basis: "hospital",
      axes_loinc: "2871-2", mapping_id: 12, mapping_status: "unmapped", orders: 9 }),
    code({ source_code: "SPEPI", description: "", units: [], system: "", method: "", axes_basis: "",
      axes_loinc: "", mapping_id: null, mapping_status: "unmapped", orders: 2 }),
  ],
  codes_total: 3,
  ...overrides,
});

const serve = (data: Partial<CoOccurringLabsPayload>) => get.mockResolvedValue({ data: payload(data) });

describe("CoOccurringLabs", () => {
  beforeEach(() => { get.mockReset(); });

  it("reads the mapping's co-occurring labs and lists them with their mapping", async () => {
    serve({});
    render(<CoOccurringLabs mappingId={7} />);

    expect(screen.getByRole("status")).toHaveTextContent("Looking for order links");
    const table = await screen.findByRole("table", { name: "Co-occurring labs" });
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/7/co-occurring/");
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(
      ["Source code", "Description", "Units", "System", "Method", "Mapping status"]);
    const [albumin, beta, interpretation] = within(table).getAllByRole("row").slice(1);
    expect(albumin).toHaveTextContent("ALBEAlbumin, electrophoresisg/dLSer/PlasElectrophoresisApproved");
    // System and Method from the hospital's own LOINC are marked.
    expect(within(albumin).queryByTitle("From the LOINC the hospital sent")).toBeNull();
    expect(within(beta).getAllByTitle("From the LOINC the hospital sent")).toHaveLength(2);
    expect(beta).toHaveTextContent("Unmapped");
    expect(interpretation).toHaveTextContent("SPEPI————Unmapped");
  });

  it("links a code to its own Edit Mapping, and leaves a code with no mapping row as text", async () => {
    serve({});
    render(<CoOccurringLabs mappingId={7} />);

    const link = await screen.findByRole("link", { name: "ALBE" });
    expect(link).toHaveAttribute("href", "/code-mappings?direction=forward&mapping=11");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("title", "Open Edit Mapping for ALBE. In 12 of 31 orders.");
    expect(screen.queryByRole("link", { name: "SPEPI" })).toBeNull();
    expect(screen.getByText("SPEPI")).toHaveAttribute("title", "Not in the mapping queue yet. In 2 of 31 orders.");
  });

  it("states the coverage: records with an order, distinct orders and order names", async () => {
    serve({ order_names_total: 5 });
    render(<CoOccurringLabs mappingId={7} />);

    expect((await screen.findByText("Records with an order reference")).nextSibling).toHaveTextContent("148 of 160");
    expect(screen.getByText("Distinct orders").nextSibling).toHaveTextContent("31 in the 30 records read");
    expect(screen.getByText("Order names").nextSibling).toHaveTextContent(
      "PROTEIN ELECTROPHORESIS SERUM (20) · SPEP (4) · and 3 more");
  });

  it("says so when the source records carry no order links", async () => {
    serve({ records_with_order: 0, orders: 0, order_names: [], order_names_total: 0, codes: [], codes_total: 0 });
    render(<CoOccurringLabs mappingId={7} />);

    expect(await screen.findByText("No order links in the source records.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("tells orders with no other result from no orders at all", async () => {
    serve({ codes: [], codes_total: 0 });
    render(<CoOccurringLabs mappingId={7} />);

    expect(await screen.findByText("No other result shares these orders in the records read.")).toBeInTheDocument();
    expect(screen.queryByText("No order links in the source records.")).toBeNull();
  });

  it("collapses a long list and says how many codes the server left out", async () => {
    const many = Array.from({ length: 12 }, (_, index) => code({ source_code: `C${index}`, mapping_id: 100 + index }));
    serve({ codes: many, codes_total: 15 });
    render(<CoOccurringLabs mappingId={7} />);

    const table = await screen.findByRole("table", { name: "Co-occurring labs" });
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    expect(screen.queryByText(/not listed/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show 2 more" }));
    expect(within(table).getAllByRole("row")).toHaveLength(13);
    expect(screen.getByText("3 more codes share these orders and are not listed.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(within(table).getAllByRole("row")).toHaveLength(11);
  });

  it("reports a read that failed or ran out of time instead of an empty answer", async () => {
    get.mockRejectedValueOnce(new Error("500"));
    const failed = render(<CoOccurringLabs mappingId={7} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load co-occurring labs");
    expect(screen.queryByText("No order links in the source records.")).toBeNull();
    failed.unmount();

    serve({ timed_out: true, records: 0, records_with_order: 0, codes: [], codes_total: 0 });
    render(<CoOccurringLabs mappingId={7} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("took too long");
    expect(screen.queryByText("No order links in the source records.")).toBeNull();
  });
});

describe("SourceEvidencePanel co-occurring labs", () => {
  beforeEach(() => { get.mockReset(); });

  const evidence = {
    organization: null, occurrence_count: 160,
    metadata: { category: { top: "laboratory", mix: "laboratory (160)" }, value_types: { quantity: 100 } },
  };

  it("shows them after the observation category and value types", async () => {
    serve({});
    render(<SourceEvidencePanel evidence={evidence} mappingId={7} loading={false} />);

    const table = await screen.findByRole("table", { name: "Co-occurring labs" });
    const valueTypes = screen.getByText("Selected FHIR value types");
    expect(valueTypes.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "Source evidence" })).toContainElement(table);
  });

  it("reads again for another mapping and never without one", async () => {
    serve({});
    const view = render(<SourceEvidencePanel evidence={evidence} loading={false} />);
    expect(get).not.toHaveBeenCalled();

    view.rerender(<SourceEvidencePanel evidence={evidence} mappingId={7} loading={false} />);
    await screen.findByRole("table", { name: "Co-occurring labs" });
    serve({ codes: [code({ source_code: "GLU", mapping_id: 30 })], codes_total: 1 });
    view.rerender(<SourceEvidencePanel evidence={evidence} mappingId={8} loading={false} />);

    expect(await screen.findByRole("link", { name: "GLU" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "ALBE" })).toBeNull();
    expect(get.mock.calls.map(([url]) => url)).toEqual(
      ["/v1/code-mappings/7/co-occurring/", "/v1/code-mappings/8/co-occurring/"]);
  });
});

describe("mapping links", () => {
  it("round-trips a mapping id and ignores anything that is not one", () => {
    const url = new URL(editMappingUrl(42), "https://promop.example");
    expect(url.pathname).toBe("/code-mappings");
    expect(linkedMappingId(url.searchParams)).toBe(42);
    for (const value of ["", "0", "-3", "1.5", "abc"]) {
      expect(linkedMappingId(new URLSearchParams({ mapping: value }))).toBeUndefined();
    }
    expect(linkedMappingId(new URLSearchParams())).toBeUndefined();
  });
});
