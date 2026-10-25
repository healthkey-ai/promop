import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LoincAxesPanel from "./LoincAxesPanel";
import type { LoincAxesPayload } from "./loincAxes";

const { get, put, post } = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: { get, put, post } }));

const glucose = { concept_id: 3004501, concept_name: "Glucose [Mass/volume] in Serum or Plasma", concept_code: "2345-7", vocabulary_id: "LOINC" };

const payload = (overrides: Partial<LoincAxesPayload> = {}): LoincAxesPayload => ({
  mapping_id: 7,
  applies: true,
  loinc_axes: {},
  source_axes: {
    axes: {
      component: { value: "Glucose", basis: "source_code", evidence: "description = GLUCOSE" },
      property: { value: "MCnc", basis: "unit", evidence: "valueQuantity.unit = mg/dL" },
      scale_type: { value: "Qn", basis: "value_type", evidence: "valueQuantity" },
      method_type: { value: "", basis: "source_code", evidence: "no method in name" },
    },
    hints: { system: "Ser/Plas" },
  },
  axis_labels: { component: "Component", property: "Property", time_aspect: "Time", system: "System", scale_type: "Scale", method_type: "Method" },
  axis_variant_count: 0,
  variants: [],
  variant_notes: [],
  ...overrides,
});

const variantPayload = () => payload({
  axis_variant_count: 2,
  variants: [
    { id: 1, axis_key: "property=MCnc", axis_values: { property: "MCnc", scale_type: "Qn" }, units: ["mg/dL"], occurrence_count: 40,
      status: "proposed", target_concept: glucose, reviewed_at: null },
    { id: null, axis_key: "property=SCnc", axis_values: { property: "SCnc", scale_type: "Qn" }, units: ["mmol/L"], occurrence_count: 3,
      status: "proposed", target_concept: null, reviewed_at: null },
  ],
  variant_notes: ["Units mg/dL and mmol/L imply different properties."],
});

beforeEach(() => {
  vi.resetAllMocks();
  get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("axis-values")
    ? { axis: "system", values: [{ value: "Ser/Plas", codes: 1200 }, { value: "Bld", codes: 900 }] }
    : url.includes("/concepts/search/") ? { results: [glucose] } : payload() }));
  put.mockImplementation((_url: string, body: { loinc_axes: Record<string, string | null> }) =>
    Promise.resolve({ data: payload({ loinc_axes: body.loinc_axes }) }));
});

