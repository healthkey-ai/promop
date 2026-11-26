import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
import PatientSourceCodesTab from "./PatientSourceCodesTab";

const STAFF = { email: "curator@example.com", is_staff: true };
// A doctor without the analyst grant reads the patient's codes and maps none (#2416).
const DOCTOR = {
  id: 9, email: "doctor@example.com", is_staff: false,
  org_accesses: [{ org_slug: "acme", org_name: "Acme", role: "doctor", expires_at: null }],
};
const auth = vi.hoisted(() => ({ user: {} as Record<string, unknown> }));
vi.mock("../../hooks/useAuth", () => ({ useAuth: () => ({ currentUser: auth.user }) }));

vi.mock("../../api/axios", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const base = {
  omop_table: "measurement", concept_id: 0, concept_name: null, row_count: 3,
  mapping_id: null, mapping_status: "unmapped",
  mapping_target_concept_id: null, mapping_target_concept_name: null,
  source_unit: "", example_quantity: "", source_metadata: {},
};

const hospital = (source_code: string, description: string, overrides: Record<string, unknown> = {}) => ({
  ...base, source_value: source_code, source_code, source_vocabulary_id: "LOINC",
  source_code_description: description, ...overrides,
});

const wearable = (overrides: Record<string, unknown> = {}) => ({
  device: "garmin", source: "Garmin", metric_key: "steps",
  rows: 30, accepted_rows: 30, provisional_rows: 0, unresolved_rows: 0, confidence: null,
  first_date: "2026-10-24", last_date: "2026-11-22", status: "accepted",
  fields: [{ name: "median_daily_steps_30d", label: "Median Daily Steps" }],
  ...overrides,
});

const HOSPITAL = [
  hospital("8716-3", "Blood Pressure"),
  hospital("9279-1", "Respirations", { mapping_id: 77, mapping_status: "proposed" }),
  hospital("8867-4", "Pulse", {
    mapping_id: 78, mapping_status: "approved", concept_id: 3027018,
    mapping_target_concept_id: 3027018, mapping_target_concept_name: "Heart rate",
    mapping_destination_concept_code: "8867-4", mapping_destination_vocabulary_id: "LOINC",
  }),
];

const DEVICE = [
  // Approved, rows accepted.
  {
    ...base, source_value: "steps", source_code: "steps", source_vocabulary_id: "Garmin",
    source_code_description: "Daily step count", row_count: 30,
    mapping_id: 41, mapping_status: "approved", concept_id: 40758552,
    mapping_target_concept_id: 40758552, mapping_target_concept_name: "Number of steps",
    mapping_destination_concept_code: "55423-8", mapping_destination_vocabulary_id: "LOINC",
    wearable: wearable(),
  },
  // Proposed, rows provisional.
  {
    ...base, source_value: "HKQuantityTypeIdentifierVO2Max", source_code: "HKQuantityTypeIdentifierVO2Max",
    source_vocabulary_id: "Apple", source_code_description: "VO2 max", row_count: 4,
    mapping_id: 42, mapping_status: "proposed",
    mapping_target_concept_id: 37174372, mapping_target_concept_name: "Maximal oxygen uptake",
    mapping_destination_concept_code: "60842-2", mapping_destination_vocabulary_id: "LOINC",
    wearable: wearable({
      device: "apple", source: "Apple Health", metric_key: "vo2_max", status: "provisional",
      rows: 4, accepted_rows: 0, provisional_rows: 4, confidence: 70,
      fields: [{ name: "vo2_max_avg_30d", label: "VO2 Max" }],
    }),
  },
  // No mapping, rows unresolved.
  {
    ...base, source_value: "FloorsClimbedRecord", source_code: "FloorsClimbedRecord",
    source_vocabulary_id: "HealthConnect", source_code_description: "", row_count: 2,
    wearable: wearable({
      device: "android", source: "Health Connect", metric_key: "", status: "unresolved",
      rows: 2, accepted_rows: 0, unresolved_rows: 2, first_date: "2026-11-22", last_date: "2026-11-22", fields: [],
    }),
  },
];

function serve(source_codes: Array<Record<string, unknown>>) {
  const count = (status: string) => source_codes.filter((sc) => sc.mapping_status === status).length;
  // The server's summary counts every code, a device's included.
  const summary = {
    total: source_codes.length, unmapped: count("unmapped"), proposed: count("proposed"), approved: count("approved"),
  };
  vi.mocked(api.get).mockImplementation((url: string) => Promise.resolve({
    data: url.includes("source-codes") ? { person_id: 48095, summary, source_codes } : {},
  }) as never);
}

/** The collapsible status section whose header button reads `label`. */
const statusSection = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}`) }).closest("section")!;

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  auth.user = STAFF;
});

describe("PatientSourceCodesTab wearables (#2835)", () => {
  it("lists a device's codes first, above the status sections", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);

    const wearables = await screen.findByTestId("wearable-codes");
    expect(within(wearables).getByRole("heading", { name: "Wearables" })).toBeInTheDocument();
    expect(wearables).toHaveTextContent("Wearables(3)");
    const unmapped = statusSection("Unmapped");
    expect(wearables.compareDocumentPosition(unmapped) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(wearables.compareDocumentPosition(statusSection("Proposed")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(wearables.compareDocumentPosition(statusSection("Approved")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Generate OMOP stays where it was, above them all.
    const generate = screen.getByRole("button", { name: "Generate OMOP" });
    expect(generate.compareDocumentPosition(wearables) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says what each device code's rows are", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);

    expect((await screen.findByTestId("wearable-status-garmin-steps")).textContent).toBe("Accepted");
    expect(screen.getByTestId("wearable-status-apple-HKQuantityTypeIdentifierVO2Max").textContent).toBe("Provisional 70%");
    expect(screen.getByTestId("wearable-status-android-FloorsClimbedRecord").textContent).toBe("Unresolvedno mapping");
    const steps = within(screen.getByTestId("wearable-code-garmin-steps")).getAllByRole("cell");
    expect(steps.map((cell) => cell.textContent)).toEqual([
      "GarminstepsDaily step count", "LOINC 55423-8Number of steps", "Measurement", "30",
      "2026-10-24 to 2026-11-22", "Accepted", "Median Daily Steps",
    ]);
  });

  it("does not repeat a device code in the Unmapped, Proposed or Approved section", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);
    await screen.findByTestId("wearable-codes");
    // Approved starts folded.
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));

    for (const code of ["steps", "HKQuantityTypeIdentifierVO2Max", "FloorsClimbedRecord"]) {
      expect(screen.getAllByText(code)).toHaveLength(1);
      expect(screen.getByText(code).closest("section")).toBe(screen.getByTestId("wearable-codes"));
    }
    // Each status section holds its one hospital code and counts only that.
    expect(statusSection("Unmapped")).toHaveTextContent("Unmapped(1)");
    // The header, the code, and the arrow row that opens its readings (#2863).
    expect(within(statusSection("Unmapped")).getAllByRole("row")).toHaveLength(3);
    expect(within(statusSection("Unmapped")).getByText("Blood Pressure")).toBeInTheDocument();
    expect(statusSection("Proposed")).toHaveTextContent("Proposed(1)");
    expect(within(statusSection("Proposed")).getByText("Respirations")).toBeInTheDocument();
    expect(statusSection("Approved")).toHaveTextContent("Approved(1)");
    expect(within(statusSection("Approved")).getByText("Pulse")).toBeInTheDocument();
  });

  it("names the wearable codes in the summary and counts the rest by status, adding up to the total", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);
    await screen.findByTestId("wearable-codes");

    expect(screen.getByText(/source\s+codes/).closest("div")).toHaveTextContent(
      "6 source codes — 3 wearable, 1 unmapped, 1 proposed, 1 approved");
  });

  it("shows a patient with only device codes the Wearables section and no empty status section", async () => {
    serve(DEVICE);
    render(<PatientSourceCodesTab personId="48095" />);

    expect(await screen.findByTestId("wearable-codes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Unmapped|Proposed|Approved)/ })).not.toBeInTheDocument();
    expect(screen.queryByText("No source codes found.")).not.toBeInTheDocument();
    expect(screen.getByText(/source\s+codes/).closest("div")).toHaveTextContent(
      "3 source codes — 3 wearable, 0 unmapped, 0 proposed, 0 approved");
  });

  it("shows no Wearables section, and the summary as before, for a patient with no device codes", async () => {
    serve(HOSPITAL);
    render(<PatientSourceCodesTab personId="48095" />);

    expect(await screen.findByText("Blood Pressure")).toBeInTheDocument();
    expect(screen.queryByTestId("wearable-codes")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Wearables" })).not.toBeInTheDocument();
    const summary = screen.getByText(/source\s+codes/).closest("div")!;
    expect(summary).toHaveTextContent("3 source codes — 1 unmapped, 1 proposed, 1 approved");
    expect(summary).not.toHaveTextContent("wearable");
  });

  it("opens Edit Mapping from a wearable row as from any other row", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);

    const row = await screen.findByTestId("wearable-code-apple-HKQuantityTypeIdentifierVO2Max");
    expect(row).toHaveClass("cursor-pointer");
    fireEvent.click(within(row).getByText("VO2 max"));
    expect(await screen.findByRole("dialog", { name: "Edit Mapping" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Source Description/)).toHaveValue("VO2 max");
    expect(screen.getByLabelText(/Destination Concept ID/)).toHaveValue(37174372);
  });

  it("opens New Mapping from a wearable row whose code has no mapping", async () => {
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);

    fireEvent.click(await screen.findByTestId("wearable-status-android-FloorsClimbedRecord"));
    expect(await screen.findByRole("dialog", { name: "New Mapping" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Destination Concept ID/)).toHaveValue(null);
  });

  it("opens nothing from a wearable row for a reader", async () => {
    auth.user = DOCTOR;
    serve([...HOSPITAL, ...DEVICE]);
    render(<PatientSourceCodesTab personId="48095" />);

    const row = await screen.findByTestId("wearable-code-garmin-steps");
    expect(screen.getByText("Read-only: mapping codes needs the analyst role.")).toBeInTheDocument();
    expect(row).not.toHaveClass("cursor-pointer");
    fireEvent.click(row);
    fireEvent.click(within(row).getByText("Daily step count"));
    fireEvent.click(screen.getByTestId("wearable-status-garmin-steps"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Source Description/)).not.toBeInTheDocument();
    // The reader still sees what the rows are.
    expect(screen.getByTestId("wearable-status-garmin-steps").textContent).toBe("Accepted");
  });
});
