import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CodeMappingAccuracyPage from "./CodeMappingAccuracyPage";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: { get } }));

const model = (version: string, overrides: Record<string, unknown> = {}) => ({
  model_version: version, suggestions: 0, reviewed: 0, approved: 0, rejected: 0, overridden: 0,
  precision: null, recall: null, f1: null, ...overrides,
});
const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((cell) => cell.textContent);

/** The suggestion-model history at /code-mappings/accuracy. No page links to it since #2730. */
describe("CodeMappingAccuracyPage", () => {
  beforeEach(() => { get.mockReset(); });

  it("lists each model, newest first, under a row scored over all of them", async () => {
    get.mockResolvedValue({ data: {
      models: [
        model("v0.3", { suggestions: 1, reviewed: 1, approved: 1, precision: 1, recall: 1, f1: 1 }),
        model("v0.2", { suggestions: 3, reviewed: 3, approved: 1, rejected: 1, overridden: 1, precision: 1 / 3, recall: 0.5, f1: 0.4 }),
      ],
      overall: { suggestions: 4, reviewed: 4, approved: 2, rejected: 1, overridden: 1, precision: 0.5, recall: 2 / 3, f1: 4 / 7 },
    } });
    render(<MemoryRouter><CodeMappingAccuracyPage /></MemoryRouter>);

    const all = (await screen.findByText("All models (2)")).closest("tr")!;
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/accuracy/dashboard/");
    expect(screen.getAllByRole("columnheader").map((header) => header.textContent)).toEqual(
      ["Model", "Suggestions", "Approved", "Rejected", "Other destination", "Precision", "Recall", "F1"]);
    expect(cells(all).slice(1)).toEqual(["4", "2", "1", "1", "50.0%", "66.7%", "57.1%"]);
    const [, newest, older] = screen.getAllByRole("row").slice(1);
    expect(cells(newest)).toEqual(["Suggest v0.3", "1", "1", "0", "0", "100.0%", "100.0%", "100.0%"]);
    expect(cells(older)).toEqual(["Suggest v0.2", "3", "1", "1", "1", "33.3%", "50.0%", "40.0%"]);
    expect(screen.getByRole("link", { name: "← Code Mapping" })).toHaveAttribute("href", "/code-mappings");
  });

  it("shows dashes for a model nobody has reviewed, and no all-models row for a single model", async () => {
    get.mockResolvedValue({ data: { models: [model("v0.4", { suggestions: 7 })], overall: model("", { suggestions: 7 }) } });
    render(<MemoryRouter><CodeMappingAccuracyPage /></MemoryRouter>);

    const row = (await screen.findByText(/Suggest v0\.4/)).closest("tr")!;
    expect(cells(row)).toEqual(["Suggest v0.4Awaiting model reviews", "7", "0", "0", "0", "—", "—", "—"]);
    expect(screen.queryByText(/All models/)).not.toBeInTheDocument();
  });

  it("says so when the history cannot be loaded", async () => {
    get.mockRejectedValue(new Error("500"));
    render(<MemoryRouter><CodeMappingAccuracyPage /></MemoryRouter>);

    expect(await screen.findByText("Failed to load suggestion accuracy.")).toBeInTheDocument();
  });
});
