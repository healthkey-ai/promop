import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SourceSystemManagementPage from "./SourceSystemManagementPage";

const mockGet = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...args: unknown[]) => mockGet(...args) } }));

const system = "urn:oid:1.2.840.114350.1.13.90.2.7.2.707679";

function response(overrides: Record<string, unknown> = {}) {
  return {
    results: [{
      source_vocabulary_id: system, facility_name: "University Hospital",
      facility_id: "H1", level: "site", confidence: "high",
      attribution_method: "connection", provenance: "healthtree-hospital-seed-20261005-v2",
    }],
    total: 1, page: 1, page_size: 50,
    seed_import: { artifact_identity: "healthtree-hospital-seed-20261005-v2", outcome: "applied", expected_rows: 691800, completed_at: null, failure_reason: "" },
    system_diagnostics: { source_code_rows: 10, codes_with_facility_evidence: 8 },
    ...overrides,
  };
}

describe("SourceSystemManagementPage", () => {
  beforeEach(() => { mockGet.mockReset(); });

  it("opens a dialog drill-through with the exact system and observed name", async () => {
    mockGet.mockResolvedValue({ data: response() });
    render(<MemoryRouter initialEntries={[`/code-mappings/source-systems?system=${encodeURIComponent(system)}&facility=University+Hospital`]}>
      <SourceSystemManagementPage />
    </MemoryRouter>);

    expect(await screen.findByText("University Hospital", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Source Code Systems/Hospitals Management" })).toBeInTheDocument();
    expect(screen.getByText(system, { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("8 with per-code facility evidence")).toBeInTheDocument();
    expect(screen.getByText("healthtree-hospital-seed-20261005-v2", { selector: "span" })).toBeInTheDocument();
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/code-mappings/source-systems/",
      { params: { system, facility: "University Hospital", page: 1 } },
    ));
    fireEvent.change(screen.getByRole("textbox", { name: "Hospital / facility name contains" }), {
      target: { value: "UPMC" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      "/v1/code-mappings/source-systems/",
      { params: { system, facility: "UPMC", page: 1 } },
    ));
  });

  it("explains missing registry rows when per-code evidence exists", async () => {
    mockGet.mockResolvedValue({ data: response({
      results: [], total: 0,
      seed_import: { artifact_identity: "healthtree-hospital-seed-20261005-v2", outcome: "failed", expected_rows: 691800, completed_at: null, failure_reason: "Download failed" },
    }) });
    render(<MemoryRouter initialEntries={[`/code-mappings/source-systems?system=${encodeURIComponent(system)}`]}>
      <SourceSystemManagementPage />
    </MemoryRouter>);
    expect(await screen.findByText(/per-code facility evidence but no registry rows/)).toBeInTheDocument();
    expect(screen.getByText("Download failed")).toBeInTheDocument();
  });
});
