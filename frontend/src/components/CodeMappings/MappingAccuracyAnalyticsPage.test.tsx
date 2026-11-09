import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MappingAccuracyAnalyticsPage, { type AccuracySection } from "./MappingAccuracyAnalyticsPage";

const get = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...args: unknown[]) => get(...args) } }));

const row = (key: string, label: string, overrides: Partial<AccuracySection["rows"][number]> = {}) => ({
  key, label, total: 0, approved: 0, proposed: 0, rejected: 0, precision: null, recall: null, f1: null, ...overrides,
});
const sections: AccuracySection[] = [
  { key: "overall", label: "Overall", rows: [
    row("overall", "Overall", { total: 12, approved: 8, proposed: 3, rejected: 1, precision: 0.875, recall: 1, f1: 0.9333 }),
    row("90-100", "90–100% confidence", { total: 5, approved: 5, precision: 1, recall: 1, f1: 1 }),
    row("80-90", "80–90%"), row("70-80", "70–80%"), row("below-70", "Below 70%", { total: 1, rejected: 1, precision: 0, recall: 0, f1: 0 }),
  ] },
  { key: "concept2code", label: "Concept to Code", rows: [row("overall", "Overall", { total: 4, approved: 4 })] },
  { key: "code2concept", label: "Code to Concept", rows: [row("overall", "Overall")] },
  { key: "patientcode", label: "Patient source codes", rows: [row("overall", "Overall")] },
];

beforeEach(() => { get.mockReset(); });

describe("MappingAccuracyAnalyticsPage (#2383)", () => {
  it("shows one table per surface with the confidence bands and the scored columns", async () => {
    get.mockResolvedValue({ data: { sections } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    const overall = await screen.findByRole("region", { name: "Overall" });
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/accuracy/analytics/");
    const headers = within(overall).getAllByRole("columnheader").map(h => h.textContent);
    expect(headers).toEqual(["Confidence", "Total", "Approved", "Proposed", "Rejected", "Precision", "Recall", "F1"]);
    const first = within(overall).getAllByRole("row")[1];
    expect(within(first).getAllByRole("cell").map(c => c.textContent))
      .toEqual(["Overall", "12", "8", "3", "1", "87.5%", "100.0%", "93.3%"]);
    expect(within(overall).getByText("Below 70%").closest("tr")).toHaveTextContent("0.0%");
    expect(within(overall).getByText("80–90%").closest("tr")).toHaveTextContent("—");
    for (const label of ["Concept to Code", "Code to Concept", "Patient source codes"]) {
      expect(screen.getByRole("region", { name: label })).toBeInTheDocument();
    }
  });

  it("reports a failed load", async () => {
    get.mockRejectedValue(new Error("boom"));
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    expect(await screen.findByText("Failed to load mapping accuracy analytics.")).toBeInTheDocument();
  });
});
