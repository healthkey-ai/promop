import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PatientWearableCodes from "./PatientWearableCodes";
import type { SourceCodeWearable, WearableSourceCode } from "../../types/sourceCodes";

type Overrides = Partial<Omit<WearableSourceCode, "wearable">> & { wearable?: Partial<SourceCodeWearable> };

const code = ({ wearable, ...overrides }: Overrides = {}): WearableSourceCode => ({
  source_value: "steps", omop_table: "measurement", concept_id: 40758552, concept_name: "Number of steps",
  row_count: 30, mapping_id: 41, mapping_status: "approved",
  mapping_target_concept_id: 40758552,
  mapping_target_concept_name: "Number of steps in unspecified time Pedometer",
  mapping_destination_concept_code: "55423-8", mapping_destination_vocabulary_id: "LOINC",
  source_vocabulary_id: "Garmin", source_code: "steps", source_unit: "", example_quantity: "",
  source_metadata: {}, source_code_description: "Daily step count",
  ...overrides,
  wearable: {
    device: "garmin", source: "Garmin", metric_key: "steps",
    rows: 30, accepted_rows: 30, provisional_rows: 0, unresolved_rows: 0, confidence: null,
    first_date: "2026-10-24", last_date: "2026-11-22", status: "accepted",
    fields: [{ name: "median_daily_steps_30d", label: "Median Daily Steps" }],
    ...wearable,
  },
});

const APPLE = {
  source_value: "HKQuantityTypeIdentifierStepCount", source_code: "HKQuantityTypeIdentifierStepCount",
  source_vocabulary_id: "Apple",
};
const apple = ({ wearable, ...overrides }: Overrides = {}) =>
  code({ ...APPLE, ...overrides, wearable: { device: "apple", source: "Apple Health", ...wearable } });

const row = (id: string) => screen.getByTestId(`wearable-code-${id}`);
const status = (id: string) => screen.getByTestId(`wearable-status-${id}`);

