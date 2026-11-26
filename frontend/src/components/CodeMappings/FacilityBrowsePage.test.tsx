import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FacilityBrowsePage from "./FacilityBrowsePage";

const mockGet = vi.fn();
vi.mock("@/api/axios", () => ({ default: { get: (...a: unknown[]) => mockGet(...a) } }));

const codes = {
  facility: "Hopkins", total: 1, page: 1, page_size: 100,
  systems: [{ source_vocabulary_id: "urn:oid:q", code_count: 1, shared_with_facilities: 1 }],
  results: [{ id: 7, source_vocabulary_id: "urn:oid:q", source_code: "MSQ1", description: "M-spike quantitation 1",
    omop_table: "measurement", status: "proposed", destination_concept_id: 5, destination_concept_name: "M spike",
    destination_vocabulary_id: "LOINC", destination_concept_code: "33358-3", destination_unit: "g/dL",
    seen: 0, axis_variant_count: 2 }],
};

describe("FacilityBrowsePage", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockGet.mockImplementation((_u: string, { params }: { params: Record<string, string> }) =>
      Promise.resolve({ data: params.facility ? codes
        : { facilities: [{ facility: "Hopkins", system_count: 1, code_count: 1 }] } }));
  });

  it("lists facilities then shows a facility's codes with an Edit mapping link", async () => {
    render(<MemoryRouter initialEntries={["/code-mappings/facilities"]}><FacilityBrowsePage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("link", { name: "Hopkins" }));
    expect(await screen.findByText("MSQ1")).toBeInTheDocument();
    expect(screen.getByText("shared")).toBeInTheDocument();
    expect(screen.getByText("2 axis values")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit mapping" })).toHaveAttribute(
      "href", "/code-mappings?direction=forward&mapping=7");
  });

  it("sends the search text", async () => {
    render(<MemoryRouter initialEntries={["/code-mappings/facilities?facility=Hopkins"]}><FacilityBrowsePage /></MemoryRouter>);
    await screen.findByText("MSQ1");
    fireEvent.change(screen.getByPlaceholderText(/M spike/), { target: { value: "band" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(mockGet).toHaveBeenLastCalledWith(expect.anything(),
      { params: expect.objectContaining({ facility: "Hopkins", q: "band" }) }));
  });
});
