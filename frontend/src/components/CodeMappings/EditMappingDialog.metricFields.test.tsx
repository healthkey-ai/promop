/**
 * Category, Measure Class and Metric Key in the Edit Mapping dialog: three
 * fields that describe a wearable source metric. Shown for the Wearables tab's
 * vocabularies only, editable by a mapping writer, and sent only when changed.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EditMappingDialog from "./EditMappingDialog";
import { emptyReference, type CodeMappingRow, type Reference } from "./codeMappingTypes";

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

const CATEGORIES = [
  "Activity", "Fitness", "Heart & Cardiovascular", "Blood & Respiratory", "Body Composition",
  "Sleep", "Mobility & Gait", "Nutrition", "Symptoms", "Reproductive Health", "Mental Wellbeing",
  "Self-care", "Hearing", "Medication & Device Use", "Safety", "Self-reported",
  "Stress & Autonomic", "Environmental", "Location", "Garmin-specific",
];
const MEASURE_CLASSES = [
  "Direct measurement", "Aggregate over interval", "Algorithmic estimate", "Vendor score",
  "Environmental context", "User-entered or device log",
];

/** GET /v1/code-mappings/reference/, as far as this dialog reads it. */
const reference: Reference = {
  ...emptyReference,
  domains: [{ domain_id: "Measurement", label: "Measurement" }],
  source_code_systems_by_domain: { Measurement: [
    { vocabulary_id: "", label: "None" },
    { vocabulary_id: "OpenWearables", label: "OpenWearables" },
    { vocabulary_id: "LOINC", label: "LOINC" },
  ] },
  omop_tables: { Measurement: "measurement" },
  wearable_metric_categories: CATEGORIES,
  wearable_measure_classes: MEASURE_CLASSES,
  wearable_source_vocabularies: ["Apple", "Garmin", "HL7-PHR", "OpenWearables"],
};

const wearableRow: CodeMappingRow = {
  mapping_id: 41,
  domain_id: "Measurement",
  source_vocabulary_id: "OpenWearables",
  source_code: "resting_heart_rate",
  source_code_description: "Resting heart rate (bpm)",
  category: "Heart & Cardiovascular",
  measure_class: "Aggregate over interval",
  metric_key: "resting_hr",
  destination_concept_id: 3027018,
  destination_concept_name: "Heart rate",
  destination_concept_code: "8867-4",
  destination_vocabulary_id: "LOINC",
  destination_concept_class_id: "Clinical Observation",
  destination_omop_table: "measurement",
  destination_domain_id: "Measurement",
  standard_concept: "S",
  status: "proposed",
  notes: "",
  origin: "import",
  origin_system: "open-wearables-seed",
  suggest_strategy: "",
  umls_cui: "",
  created_by: "",
  occurrence_count: 0,
  destination_count: 1,
  has_mapping: true,
};

const labRow: CodeMappingRow = {
  ...wearableRow, mapping_id: 42, source_vocabulary_id: "LOINC", source_code: "8867-4",
  category: "", measure_class: "", metric_key: "",
};

function renderDialog(row: CodeMappingRow, props: Partial<React.ComponentProps<typeof EditMappingDialog>> = {}) {
  const onSaved = vi.fn();
  // The dialog re-reads the stored row when it opens.
  mockGet.mockImplementation((url: string) => Promise.resolve({
    data: url === `/v1/code-mappings/${row.mapping_id}/` ? { ...row, destination_options: [] } : {},
  }));
  render(
    <EditMappingDialog open mode="edit" row={row} reference={reference} canApprove={false}
      strategies={{ umls: true, lexical: true, vectors: true }} rankingModel="anthropic"
      surface="code2concept" onClose={vi.fn()} onSaved={onSaved} onDeleted={vi.fn()} {...props} />,
  );
  return { onSaved, source: within(screen.getByTestId("source-block")) };
}

const save = () => fireEvent.click(screen.getByRole("button", { name: "Update Mapping" }));
const lastPayload = () => mockPatch.mock.calls.at(-1)![1] as Record<string, unknown>;

