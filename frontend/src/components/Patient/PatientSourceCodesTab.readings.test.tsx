import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
import PatientSourceCodesTab from "./PatientSourceCodesTab";

const auth = vi.hoisted(() => ({ user: {} as Record<string, unknown> }));
vi.mock("../../hooks/useAuth", () => ({ useAuth: () => ({ currentUser: auth.user }) }));
vi.mock("../../api/axios", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const code = (source_value: string, omop_table: string, status: string, vocabulary: string, extra: Record<string, unknown> = {}) => ({
  source_value, source_code: source_value, source_vocabulary_id: vocabulary, omop_table,
  concept_id: 0, concept_name: null, row_count: 3,
  mapping_id: status === "unmapped" ? null : 5, mapping_status: status,
  mapping_target_concept_id: null, mapping_target_concept_name: null,
  source_unit: "", example_quantity: "", source_metadata: {}, source_code_description: `${source_value} name`,
  ...extra,
});

const CODES = [
  code("GLU-1", "measurement", "unmapped", "HT-Epic"),
  code("C50.9", "condition", "proposed", "ICD10CM"),
  code("RX-1", "drug_exposure", "approved", "RxNorm"),
  code("steps", "observation", "approved", "Garmin", {
    wearable: {
      device: "garmin", source: "Garmin", metric_key: "steps", rows: 30, accepted_rows: 30,
      provisional_rows: 0, unresolved_rows: 0, confidence: null, first_date: "2026-10-24",
      last_date: "2026-11-22", status: "accepted", fields: [],
    },
  }),
];

const reading = (id: number, date: string, value: number | null, extra: Record<string, unknown> = {}) => ({
  id, date, datetime: null, value_as_number: value, value_as_string: null, unit: null,
  source_value: "GLU-1", concept_id: 0, status: "unresolved", confidence: null, is_erroneous: false, ...extra,
});

const rowRequests = () => vi.mocked(api.get).mock.calls.filter(([url]) => String(url).includes("source-code-rows"));

function serve(rowsFor: (params: Record<string, unknown>) => unknown = () => ({ rows: [], next_cursor: null })) {
  vi.mocked(api.get).mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url.includes("source-code-rows")) return Promise.resolve({ data: rowsFor(config?.params ?? {}) }) as never;
    if (url.includes("source-codes")) {
      return Promise.resolve({
        data: { person_id: 7, source_codes: CODES, summary: { total: 4, unmapped: 1, proposed: 1, approved: 2 } },
      }) as never;
    }
    return Promise.resolve({ data: {} }) as never;
  });
}

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  auth.user = { email: "curator@example.com", is_staff: true };
});

