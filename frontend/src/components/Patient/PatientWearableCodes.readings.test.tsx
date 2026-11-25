import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
import PatientWearableCodes from "./PatientWearableCodes";
import type { WearableSourceCode } from "../../types/sourceCodes";

vi.mock("../../api/axios", () => ({
  default: { get: vi.fn() },
}));

const get = vi.mocked(api.get);

const steps = (rows = 3): WearableSourceCode => ({
  source_value: "steps", omop_table: "observation", concept_id: 40758552, concept_name: "Number of steps",
  row_count: rows, mapping_id: 41, mapping_status: "approved",
  mapping_target_concept_id: 40758552, mapping_target_concept_name: "Number of steps",
  mapping_destination_concept_code: "55423-8", mapping_destination_vocabulary_id: "LOINC",
  source_vocabulary_id: "Garmin", source_code: "steps", source_unit: "", example_quantity: "",
  source_metadata: {}, source_code_description: "Daily step count",
  wearable: {
    device: "garmin", source: "Garmin", metric_key: "steps", rows, accepted_rows: rows,
    provisional_rows: 0, unresolved_rows: 0, confidence: null, first_date: "2026-11-01",
    last_date: "2026-11-03", status: "accepted", fields: [],
  },
});

const reading = (id: number, date: string, value: number, extra: Record<string, unknown> = {}) => ({
  id, date, datetime: null, value_as_number: value, value_as_string: null, unit: "/d",
  source_value: "steps", concept_id: 40758552, status: "accepted", confidence: null,
  is_erroneous: false, ...extra,
});

const arrow = () => screen.getByRole("button", { name: /(Show|Hide) readings of Garmin steps/ });

describe("PatientWearableCodes readings", () => {
  beforeEach(() => {
    get.mockReset();
  });

  it("has an arrow under each code, closed, that reads nothing until it is opened", () => {
    render(<PatientWearableCodes codes={[steps()]} personId="7" />);
    expect(arrow()).toHaveAttribute("aria-expanded", "false");
    expect(arrow()).toHaveTextContent("Readings (3)");
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByRole("table", { name: "Readings of Garmin steps" })).not.toBeInTheDocument();
  });

  it("has no arrow without a patient to read for, or for a code with no rows", () => {
    const { rerender } = render(<PatientWearableCodes codes={[steps()]} />);
    expect(screen.queryByRole("button", { name: /readings of/i })).not.toBeInTheDocument();
    rerender(<PatientWearableCodes codes={[steps(0)]} personId="7" />);
    expect(screen.queryByRole("button", { name: /readings of/i })).not.toBeInTheDocument();
  });

  it("opens to the stored readings, newest first, with datetime or date, value, unit and status", async () => {
    get.mockResolvedValue({ data: {
      rows: [
        reading(3, "2026-11-03", 9100, { datetime: "2026-11-03T21:45:00+00:00", status: "provisional", confidence: 70 }),
        reading(2, "2026-11-02", 8000, { concept_id: 0, status: "unresolved" }),
        reading(1, "2026-11-01", 7000),
      ],
      next_cursor: null,
    } });
    render(<PatientWearableCodes codes={[steps()]} personId="7" />);

    await userEvent.click(arrow());

    expect(arrow()).toHaveAttribute("aria-expanded", "true");
    expect(get).toHaveBeenCalledWith("/v1/patient-records/7/source-code-rows/", {
      params: { table: "observation", code: "steps", vocabulary: "Garmin" },
    });
    const table = await screen.findByRole("table", { name: "Readings of Garmin steps" });
    const rows = within(table).getAllByTestId("reading").map((row) => row.textContent);
    expect(rows).toEqual([
      "2026-11-03 21:45 UTC9100/dProvisional 70%",
      "2026-11-02date only8000/dUnresolved",
      "2026-11-01date only7000/dAccepted",
    ]);
    // A day-only row says so, and is given no time of day.
    expect(within(table).getAllByTitle("Only a date is stored for this reading; no time of day was recorded.")).toHaveLength(2);
    expect(within(table).queryByText(/00:00/)).not.toBeInTheDocument();
  });

  it("pages with Show more, appending the next page with its cursor", async () => {
    get.mockResolvedValueOnce({ data: { rows: [reading(3, "2026-11-03", 3), reading(2, "2026-11-02", 2)], next_cursor: "abc" } });
    get.mockResolvedValueOnce({ data: { rows: [reading(1, "2026-11-01", 1)], next_cursor: null } });
    render(<PatientWearableCodes codes={[steps()]} personId="7" />);
    await userEvent.click(arrow());
    await screen.findAllByTestId("reading");

    await userEvent.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(() => expect(screen.getAllByTestId("reading")).toHaveLength(3));
    expect(get).toHaveBeenLastCalledWith("/v1/patient-records/7/source-code-rows/", {
      params: { table: "observation", code: "steps", vocabulary: "Garmin", cursor: "abc" },
    });
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("closes again, dropping the rows, and does not open the mapping dialog", async () => {
    const onRowClick = vi.fn();
    get.mockResolvedValue({ data: { rows: [reading(1, "2026-11-01", 1)], next_cursor: null } });
    render(<PatientWearableCodes codes={[steps()]} personId="7" onRowClick={onRowClick} />);
    await userEvent.click(arrow());
    await screen.findByTestId("reading");
    await userEvent.click(arrow());
    expect(screen.queryByTestId("reading")).not.toBeInTheDocument();
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("is operable from the keyboard", async () => {
    get.mockResolvedValue({ data: { rows: [reading(1, "2026-11-01", 1)], next_cursor: null } });
    render(<PatientWearableCodes codes={[steps()]} personId="7" />);
    arrow().focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByTestId("reading")).toBeInTheDocument();
    await userEvent.keyboard(" ");
    expect(screen.queryByTestId("reading")).not.toBeInTheDocument();
  });

  it("says when the readings cannot be read", async () => {
    get.mockRejectedValue({ response: { data: { detail: "Only administrators can view source codes." } } });
    render(<PatientWearableCodes codes={[steps()]} personId="7" />);
    fireEvent.click(arrow());
    expect(await screen.findByRole("alert")).toHaveTextContent("Only administrators can view source codes.");
  });
});