describe("PatientWearableCodes", () => {
  it("renders nothing for a patient with no device codes", () => {
    const { container } = render(<PatientWearableCodes codes={[]} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("wearable-codes")).not.toBeInTheDocument();
  });

  it("heads the section, counts the codes and says what the three kinds of row are", () => {
    render(<PatientWearableCodes codes={[code(), apple()]} />);
    const section = screen.getByTestId("wearable-codes");
    expect(within(section).getByRole("heading", { name: "Wearables" })).toBeInTheDocument();
    expect(section).toHaveTextContent("Wearables(2)");
    expect(section).toHaveTextContent(
      "Device codes and the OMOP concept each is mapped to. Accepted rows come from an approved mapping. "
      + "Provisional rows come from a proposed mapping and become accepted when it is approved. "
      + "Unresolved rows are stored and wait for a mapping.");
  });

  it("lists a code's source, concept, table, rows, dates and 30-day metrics under column headers", () => {
    render(<PatientWearableCodes codes={[code({
      wearable: { fields: [
        { name: "median_daily_steps_30d", label: "Median Daily Steps" },
        { name: "activity_trend_30d", label: "Activity Trend" },
      ] },
    })]} />);
    const table = screen.getByRole("table", { name: "Wearable codes" });
    expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(
      ["Source", "OMOP concept", "Table", "Rows", "Dates", "Status", "30-day metrics"]);
    const cells = within(row("garmin-steps")).getAllByRole("cell");
    expect(cells[0].textContent).toBe("GarminstepsDaily step count");
    expect(within(cells[0]).getByText("steps")).toHaveClass("font-mono");
    expect(cells[1].textContent).toBe("LOINC 55423-8Number of steps in unspecified time Pedometer");
    expect(cells[2].textContent).toBe("Measurement");
    expect(cells[3].textContent).toBe("30");
    expect(cells[4].textContent).toBe("2026-10-24 to 2026-11-22");
    expect(cells[5].textContent).toBe("Accepted");
    expect(cells[6].textContent).toBe("Median Daily Steps, Activity Trend");
  });

  it("names each row by its device and code", () => {
    render(<PatientWearableCodes codes={[code(), apple({ omop_table: "observation" })]} />);
    const cells = within(row("apple-HKQuantityTypeIdentifierStepCount")).getAllByRole("cell");
    expect(cells[0]).toHaveTextContent("Apple Health");
    expect(cells[0]).toHaveTextContent("HKQuantityTypeIdentifierStepCount");
    expect(cells[2].textContent).toBe("Observation");
  });

  it("shows one date when the rows fall on one day, and a dash when there are none", () => {
    render(<PatientWearableCodes codes={[
      code({ wearable: { first_date: "2026-11-22", last_date: "2026-11-22" } }),
      apple({ wearable: { rows: 0, accepted_rows: 0, status: "none", first_date: null, last_date: null } }),
    ]} />);
    expect(within(row("garmin-steps")).getAllByRole("cell")[4].textContent).toBe("2026-11-22");
    expect(within(row("apple-HKQuantityTypeIdentifierStepCount")).getAllByRole("cell")[4].textContent).toBe("—");
  });

  it("shows a dash for a code whose mapping has no destination and that feeds no record metric", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_id: null, mapping_status: "unmapped", source_code_description: "",
      mapping_target_concept_id: null, mapping_target_concept_name: null,
      mapping_destination_concept_code: "", mapping_destination_vocabulary_id: "",
      wearable: { metric_key: "", status: "unresolved", accepted_rows: 0, unresolved_rows: 30, fields: [] },
    })]} />);
    const cells = within(row("garmin-steps")).getAllByRole("cell");
    expect(cells[0].textContent).toBe("Garminsteps");
    expect(cells[1].textContent).toBe("—");
    expect(cells[6].textContent).toBe("—");
  });

  it("hands a clicked row's source code to the tab's row handler, wherever in the row it is clicked", () => {
    const onRowClick = vi.fn();
    const steps = code();
    render(<PatientWearableCodes codes={[steps, apple()]} onRowClick={onRowClick} />);
    expect(row("garmin-steps")).toHaveClass("cursor-pointer");

    fireEvent.click(within(row("garmin-steps")).getByText("Daily step count"));
    fireEvent.click(status("garmin-steps"));
    expect(onRowClick).toHaveBeenCalledTimes(2);
    expect(onRowClick).toHaveBeenNthCalledWith(1, steps);
    expect(onRowClick).toHaveBeenNthCalledWith(2, steps);
    // It opens the tab's dialog: nothing in the row links elsewhere.
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("is inert with no handler, as for a reader", () => {
    render(<PatientWearableCodes codes={[code()]} />);
    expect(row("garmin-steps")).not.toHaveClass("cursor-pointer");
    fireEvent.click(row("garmin-steps"));
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("PatientWearableCodes status", () => {
  it("marks rows at an approved mapping's concept Accepted, in green, with nothing more to say", () => {
    render(<PatientWearableCodes codes={[code()]} />);
    const cell = status("garmin-steps");
    expect(cell.textContent).toBe("Accepted");
    expect(within(cell).getByText("Accepted")).toHaveClass("bg-green-100", "text-green-800");
  });

  it("marks rows a proposed mapping gave a concept Provisional, in amber, with the confidence", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_status: "proposed",
      wearable: { status: "provisional", accepted_rows: 0, provisional_rows: 30, confidence: 85 },
    })]} />);
    const cell = status("garmin-steps");
    expect(cell.textContent).toBe("Provisional 85%");
    expect(within(cell).getByText("Provisional 85%")).toHaveClass("bg-amber-100", "text-amber-800");
  });

  it("marks rows waiting for a mapping Unresolved, in grey, and says why", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_id: null, mapping_status: "unmapped",
      wearable: { status: "unresolved", accepted_rows: 0, unresolved_rows: 30 },
    })]} />);
    const cell = status("garmin-steps");
    const badge = within(cell).getByText("Unresolved");
    expect(badge).toHaveClass("bg-slate-100", "text-slate-600");
    expect(badge.title).toBe("Stored and waiting: no mapping gives these rows a concept yet.");
    expect(cell.textContent).toBe("Unresolvedno mapping");
  });

  it("shows one badge per kind of row a mixed code has", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_status: "proposed",
      wearable: { status: "mixed", rows: 17, accepted_rows: 12, provisional_rows: 3, unresolved_rows: 2, confidence: 70 },
    })]} />);
    const cell = status("garmin-steps");
    expect(within(cell).getByText("Accepted 12")).toHaveClass("bg-green-100");
    const provisional = within(cell).getByText("Provisional 3");
    expect(provisional).toHaveClass("bg-amber-100");
    expect(provisional.title).toMatch(/Lowest confidence 70%/);
    expect(within(cell).getByText("Unresolved 2")).toHaveClass("bg-slate-100");
    // The mapping's own status explains the mix.
    expect(cell.textContent).toBe("Accepted 12Provisional 3Unresolved 2mapping proposed");
  });

  it("leaves out the badge of a kind a mixed code has none of", () => {
    render(<PatientWearableCodes codes={[code({
      wearable: { status: "mixed", rows: 14, accepted_rows: 12, provisional_rows: 0, unresolved_rows: 2 },
    })]} />);
    expect(status("garmin-steps").textContent).toBe("Accepted 12Unresolved 2mapping approved");
  });

  it("shows both counts for provisional rows beside unresolved ones", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_status: "proposed",
      wearable: { status: "mixed", rows: 9, accepted_rows: 0, provisional_rows: 5, unresolved_rows: 4, confidence: 80 },
    })]} />);
    expect(status("garmin-steps").textContent).toBe("Provisional 5Unresolved 4mapping proposed");
  });

  it("reads the counts when the status is one it does not know, and never says No rows over stored ones", () => {
    render(<PatientWearableCodes codes={[code({
      mapping_status: "approved",
      // A status a later server might send.
      wearable: { status: "superseded" as never, rows: 3, accepted_rows: 3, provisional_rows: 0, unresolved_rows: 0 },
    })]} />);
    const cell = status("garmin-steps");
    expect(within(cell).getByText("Accepted")).toBeInTheDocument();
    expect(within(cell).queryByText("No rows")).not.toBeInTheDocument();
  });

  it.each([
    ["unmapped", "no mapping"],
    ["proposed", "mapping proposed"],
    ["approved", "mapping approved"],
  ] as const)("says No rows, in grey, for a code with nothing stored (%s: %s)", (mappingStatus, note) => {
    render(<PatientWearableCodes codes={[code({
      mapping_status: mappingStatus,
      wearable: { status: "none", rows: 0, accepted_rows: 0, first_date: null, last_date: null },
    })]} />);
    const cell = status("garmin-steps");
    expect(within(cell).getByText("No rows")).toHaveClass("bg-slate-100");
    expect(cell.textContent).toBe(`No rows${note}`);
  });

  it("names the mapping's status when it differs from what the rows are", () => {
    render(<PatientWearableCodes codes={[
      // Accepted rows whose mapping has since been un-approved.
      code({ mapping_status: "proposed" }),
      // Unresolved rows whose mapping is approved: Generate OMOP has not run for them yet.
      apple({ mapping_status: "approved", wearable: { status: "unresolved", accepted_rows: 0, unresolved_rows: 2 } }),
    ]} />);
    expect(status("garmin-steps").textContent).toBe("Acceptedmapping proposed");
    expect(status("apple-HKQuantityTypeIdentifierStepCount").textContent).toBe("Unresolvedmapping approved");
  });
});