describe("EditMappingDialog wearable metric fields", () => {
  beforeEach(() => {
    [mockGet, mockPost, mockPatch, mockDelete].forEach((mock) => mock.mockReset());
    mockPost.mockResolvedValue({ data: {} });
    mockDelete.mockResolvedValue({ data: {} });
    mockPatch.mockImplementation((_url: string, payload: Record<string, unknown>) =>
      Promise.resolve({ data: { ...wearableRow, ...payload } }));
  });

  it("offers Category, Measure Class and Metric Key on a Wearables row, in the source block", async () => {
    const { source } = renderDialog(wearableRow);

    const category = source.getByLabelText("Category") as HTMLSelectElement;
    expect(category.tagName).toBe("SELECT");
    expect(category).toHaveValue("Heart & Cardiovascular");
    // Every allowed category, in the server's order, after an empty option.
    expect(Array.from(category.options).map((option) => option.value)).toEqual(["", ...CATEGORIES]);
    for (const added of ["Symptoms", "Reproductive Health", "Mental Wellbeing", "Self-care", "Hearing"]) {
      expect(within(category).getByRole("option", { name: added })).toBeInTheDocument();
    }

    const measureClass = source.getByLabelText("Measure Class") as HTMLSelectElement;
    expect(measureClass.tagName).toBe("SELECT");
    expect(measureClass).toHaveValue("Aggregate over interval");
    expect(Array.from(measureClass.options).map((option) => option.value)).toEqual(["", ...MEASURE_CLASSES]);

    const metricKey = source.getByLabelText("Metric Key") as HTMLInputElement;
    expect(metricKey.tagName).toBe("INPUT");
    expect(metricKey).toHaveValue("resting_hr");
    expect(metricKey).not.toHaveAttribute("readonly");

    // Category takes the cell beside BasedOn; the other two follow in that grid.
    const cells = Array.from(screen.getByTestId("source-context-row").children);
    expect(cells).toHaveLength(4);
    expect(cells[0]).toContainElement(category);
    expect(cells[1]).toContainElement(screen.getByTestId("source-based-on"));
    expect(cells[2]).toContainElement(measureClass);
    expect(cells[3]).toContainElement(metricKey);
    // None of them is a destination field.
    const destination = within(screen.getByTestId("destination-block"));
    for (const label of ["Category", "Measure Class", "Metric Key"]) {
      expect(destination.queryByLabelText(label)).not.toBeInTheDocument();
    }
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/code-mappings/41/"));
  });

  it("saves the values the curator chose with the mapping", async () => {
    const { onSaved, source } = renderDialog(wearableRow);
    fireEvent.change(source.getByLabelText("Category"), { target: { value: "Sleep" } });
    fireEvent.change(source.getByLabelText("Measure Class"), { target: { value: "Vendor score" } });
    fireEvent.change(source.getByLabelText("Metric Key"), { target: { value: " resting_heart_rate " } });
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/41/", expect.objectContaining({
      category: "Sleep", measure_class: "Vendor score", metric_key: "resting_heart_rate",
      decision_surface: "code2concept",
    })));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(
      expect.objectContaining({ category: "Sleep", metric_key: "resting_heart_rate" }), null));
  });

  it("sends only the field that changed, and can clear one", async () => {
    const { source } = renderDialog(wearableRow);
    fireEvent.change(source.getByLabelText("Category"), { target: { value: "" } });
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(lastPayload()).toMatchObject({ category: "" });
    expect(lastPayload()).not.toHaveProperty("measure_class");
    expect(lastPayload()).not.toHaveProperty("metric_key");
  });

  it("sends none of them when the curator changed none", async () => {
    renderDialog(wearableRow);
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    for (const key of ["category", "measure_class", "metric_key"]) {
      expect(lastPayload()).not.toHaveProperty(key);
    }
  });

  it("shows the server's reason when it refuses a metric key", async () => {
    const reason = "A metric key is lowercase snake case: a letter, then letters, digits or underscores, at most 64 characters (e.g. resting_hr).";
    mockPatch.mockRejectedValue({ response: { status: 400, data: { metric_key: [reason] } } });
    const { onSaved, source } = renderDialog(wearableRow);
    fireEvent.change(source.getByLabelText("Metric Key"), { target: { value: "Resting HR" } });
    save();
    const refusal = await screen.findByText(`metric_key: ${reason}`);
    expect(refusal).toHaveAttribute("role", "alert");
    expect(lastPayload()).toMatchObject({ metric_key: "Resting HR" });
    expect(onSaved).not.toHaveBeenCalled();
    // The dialog stays open with what was typed.
    expect(source.getByLabelText("Metric Key")).toHaveValue("Resting HR");
  });

  it("reads as text for a user who cannot write mappings", () => {
    const { source } = renderDialog(wearableRow, { canWrite: false });
    for (const [label, value] of [
      ["Category", "Heart & Cardiovascular"], ["Measure Class", "Aggregate over interval"],
      ["Metric Key", "resting_hr"],
    ]) {
      const field = source.getByLabelText(label);
      expect(field.tagName).toBe("INPUT");
      expect(field).toHaveAttribute("readonly");
      expect(field).toHaveValue(value);
    }
    expect(source.queryByRole("combobox", { name: "Category" })).not.toBeInTheDocument();
    expect(source.queryByRole("combobox", { name: "Measure Class" })).not.toBeInTheDocument();
  });

  it("shows none of them on a row from another tab, and sends none", async () => {
    const { source } = renderDialog(labRow);
    for (const label of ["Category", "Measure Class", "Metric Key"]) {
      expect(source.queryByLabelText(label)).not.toBeInTheDocument();
    }
    // The context row is as it was: the empty cell and BasedOn.
    expect(screen.getByTestId("source-context-row").children).toHaveLength(2);
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalledWith("/v1/code-mappings/42/", expect.anything()));
    for (const key of ["category", "measure_class", "metric_key"]) {
      expect(lastPayload()).not.toHaveProperty(key);
    }
  });

  it.each(["Apple", "Garmin", "HL7-PHR"])("shows them for a %s row too", (vocabulary) => {
    const { source } = renderDialog({ ...wearableRow, source_vocabulary_id: vocabulary });
    expect(source.getByLabelText("Category")).toHaveValue("Heart & Cardiovascular");
    expect(source.getByLabelText("Measure Class")).toBeInTheDocument();
    expect(source.getByLabelText("Metric Key")).toBeInTheDocument();
  });

  it("stops sending them once the source code system leaves the Wearables tab", async () => {
    const { source } = renderDialog(wearableRow);
    fireEvent.change(source.getByLabelText("Category"), { target: { value: "Sleep" } });
    fireEvent.change(source.getByLabelText("Source Code System"), { target: { value: "LOINC" } });
    expect(source.queryByLabelText("Category")).not.toBeInTheDocument();
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(lastPayload()).not.toHaveProperty("category");
  });

  it("keeps a stored value the list no longer offers selectable", () => {
    const { source } = renderDialog({ ...wearableRow, category: "Retired Category" });
    const category = source.getByLabelText("Category") as HTMLSelectElement;
    expect(category).toHaveValue("Retired Category");
    expect(Array.from(category.options).map((option) => option.value)).toEqual(
      ["", ...CATEGORIES, "Retired Category"]);
  });

  it("puts all three after BasedOn on the patient page, where that cell is taken", () => {
    const { source } = renderDialog({ ...wearableRow, source_code_id: 9 }, { patientId: "77" });
    const cells = Array.from(screen.getByTestId("source-context-row").children);
    expect(cells).toHaveLength(5);
    expect(within(cells[0] as HTMLElement).getByRole("button", { name: "Save source context" })).toBeInTheDocument();
    expect(cells[1]).toContainElement(screen.getByTestId("source-based-on"));
    expect(cells[2]).toContainElement(source.getByLabelText("Category"));
    expect(cells[3]).toContainElement(source.getByLabelText("Measure Class"));
    expect(cells[4]).toContainElement(source.getByLabelText("Metric Key"));
    expect(source.getAllByLabelText("Category")).toHaveLength(1);
  });

  it("takes the stored values when the row it opened from carries none", async () => {
    // The patient page builds its rows from the patient's source codes, which
    // carry no category: the dialog must not show, or save back, a blank.
    const stale = { ...wearableRow, category: undefined, measure_class: undefined, metric_key: undefined };
    mockGet.mockReset();
    const onSaved = vi.fn();
    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: url === "/v1/code-mappings/41/" ? { ...wearableRow, destination_options: [] } : {},
    }));
    render(
      <EditMappingDialog open mode="edit" row={stale} reference={reference} canApprove={false}
        strategies={{ umls: true, lexical: true, vectors: true }} rankingModel="anthropic"
        onClose={vi.fn()} onSaved={onSaved} onDeleted={vi.fn()} />,
    );
    const source = within(screen.getByTestId("source-block"));
    await waitFor(() => expect(source.getByLabelText("Category")).toHaveValue("Heart & Cardiovascular"));
    expect(source.getByLabelText("Measure Class")).toHaveValue("Aggregate over interval");
    expect(source.getByLabelText("Metric Key")).toHaveValue("resting_hr");
    save();
    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    for (const key of ["category", "measure_class", "metric_key"]) {
      expect(lastPayload()).not.toHaveProperty(key);
    }
  });
});
