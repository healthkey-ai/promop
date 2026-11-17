import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import MappingHubPage from "./MappingHubPage";

vi.mock("@/api/mappingHub", () => ({
  fetchMappingStats: vi.fn().mockResolvedValue({
    field_mappings: { total: 1, approved: 1, proposed: 0, unmapped: 0 },
    code_mappings: { total: 1, approved: 1, proposed: 0 },
    therapy: { regimens: 1, components: 1, classes: 1, disease_links: 1 },
    relevance_signals: { proposed: 3, accepted: 2, rejected: 1 },
    disease_category: { total: 7 },
    chronic_overrides: { total: 4 },
  }),
}));

function renderHub() {
  return render(
    <MemoryRouter initialEntries={["/mappings"]}>
      <Routes>
        <Route path="/mappings" element={<MappingHubPage />} />
        <Route path="*" element={<div>went to elsewhere</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MappingHubPage panel order", () => {
  it("puts Code Mapping first, and leaves the other panels in their order", async () => {
    renderHub();
    await screen.findByText("4 overrides");
    const panels = screen.getAllByRole("heading", { level: 2 }).map(heading => heading.textContent);
    expect(panels).toEqual(["Code Mapping", "Field Mapping", "Therapy Mapping", "Disease Mapping", "Survey Mapping"]);
  });

  it("still opens Code Mapping from its panel", async () => {
    renderHub();
    const panel = screen.getByRole("heading", { name: "Code Mapping" }).closest("button")!;
    expect(panel).toHaveTextContent("Normalize source coding systems into the OMOP vocabulary.");
    fireEvent.click(panel);
    expect(await screen.findByText("went to elsewhere")).toBeInTheDocument();
  });
});

describe("MappingHubPage Disease Mapping card", () => {
  it("is one card with the three disease functions and their counts", async () => {
    renderHub();
    const card = screen.getByRole("region", { name: "Disease Mapping" });
    expect(card).toBeInTheDocument();
    expect(await screen.findByText("3 to review")).toBeInTheDocument();
    expect(screen.getByText("7 mappings")).toBeInTheDocument();
    expect(screen.getByText("4 overrides")).toBeInTheDocument();
    for (const name of ["Disease Relevance", "Disease Category", "Chronic Condition Overrides"]) {
      expect(screen.getAllByRole("heading", { name })).toHaveLength(1);
    }
    expect(screen.queryByRole("heading", { name: "Disease Mapping", level: 2 })).toBeInTheDocument();
  });

  it("opens each function's page", async () => {
    renderHub();
    await screen.findByText("4 overrides");
    fireEvent.click(screen.getByText("Chronic Condition Overrides"));
    expect(await screen.findByText("went to elsewhere")).toBeInTheDocument();
  });
});
