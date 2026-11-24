import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MappingAccuracyAnalyticsPage, { type AccuracySection, type AutoApprovedBlock } from "./MappingAccuracyAnalyticsPage";

const get = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...args: unknown[]) => get(...args) } }));

const row = (key: string, label: string, overrides: Partial<AccuracySection["rows"][number]> = {}) => ({
  key, label, total: 0, approved: 0, proposed: 0, rejected: 0, scored: 0, precision: null, recall: null, f1: null, ...overrides,
});
const sections: AccuracySection[] = [
  { key: "overall", label: "Overall", rows: [
    row("overall", "Overall", { total: 12, approved: 8, proposed: 3, rejected: 1, scored: 8, precision: 0.875, recall: 1, f1: 0.9333 }),
    row("90-100", "90–100% confidence", { total: 5, approved: 5, precision: 1, recall: 1, f1: 1 }),
    row("80-90", "80–90%"), row("70-80", "70–80%"), row("below-70", "Below 70%", { total: 1, rejected: 1, precision: 0, recall: 0, f1: 0 }),
  ] },
  { key: "concept2code", label: "Concept to Code", rows: [row("overall", "Overall", { total: 4, approved: 4 })] },
  { key: "code2concept", label: "Code to Concept", rows: [row("overall", "Overall")] },
  { key: "patientcode", label: "Patient source codes", rows: [row("overall", "Overall")] },
];

const autoRow = (key: string, label: string, overrides: Partial<AutoApprovedBlock["rows"][number]> = {}) => ({
  key, label, total: 0, reviewed: 0, confirmed: 0, reversed: 0, unreviewed: 0, precision_of_reviewed: null, ...overrides,
});
const autoApproved: AutoApprovedBlock = {
  key: "auto_approved", label: "Auto-approved by Suggest", rows: [
    autoRow("overall", "Overall", { total: 10, reviewed: 4, confirmed: 3, reversed: 1, unreviewed: 6, precision_of_reviewed: 0.75 }),
    autoRow("90-100", "90–100% confidence", { total: 10, reviewed: 4, confirmed: 3, reversed: 1, unreviewed: 6, precision_of_reviewed: 0.75 }),
    autoRow("80-90", "80–90%"), autoRow("70-80", "70–80%"), autoRow("below-70", "Below 70%"),
  ],
};

beforeEach(() => { get.mockReset(); });

describe("MappingAccuracyAnalyticsPage (#2383)", () => {
  it("says how many reviewed decisions were on a fallback and so are not scored (#2743)", async () => {
    get.mockResolvedValue({ data: { sections, unscored_fallbacks: 1531 } });
    const view = render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    expect(await screen.findByText(/1,531 reviewed decisions were on a fallback destination/)).toHaveTextContent(
      "They count in the status columns and are not Scored: the model never proposed them.");
    view.unmount();

    get.mockResolvedValue({ data: { sections, unscored_fallbacks: 1 } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    expect(await screen.findByText(/1 reviewed decision was on a fallback destination/)).toHaveTextContent(
      "It counts in the status columns and is not Scored: the model never proposed it.");
  });

  it("says nothing about fallbacks when there are none, or when the answer predates the count", async () => {
    get.mockResolvedValue({ data: { sections, unscored_fallbacks: 0 } });
    const view = render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    await screen.findByRole("region", { name: "Overall" });
    expect(screen.queryByText(/fallback/)).not.toBeInTheDocument();
    view.unmount();

    get.mockResolvedValue({ data: { sections } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    await screen.findByRole("region", { name: "Overall" });
    expect(screen.queryByText(/fallback/)).not.toBeInTheDocument();
  });

  it("shows one table per surface with the confidence bands and the scored columns", async () => {
    get.mockResolvedValue({ data: { sections } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    const overall = await screen.findByRole("region", { name: "Overall" });
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/accuracy/analytics/");
    const headers = within(overall).getAllByRole("columnheader").map(h => h.textContent);
    expect(headers).toEqual(["Confidence", "Total", "Approved", "Proposed", "Rejected", "Scored", "Precision", "Recall", "F1"]);
    const first = within(overall).getAllByRole("row")[1];
    expect(within(first).getByRole("rowheader")).toHaveTextContent("Overall");
    expect(within(first).getAllByRole("cell").map(c => c.textContent))
      .toEqual(["12", "8", "3", "1", "8", "87.5%", "100.0%", "93.3%"]);
    expect(within(overall).getByText("Below 70%").closest("tr")).toHaveTextContent("0.0%");
    expect(within(overall).getByText("80–90%").closest("tr")).toHaveTextContent("—");
    for (const label of ["Concept to Code", "Code to Concept", "Patient source codes"]) {
      expect(screen.getByRole("region", { name: label })).toBeInTheDocument();
    }
  });

  it("shows what curators made of the auto-approvals in a block of its own (#2856)", async () => {
    get.mockResolvedValue({ data: { sections, auto_approved: autoApproved } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    const block = await screen.findByRole("region", { name: "Auto-approved by Suggest" });
    expect(within(block).getAllByRole("columnheader").map(h => h.textContent)).toEqual(
      ["Confidence", "Total", "Reviewed", "Confirmed", "Reversed", "Not yet reviewed", "Precision of reviewed"]);
    const [, overall, top, mid] = within(block).getAllByRole("row");
    expect(within(overall).getByRole("rowheader")).toHaveTextContent("Overall");
    expect(within(overall).getAllByRole("cell").map(c => c.textContent)).toEqual(["10", "4", "3", "1", "6", "75.0%"]);
    expect(within(top).getAllByRole("cell").map(c => c.textContent)).toEqual(["10", "4", "3", "1", "6", "75.0%"]);
    expect(within(mid).getAllByRole("cell").map(c => c.textContent)).toEqual(["0", "0", "0", "0", "0", "—"]);
    expect(within(block).getByText(/a sample, not all of them/)).toBeInTheDocument();
    // The per-surface tables are the same as before.
    expect(screen.getAllByRole("region")).toHaveLength(5);
  });

  it("groups the thousands of the auto-approved counts and says an untouched approval is not yet reviewed", async () => {
    const big: AutoApprovedBlock = { ...autoApproved, rows: [autoRow("overall", "Overall", { total: 12345, unreviewed: 12000, reviewed: 345, confirmed: 300, reversed: 45, precision_of_reviewed: 300 / 345 })] };
    get.mockResolvedValue({ data: { sections, auto_approved: big } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    const block = await screen.findByRole("region", { name: "Auto-approved by Suggest" });
    expect(within(block).getAllByRole("cell").map(c => c.textContent)).toEqual(["12,345", "345", "300", "45", "12,000", "87.0%"]);
    expect(within(block).getByText(/nobody has changed is Not yet reviewed/)).toBeInTheDocument();
  });

  it("shows no auto-approved block when the answer has none or predates it", async () => {
    get.mockResolvedValue({ data: { sections } });
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    await screen.findByRole("region", { name: "Overall" });
    expect(screen.queryByRole("region", { name: "Auto-approved by Suggest" })).not.toBeInTheDocument();
  });

  it("reports a failed load", async () => {
    get.mockRejectedValue(new Error("boom"));
    render(<MemoryRouter><MappingAccuracyAnalyticsPage /></MemoryRouter>);
    expect(await screen.findByText("Failed to load mapping accuracy analytics.")).toBeInTheDocument();
  });
});