const section = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}`) }).closest("section")!;

describe("PatientSourceCodesTab readings under every code (#2863)", () => {
  it("puts a closed arrow under each code of each section, and reads nothing until one is opened", async () => {
    serve();
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));        // starts folded

    for (const name of ["HT-Epic GLU-1", "ICD10CM C50.9", "RxNorm RX-1", "Garmin steps"]) {
      const arrow = screen.getByRole("button", { name: `Show readings of ${name}` });
      expect(arrow).toHaveAttribute("aria-expanded", "false");
    }
    expect(within(section("Unmapped")).getByRole("button", { name: /readings of HT-Epic GLU-1/ })).toHaveTextContent("Readings");
    expect(within(section("Proposed")).getByRole("button", { name: /readings of ICD10CM C50.9/ })).toBeInTheDocument();
    expect(within(section("Approved")).getByRole("button", { name: /readings of RxNorm RX-1/ })).toBeInTheDocument();
    // Only a wearable's count is exact.
    expect(screen.getByRole("button", { name: /readings of Garmin steps/ })).toHaveTextContent("Readings (30)");
    expect(rowRequests()).toHaveLength(0);
  });

  it("opens a hospital measurement code to its rows, newest first, and asks for that code only", async () => {
    serve(() => ({
      rows: [
        reading(2, "2026-03-01", 6.1, { unit: "mg/dL", datetime: "2026-03-01T08:30:00+00:00", concept_id: 3004410, status: "accepted" }),
        reading(1, "2026-02-01", 5.9, { unit: "mg/dL" }),
      ],
      next_cursor: null,
    }));
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");

    await userEvent.click(screen.getByRole("button", { name: "Show readings of HT-Epic GLU-1" }));

    const table = await screen.findByRole("table", { name: "Readings of HT-Epic GLU-1" });
    expect(within(table).getAllByTestId("reading").map((row) => row.textContent)).toEqual([
      "2026-03-01 08:30 UTC6.1mg/dLAccepted",
      "2026-02-01date only5.9mg/dLUnresolved",
    ]);
    expect(rowRequests()).toEqual([[
      "/v1/patient-records/7/source-code-rows/",
      { params: { table: "measurement", code: "GLU-1", vocabulary: "HT-Epic" } },
    ]]);
  });

  it("opens a condition and a drug code, with no value for a condition", async () => {
    serve((params) => ({
      rows: [reading(9, "2026-01-05", params.table === "drug_exposure" ? 2.5 : null,
        { unit: params.table === "drug_exposure" ? "mg" : null, source_value: String(params.code) })],
      next_cursor: null,
    }));
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");
    fireEvent.click(screen.getByRole("button", { name: /^Approved/ }));

    await userEvent.click(screen.getByRole("button", { name: "Show readings of ICD10CM C50.9" }));
    await userEvent.click(screen.getByRole("button", { name: "Show readings of RxNorm RX-1" }));

    const condition = await screen.findByRole("table", { name: "Readings of ICD10CM C50.9" });
    expect(within(condition).getByTestId("reading").textContent).toBe("2026-01-05date only——Unresolved");
    const drug = await screen.findByRole("table", { name: "Readings of RxNorm RX-1" });
    expect(within(drug).getByTestId("reading").textContent).toBe("2026-01-05date only2.5mgUnresolved");
    expect(rowRequests().map(([, config]) => (config as { params: unknown }).params)).toEqual([
      { table: "condition", code: "C50.9", vocabulary: "ICD10CM" },
      { table: "drug_exposure", code: "RX-1", vocabulary: "RxNorm" },
    ]);
  });

  it("pages a code's rows with Show more", async () => {
    serve((params) => (params.cursor
      ? { rows: [reading(1, "2026-01-01", 1)], next_cursor: null }
      : { rows: [reading(2, "2026-01-02", 2)], next_cursor: "more" }));
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");
    await userEvent.click(screen.getByRole("button", { name: "Show readings of HT-Epic GLU-1" }));
    await screen.findAllByTestId("reading");
    expect(rowRequests()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(() => expect(screen.getAllByTestId("reading")).toHaveLength(2));
    expect(rowRequests()).toHaveLength(2);
  });

  it("folds again, and opening the arrow does not open the mapping dialog", async () => {
    auth.user = { email: "curator@example.com", is_staff: true };
    serve(() => ({ rows: [reading(1, "2026-01-01", 1)], next_cursor: null }));
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");

    const arrow = () => screen.getByRole("button", { name: /readings of HT-Epic GLU-1/ });
    await userEvent.click(arrow());
    await screen.findByTestId("reading");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await userEvent.click(arrow());
    expect(screen.queryByTestId("reading")).not.toBeInTheDocument();
    expect(arrow()).toHaveAttribute("aria-expanded", "false");
  });

  it("is on the keyboard too", async () => {
    serve(() => ({ rows: [reading(1, "2026-01-01", 1)], next_cursor: null }));
    render(<PatientSourceCodesTab personId="7" />);
    await screen.findByTestId("wearable-codes");
    screen.getByRole("button", { name: /readings of HT-Epic GLU-1/ }).focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByTestId("reading")).toBeInTheDocument();
  });
});