describe("LoincAxesPanel", () => {
  it("leads with the axis variants and folds the shared axes under them (#2014)", async () => {
    get.mockResolvedValue({ data: variantPayload() });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove />);
    const variants = await screen.findByRole("table", { name: "Axis variants" });
    const shared = screen.getByText("Axes shared by every variant");
    expect(variants.compareDocumentPosition(shared) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(shared.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText(/Each is a variant with its own destination/)).toBeInTheDocument();
    // The shared axes are still there to edit, under the fold; Save and the
    // unsaved notice stay outside it.
    expect(screen.getByTestId("axis-value-property")).toHaveTextContent("MCnc");
    const save = screen.getByRole("button", { name: "Save axes" });
    expect(save.closest("details")).toBeNull();
    fireEvent.change(screen.getByLabelText("System value"), { target: { value: "Ser" } });
    expect(shared).toHaveTextContent("(unsaved changes)");
    expect(screen.getByText(/Unsaved axes still apply to Suggest/).closest("details")).toBeNull();
  });

  it("reads a property set as 'any of', in full in the title (#2029)", async () => {
    const fractions = ["NFr", "MFr", "AFr", "VFr", "SFr", "CFr"];
    get.mockResolvedValue({ data: payload({
      axis_variant_count: 2,
      source_axes: { axes: { property: { value: fractions, basis: "unit", evidence: "every record unit is %" } }, hints: {} },
      variants: [
        { id: 1, axis_key: "property=MCnc|scale_type=Qn", axis_values: { property: "MCnc", scale_type: "Qn" }, units: ["g/dL"],
          occurrence_count: 10, status: "proposed", target_concept: glucose, reviewed_at: null },
        { id: null, axis_key: "unit=%", axis_values: { property: fractions, scale_type: "Qn" }, units: ["%"],
          occurrence_count: 3, status: "proposed", target_concept: null, reviewed_at: null },
      ],
    }) });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} onSuggestVariant={() => {}} />);
    const variants = await screen.findByRole("table", { name: "Axis variants" });
    const cell = within(variants).getByText(/Property any of NFr, MFr, AFr, VFr \(\+2 more\) · Scale Qn/);
    expect(cell.closest("td")).toHaveAttribute("title", "Property any of NFr, MFr, AFr, VFr, SFr, CFr · Scale Qn");
    expect(screen.getByRole("button", { name: "Suggest for Property any of NFr, MFr, AFr, VFr (+2 more) · Scale Qn" })).toBeInTheDocument();
    // The shared axes show the set too, since every % record settles it.
    expect(screen.getByTestId("axis-value-property")).toHaveTextContent("any of NFr, MFr, AFr, VFr (+2 more)");
    expect(screen.getByTestId("axis-value-property")).toHaveAttribute("title", "any of NFr, MFr, AFr, VFr, SFr, CFr");
  });

  it("shows the axes table on its own for a code with one axis set", async () => {
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    await screen.findByRole("region", { name: "LOINC axes" });
    expect(screen.queryByText("Axes shared by every variant")).not.toBeInTheDocument();
    expect(screen.getByText(/Settled axes are constraints/)).toBeInTheDocument();
  });

  it("reports how many axis variants the code has, so Suggest can fill them in (#2012)", async () => {
    const onVariantsChange = vi.fn();
    get.mockResolvedValue({ data: variantPayload() });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} onVariantsChange={onVariantsChange} />);
    await screen.findByRole("table", { name: "Axis variants" });
    expect(onVariantsChange).toHaveBeenLastCalledWith(2);
    expect(screen.getByText(/Suggest fills in a proposed LOINC for each variant/)).toBeInTheDocument();
  });

  it("reports zero variants for a code with one axis set", async () => {
    const onVariantsChange = vi.fn();
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} onVariantsChange={onVariantsChange} />);
    await screen.findByRole("region", { name: "LOINC axes" });
    expect(onVariantsChange).toHaveBeenLastCalledWith(0);
  });

  it("shows each axis with its value, basis and evidence, and the metadata hints", async () => {
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    expect(await screen.findByRole("region", { name: "LOINC axes" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/7/loinc-axes/");
    expect(screen.getByTestId("axis-value-property")).toHaveTextContent("MCnc");
    expect(screen.getByTestId("axis-why-property")).toHaveTextContent("From unit: valueQuantity.unit = mg/dL");
    expect(screen.getByTestId("axis-value-method_type")).toHaveTextContent("Methodless");
    expect(screen.getByTestId("axis-value-system")).toHaveTextContent("—");
    expect(screen.getByTestId("axis-why-system")).toHaveTextContent("Not settled");
    expect(screen.getByText(/Metadata says: System — Ser\/Plas/)).toBeInTheDocument();
    expect(screen.getByText(/Settled axes are constraints/)).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Axis variants" })).not.toBeInTheDocument();
  });

  it("marks curator-set axes", async () => {
    get.mockResolvedValue({ data: payload({
      loinc_axes: { system: "Ser/Plas" },
      source_axes: { axes: { system: { value: "Ser/Plas", basis: "curator", evidence: "" } }, hints: {} },
    }) });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    expect(await screen.findByTestId("axis-why-system")).toHaveTextContent("Set by curator");
  });

  it("renders nothing when the domain has no LOINC", async () => {
    get.mockResolvedValue({ data: payload({ applies: false }) });
    const { container } = render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("says it is loading until the axes arrive, naming the flagged variants, in one section throughout (#2030)", async () => {
    let answer: (value: { data: LoincAxesPayload }) => void = () => {};
    get.mockImplementation(() => new Promise(resolve => { answer = resolve; }));
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} expectedVariants={3} />);
    const section = screen.getByRole("region", { name: "LOINC axes" });
    expect(section).toHaveAttribute("aria-busy", "true");
    const status = within(section).getByRole("status");
    expect(status).toHaveTextContent("Loading the 3 axis values the queue flagged for this code, and their destinations…");
    answer({ data: variantPayload() });
    await within(section).findByRole("table", { name: "Axis variants" });
    expect(section).not.toHaveAttribute("aria-busy");
    expect(within(section).getByRole("status")).toHaveTextContent(/Each is a variant with its own destination/);
    expect(screen.queryByText(/^Loading the/)).not.toBeInTheDocument();
  });

  it("says it is loading the axes for a code the queue has not flagged", () => {
    get.mockImplementation(() => new Promise(() => {}));
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading the LOINC axes this code settles");
  });

  it("stays off screen while loading for a row whose domain cannot hold LOINC axes", () => {
    get.mockImplementation(() => new Promise(() => {}));
    const { container } = render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} mayApply={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("marks a re-read busy too, keeping the axes on screen", async () => {
    const { rerender } = render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    const section = await screen.findByRole("region", { name: "LOINC axes" });
    expect(section).not.toHaveAttribute("aria-busy");
    get.mockImplementation(() => new Promise(() => {}));
    rerender(<LoincAxesPanel mappingId={7} canEdit canApprove={false} reloadToken={1} />);
    expect(section).toHaveAttribute("aria-busy", "true");
    expect(within(section).getByRole("status")).toHaveTextContent("Re-reading the axes and variants…");
    expect(screen.getByTestId("axis-value-property")).toHaveTextContent("MCnc");
  });

  it("reports a failed load inside the section", async () => {
    get.mockRejectedValue(new Error("boom"));
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    const section = screen.getByRole("region", { name: "LOINC axes" });
    expect(await within(section).findByRole("alert")).toHaveTextContent("Could not load the LOINC axes for this code.");
    expect(section).not.toHaveAttribute("aria-busy");
  });

  it("reports edits, Clear and Methodless, and saves them as the PUT body", async () => {
    const onAxesChange = vi.fn();
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} onAxesChange={onAxesChange} />);
    await screen.findByRole("region", { name: "LOINC axes" });
    expect(onAxesChange).toHaveBeenLastCalledWith({});
    const system = screen.getByLabelText("System value");
    fireEvent.focus(system);
    fireEvent.change(system, { target: { value: "Ser" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/v1/loinc/axis-values/", expect.objectContaining({ params: { axis: "system", q: "Ser" } })));
    expect(onAxesChange).toHaveBeenLastCalledWith({ system: "Ser" });
    fireEvent.change(system, { target: { value: "Ser/Plas" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear Property" }));
    expect(screen.getByTestId("axis-value-property")).toHaveTextContent("No constraint");
    fireEvent.click(screen.getByRole("button", { name: "Methodless" }));
    expect(onAxesChange).toHaveBeenLastCalledWith({ system: "Ser/Plas", property: null, method_type: "" });
    fireEvent.click(screen.getByRole("button", { name: "Save axes" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/v1/code-mappings/7/loinc-axes/", {
      loinc_axes: { system: "Ser/Plas", property: null, method_type: "" },
    }));
    expect(await screen.findByText("LOINC axes saved.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save axes" })).toBeDisabled();
  });

  it("hides edit controls when the curator cannot edit", async () => {
    render(<LoincAxesPanel mappingId={7} canEdit={false} canApprove={false} />);
    await screen.findByRole("region", { name: "LOINC axes" });
    expect(screen.queryByLabelText("System value")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save axes" })).not.toBeInTheDocument();
  });

  it("lists axis variants, suggests per variant, and approves one with its destination", async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("/concepts/search/") ? { results: [glucose] } : variantPayload() }));
    post.mockResolvedValue({ data: { ...variantPayload(), variant_status: "approved", repoint: { rows_updated: 40, persons_marked_stale: 5, rows_collapsed: 0 } } });
    const onSuggestVariant = vi.fn();
    render(<LoincAxesPanel mappingId={7} canEdit canApprove onSuggestVariant={onSuggestVariant} />);
    expect(await screen.findByText("2 axis values — this code may need one LOINC per variant")).toBeInTheDocument();
    expect(screen.getByText("Units mg/dL and mmol/L imply different properties.")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Axis variants" });
    const first = within(table).getByText("Property MCnc · Scale Qn").closest("tr")!;
    expect(first).toHaveTextContent("mg/dL");
    expect(first).toHaveTextContent("40");
    expect(first).toHaveTextContent("Glucose [Mass/volume] in Serum or Plasma");
    expect(within(table).getByRole("button", { name: "Approve Property SCnc · Scale Qn" })).toBeDisabled();

    fireEvent.click(within(first).getByRole("button", { name: "Suggest for Property MCnc · Scale Qn" }));
    expect(onSuggestVariant).toHaveBeenCalledWith("property=MCnc");

    fireEvent.click(within(first).getByRole("button", { name: "Approve Property MCnc · Scale Qn" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/v1/code-mappings/7/axis-variants/", {
      axis_key: "property=MCnc", target_concept_id: glucose.concept_id, approve: true,
    }));
    expect(await screen.findByText(/Variant approved: moved 40 row\(s\)/)).toBeInTheDocument();
  });

  it("reopens an approved variant explicitly, and says so", async () => {
    const approved = variantPayload();
    approved.variants[0] = { ...approved.variants[0], status: "approved" };
    get.mockResolvedValue({ data: approved });
    post.mockResolvedValue({ data: { ...variantPayload(), variant_status: "proposed",
      repoint: { rows_updated: 40, persons_marked_stale: 5, rows_collapsed: 0 } } });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove />);
    const table = await screen.findByRole("table", { name: "Axis variants" });
    fireEvent.click(within(table).getByRole("button", { name: "Reopen Property MCnc · Scale Qn" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/v1/code-mappings/7/axis-variants/", {
      axis_key: "property=MCnc", target_concept_id: glucose.concept_id, approve: false, reopen: true,
    }));
    expect(await screen.findByText(/Variant reopened: moved 40 row\(s\)/)).toBeInTheDocument();
  });

  it("hides Set destination on an approved variant from a curator who cannot approve", async () => {
    const approved = variantPayload();
    approved.variants[0] = { ...approved.variants[0], status: "approved" };
    get.mockResolvedValue({ data: approved });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    const table = await screen.findByRole("table", { name: "Axis variants" });
    expect(within(table).queryByRole("button", { name: "Set destination for Property MCnc · Scale Qn" })).toBeNull();
    expect(within(table).getByRole("button", { name: "Set destination for Property SCnc · Scale Qn" })).toBeInTheDocument();
  });

  it("sets a variant's destination from the concept search", async () => {
    get.mockImplementation((url: string) => Promise.resolve({ data: url.includes("/concepts/search/") ? { results: [glucose] } : variantPayload() }));
    post.mockResolvedValue({ data: variantPayload() });
    render(<LoincAxesPanel mappingId={7} canEdit canApprove={false} />);
    const table = await screen.findByRole("table", { name: "Axis variants" });
    expect(within(table).queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "Set destination for Property SCnc · Scale Qn" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search destination for Property SCnc · Scale Qn" }), { target: { value: "glucose" } });
    fireEvent.click(await screen.findByRole("option", { name: /Glucose \[Mass\/volume\]/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/v1/code-mappings/7/axis-variants/", {
      axis_key: "property=SCnc", target_concept_id: glucose.concept_id, approve: false,
    }));
  });
});
