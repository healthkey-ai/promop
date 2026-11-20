import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DiseaseCategoryPage from "./DiseaseCategoryPage";

const mockUseAuth = vi.fn();
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));

const mockHub = vi.hoisted(() => ({
  fetchDiseaseCategories: vi.fn(),
  createDiseaseCategory: vi.fn(),
  deleteDiseaseCategory: vi.fn(),
  fetchDiseaseCategoryMappings: vi.fn(),
  createDiseaseCategoryMapping: vi.fn(),
  updateDiseaseCategoryMapping: vi.fn(),
  deleteDiseaseCategoryMapping: vi.fn(),
}));
vi.mock("@/api/mappingHub", () => mockHub);

/** A doctor without the analyst grant: reads every mapping page, writes none (#2416). */
const READER = {
  id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
  org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
};

function renderPage() {
  return render(<MemoryRouter><DiseaseCategoryPage /></MemoryRouter>);
}

describe("DiseaseCategoryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockHub.fetchDiseaseCategories.mockResolvedValue([
      { value: "MM", label: "Multiple Myeloma" },
      { value: "FL", label: "Follicular Lymphoma" },
    ]);
    mockHub.fetchDiseaseCategoryMappings.mockResolvedValue([
      { id: 1, disease_name: "Smoldering myeloma", category: "MM" },
    ]);
  });

  it("lets an org admin add categories and mappings, recategorize and delete", async () => {
    mockUseAuth.mockReturnValue({ currentUser: { id: 1, is_staff: false, is_org_admin: true } });
    renderPage();
    expect(await screen.findByText("Smoldering myeloma")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new category/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete category MM" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Disease Name" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^add$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Smoldering myeloma" })).toBeInTheDocument();
    expect(screen.getAllByRole("combobox").length).toBe(2);
  });

  it("shows a reader the mappings with no write controls (#2416)", async () => {
    mockUseAuth.mockReturnValue({ currentUser: READER });
    renderPage();
    expect(await screen.findByText("Smoldering myeloma")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Read-only: editing reference data needs the org admin role.");
    expect(screen.getByText("Multiple Myeloma", { selector: "td > span" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new category/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete category MM" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Disease Name" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^add$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete Smoldering myeloma" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    // Search still works for a reader.
    expect(screen.getByRole("button", { name: "Search" })).toBeInTheDocument();
    for (const write of [
      mockHub.createDiseaseCategory, mockHub.deleteDiseaseCategory,
      mockHub.createDiseaseCategoryMapping, mockHub.updateDiseaseCategoryMapping, mockHub.deleteDiseaseCategoryMapping,
    ]) expect(write).not.toHaveBeenCalled();
  });
});
