import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
import PatientOmopTab from "./PatientOmopTab";
import { WEARABLE_LABEL, orderTables } from "./omopRows";

vi.mock("../../api/axios", () => ({
  default: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

const group = (overrides: Record<string, unknown> = {}) => ({
  concept_id: 3004410, concept_name: "Hemoglobin A1c", concept_code: "4548-4", vocabulary_id: "LOINC",
  source_value: null, count: 3, provisional_rows: 0, rows_with_time: 3, wearable: false, wearable_rows: 0,
  latest_date: "2024-03-05", latest_datetime: "2024-03-05T09:15:00+00:00",
  earliest_date: "2024-01-01", earliest_datetime: "2024-01-01T08:30:00+00:00",
  ...overrides,
});

const raw = (key: string, label: string, rows: Record<string, unknown>[] = []) => ({
  key, label, count: rows.length, rows,
});

// The server lists conditions and drugs before measurements; the tab reorders.
const payload = (overrides: Record<string, unknown> = {}) => ({
  person_id: 7,
  tables: [
    raw("person", "Person", [{ person_id: 7, given_name: "Ada" }]),
    raw("condition_occurrences", "Condition Occurrences", [{ condition_occurrence_id: 11, condition_source_value: "MYELOMA-ROW" }]),
    raw("drug_exposures", "Drug Exposures"),
    {
      key: "measurements", label: "Measurements", count: 5, rows: [], grouped: true, truncated: false,
      groups: [
        group(),
        group({
          concept_id: 3027018, concept_name: "Heart rate", concept_code: "8867-4", count: 2,
          provisional_rows: 1, rows_with_time: 0, wearable: true, wearable_rows: 2,
          latest_date: "2024-04-01", latest_datetime: null, earliest_date: "2024-03-31", earliest_datetime: null,
        }),
      ],
      unresolved_groups: [
        group({ concept_id: 0, concept_name: undefined, concept_code: undefined, vocabulary_id: undefined,
          source_value: "spo2", count: 1, wearable: true, wearable_rows: 1, rows_with_time: 0,
          latest_datetime: null, earliest_datetime: null }),
        group({ concept_id: 0, concept_name: undefined, concept_code: undefined, vocabulary_id: undefined,
          source_value: "spo2", count: 1, wearable: false, rows_with_time: 0,
          latest_datetime: null, earliest_datetime: null }),
      ],
    },
    {
      key: "observations", label: "Observations", count: 0, rows: [], grouped: true, truncated: false,
      groups: [], unresolved_groups: [],
    },
    raw("procedure_occurrences", "Procedure Occurrences"),
    ...(overrides.tables as unknown[] ?? []),
  ],
});

const rowsResponse = (rows: Record<string, unknown>[], next: string | null = null) => ({ data: { rows, next_cursor: next } });

const reading = (id: number, date: string, value: number, extra: Record<string, unknown> = {}) => ({
  id, date, datetime: null, value_as_number: value, value_as_string: null, unit: "mg/dL",
  source_value: "GLU", concept_id: 3004410, status: "accepted", confidence: null, is_erroneous: false, ...extra,
});

function mockApi(rowsFor: (url: string, params: Record<string, unknown>) => unknown = () => rowsResponse([])) {
  get.mockImplementation(async (url: string, config?: { params?: Record<string, unknown> }) => {
    if (url.endsWith("/omop/")) return { data: payload() };
    return rowsFor(url, config?.params ?? {}) as never;
  });
}

const section = (key: string) => screen.getByTestId(`omop-table-${key}`);
const header = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}`) });

describe("PatientOmopTab", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("orders the tables Person, Measurements, Observations, then the rest as the server lists them", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual([
      "Person", "Measurements", "Observations",
      "Condition Occurrences", "Drug Exposures", "Procedure Occurrences",
    ]);
  });

  it("orders by key without disturbing the tables it does not name", () => {
    const keys = orderTables([
      raw("episodes", "Episodes"), raw("observations", "Observations"), raw("death", "Death"),
      raw("measurements", "Measurements"), raw("person", "Person"),
    ]).map((t) => t.key);
    expect(keys).toEqual(["person", "measurements", "observations", "episodes", "death"]);
  });

  it("opens Person, Measurements and Observations and closes the others, keeping every row count", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");

    for (const label of ["Person", "Measurements", "Observations"]) {
      expect(header(label)).toHaveAttribute("aria-expanded", "true");
    }
    for (const label of ["Condition Occurrences", "Drug Exposures", "Procedure Occurrences"]) {
      expect(header(label)).toHaveAttribute("aria-expanded", "false");
    }
    // The counts stay on a closed header.
    expect(section("condition_occurrences")).toHaveTextContent("1 row");
    expect(section("drug_exposures")).toHaveTextContent("0 rows");
    expect(section("measurements")).toHaveTextContent("5 rows");
  });

  it("does not render the rows of a closed section, and renders them once it is opened", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    expect(screen.queryByText("MYELOMA-ROW")).not.toBeInTheDocument();
    expect(within(section("drug_exposures")).queryByText("No rows")).not.toBeInTheDocument();

    await userEvent.click(header("Condition Occurrences"));
    expect(header("Condition Occurrences")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("MYELOMA-ROW")).toBeInTheDocument();

    await userEvent.click(header("Drug Exposures"));
    expect(within(section("drug_exposures")).getByText("No rows")).toBeInTheDocument();

    await userEvent.click(header("Condition Occurrences"));
    expect(screen.queryByText("MYELOMA-ROW")).not.toBeInTheDocument();
  });

  it("closes an open section from the keyboard", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    expect(screen.getByText("Ada")).toBeInTheDocument();
    header("Person").focus();
    await userEvent.keyboard("{Enter}");
    expect(header("Person")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Ada")).not.toBeInTheDocument();
    await userEvent.keyboard(" ");
    expect(header("Person")).toHaveAttribute("aria-expanded", "true");
  });

  it("lists measurements by concept, closed, and reads no row until a concept is opened", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    const list = screen.getByRole("list", { name: "Measurements by concept" });
    const items = within(list).getAllByTestId("omop-group");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Hemoglobin A1c");
    expect(items[0]).toHaveTextContent("LOINC 4548-4");
    expect(items[0]).toHaveTextContent("3 rows");
    expect(items[0]).toHaveTextContent("Latest 2024-03-05 09:15 UTC");
    expect(items[0]).toHaveTextContent("Earliest 2024-01-01 08:30 UTC");
    // A group that stores only days shows the days, as such, and counts its provisional rows.
    expect(items[1]).toHaveTextContent("Latest 2024-04-01date only");
    expect(items[1]).toHaveTextContent("1 provisional");
    expect(get.mock.calls.filter(([url]) => String(url).includes("omop-rows"))).toHaveLength(0);
    expect(within(items[0]).getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("opens a concept to its rows, newest first, with the datetime and source value", async () => {
    mockApi((_url, params) => {
      expect(params).toEqual({ table: "measurements", concept: 3004410 });
      return rowsResponse([
        reading(3, "2024-03-05", 7.1, { datetime: "2024-03-05T09:15:00+00:00" }),
        reading(2, "2024-02-01", 6.9, { status: "provisional", confidence: 70 }),
        reading(1, "2024-01-01", 6.5, { datetime: "2024-01-01T08:30:00+00:00" }),
      ]);
    });
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");

    await userEvent.click(within(screen.getAllByTestId("omop-group")[0]).getByRole("button"));

    const table = await screen.findByRole("table", { name: "Hemoglobin A1c rows" });
    expect(within(table).getAllByTestId("reading").map((row) => row.textContent)).toEqual([
      "2024-03-05 09:15 UTC7.1mg/dLGLUAccepted",
      "2024-02-01date only6.9mg/dLGLUProvisional 70%",
      "2024-01-01 08:30 UTC6.5mg/dLGLUAccepted",
    ]);
    expect(get).toHaveBeenCalledWith("/v1/patient-records/7/omop-rows/", {
      params: { table: "measurements", concept: 3004410 },
    });
  });

  it("pages a concept's rows with Show more and never loads them all up front", async () => {
    let calls = 0;
    mockApi((_url, params) => {
      calls += 1;
      return params.cursor
        ? rowsResponse([reading(1, "2024-01-01", 1)])
        : rowsResponse([reading(2, "2024-01-02", 2)], "next-page");
    });
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    await userEvent.click(within(screen.getAllByTestId("omop-group")[0]).getByRole("button"));
    await screen.findAllByTestId("reading");
    expect(calls).toBe(1);

    await userEvent.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(() => expect(screen.getAllByTestId("reading")).toHaveLength(2));
    expect(get).toHaveBeenLastCalledWith("/v1/patient-records/7/omop-rows/", {
      params: { table: "measurements", concept: 3004410, cursor: "next-page" },
    });
  });

  it("puts rows at concept 0 under an Unresolved heading, by source value, each reading its own rows", async () => {
    mockApi(() => rowsResponse([reading(5, "2024-01-01", 97, { concept_id: 0, status: "unresolved", source_value: "spo2" })]));
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");

    expect(screen.getByRole("heading", { level: 4, name: /Unresolved/ })).toBeInTheDocument();
    const unresolved = within(screen.getByRole("list", { name: "Measurements, unresolved" })).getAllByTestId("omop-group");
    expect(unresolved).toHaveLength(2);
    expect(unresolved[0]).toHaveTextContent("spo2");

    await userEvent.click(within(unresolved[1]).getByRole("button"));
    expect(get).toHaveBeenCalledWith("/v1/patient-records/7/omop-rows/", {
      params: { table: "measurements", concept: 0, source_value: "spo2", wearable: "false" },
    });
    await userEvent.click(within(unresolved[0]).getByRole("button"));
    expect(get).toHaveBeenCalledWith("/v1/patient-records/7/omop-rows/", {
      params: { table: "measurements", concept: 0, source_value: "spo2", wearable: "true" },
    });
  });

  it("marks a wearable's group with a purple watch, and a hospital's group of the same code with none", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");

    const concepts = screen.getAllByTestId("omop-group");
    expect(within(concepts[0]).queryByTestId("wearable-mark")).not.toBeInTheDocument();
    const mark = within(concepts[1]).getByTestId("wearable-mark");
    expect(mark).toHaveAttribute("aria-label", WEARABLE_LABEL);
    expect(mark).toHaveAttribute("title", WEARABLE_LABEL);
    expect(within(concepts[1]).getByRole("img", { name: WEARABLE_LABEL })).toBeInTheDocument();
    expect(mark.querySelector("svg")).toHaveClass("text-purple-600");

    // Two unresolved groups of the one spelling: only the device's is marked.
    const unresolved = within(screen.getByRole("list", { name: "Measurements, unresolved" })).getAllByTestId("omop-group");
    expect(within(unresolved[0]).getByTestId("wearable-mark")).toBeInTheDocument();
    expect(within(unresolved[1]).queryByTestId("wearable-mark")).not.toBeInTheDocument();
  });

  it("says plainly when a grouped table is empty, and when groups were left out", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" />);
    await screen.findByTestId("omop-table-person");
    expect(within(section("observations")).getByText("No rows")).toBeInTheDocument();
  });

  it("says when the request fails", async () => {
    get.mockRejectedValue({ response: { data: { detail: "Only administrators can view raw OMOP rows." } } });
    render(<PatientOmopTab personId="7" />);
    expect(await screen.findByText("Only administrators can view raw OMOP rows.")).toBeInTheDocument();
  });
});

describe("PatientOmopTab opened at a group (#2887)", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("opens the group a 30-day wearable value pointed at, and reads only its rows", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" focus={{ table: "measurements", concept_id: 3027018, source_value: null, wearable: true }} />);
    const rows = await screen.findAllByTestId("omop-group");
    const heart = rows.find((row) => within(row).queryByText("Heart rate"));
    expect(heart).toBeTruthy();
    await waitFor(() => expect(within(heart as HTMLElement).getByRole("button", { expanded: true })).toBeInTheDocument());
    const reads = get.mock.calls.filter(([url]) => String(url).includes("omop-rows"));
    expect(reads).toHaveLength(1);
    expect(reads[0][1]).toMatchObject({ params: { table: "measurements", concept: 3027018 } });
  });

  it("opens the device's group of an unresolved spelling, not the hospital's", async () => {
    mockApi();
    render(<PatientOmopTab personId="7" focus={{ table: "measurements", concept_id: 0, source_value: "spo2", wearable: true }} />);
    await screen.findAllByTestId("omop-group");
    await waitFor(() => {
      const reads = get.mock.calls.filter(([url]) => String(url).includes("omop-rows"));
      expect(reads).toHaveLength(1);
      expect(reads[0][1]).toMatchObject({ params: { source_value: "spo2", wearable: "true" } });
    });
  });
});
