import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "@/api/axios";
import SuggestRunLogPage from "./SuggestRunLogPage";

vi.mock("@/api/axios", () => ({ default: { get: vi.fn() } }));
const get = vi.mocked(api.get);
const source = { mapping_id: 7, source_code: "GLU", source_vocabulary_id: "Lab", occurrences: 400 };
const log = {
  run_id: "run-7", state: "success", done: 1, total: 1, error: "",
  selection: { limit: 50, order: "Codes without destinations first, ordered by Seen count highest first.", strategies: ["umls"], model_version: "v1" },
  activity: [
    { stage: "selected", at: "2026-09-09T10:00:00Z", sources: [source] },
    { stage: "result", at: "2026-09-09T10:00:03Z", ...source, updated: true,
      suggested: { concept_id: 123, concept_code: "2345-7", vocabulary_id: "LOINC", concept_name: "Glucose" },
      note: "Exact CUI bridge", strategy_used: "umls" },
  ],
};
const show = () => render(<MemoryRouter initialEntries={["/code-mappings/suggest-runs/run-7"]}>
  <Routes><Route path="/code-mappings/suggest-runs/:runId" element={<SuggestRunLogPage />} /></Routes>
</MemoryRouter>);

describe("Suggestion run log", () => {
  beforeEach(() => vi.clearAllMocks());

  it("opens a completed run directly with selection policy and persisted destination", async () => {
    get.mockResolvedValue({ data: log });
    show();
    expect(await screen.findByText("How the first 50 codes are chosen")).toBeInTheDocument();
    expect(screen.getByText(/Seen count highest first/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Done · 1/1");
    expect(screen.getByText(/Chosen destination: LOINC:2345-7 — Glucose/)).toBeInTheDocument();
    expect(screen.getByText("Exact CUI bridge")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/v1/code-mappings/suggest-runs/run-7/", { params: { include_activity: "1" } });
  });

  it("updates the current source to its final destination while the run finishes", async () => {
    get.mockResolvedValueOnce({ data: { ...log, state: "running", done: 0,
      activity: [{ ...source, stage: "retrieving", at: "2026-09-09T10:00:00Z" }] } })
      .mockResolvedValue({ data: log });
    show();
    expect(await screen.findByText("Searching for candidates")).toBeInTheDocument();
    expect(screen.getByText(/Lab:GLU/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Done"), { timeout: 2500 });
    expect(screen.getByText(/Chosen destination/)).toBeInTheDocument();
  });

  it("can retry a failed load and displays older runs without invented log contents", async () => {
    get.mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ data: { ...log, selection: {}, activity: [] } });
    show();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No detailed activity was recorded for this run.")).toBeInTheDocument();
    expect(screen.getByText("Selection order was not recorded for this older run.")).toBeInTheDocument();
  });
});
