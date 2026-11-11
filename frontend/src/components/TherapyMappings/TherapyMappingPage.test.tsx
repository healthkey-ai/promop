import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TherapyMappingPage from "./TherapyMappingPage";

const mockUseAuth = vi.fn();
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));

const mockHub = vi.hoisted(() => ({
  fetchRegimens: vi.fn(),
  fetchRegimenDetail: vi.fn(),
  createRegimen: vi.fn(),
  deleteRegimen: vi.fn(),
  addRegimenComponent: vi.fn(),
  removeRegimenComponent: vi.fn(),
  fetchComponents: vi.fn(),
  createComponent: vi.fn(),
  updateComponent: vi.fn(),
  addComponentClass: vi.fn(),
  removeComponentClass: vi.fn(),
  fetchClasses: vi.fn(),
  createClass: vi.fn(),
  fetchDiseaseTherapyRegimens: vi.fn(),
  addDiseaseTherapyRegimen: vi.fn(),
  removeDiseaseTherapyRegimen: vi.fn(),
}));
vi.mock("@/api/mappingHub", () => mockHub);

/** A doctor without the analyst grant: reads every mapping page, writes none (#2416). */
const READER = {
  id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
  org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
};

const REGIMEN = { code: "VRd", title: "VRd", concept_id: 35803000, components: [
  { code: "bortezomib", title: "Bortezomib", concept_id: 1, classes: [] },
] };
const COMPONENT = { code: "bortezomib", title: "Bortezomib", concept_id: 1, classes: [{ code: "PI", title: "Proteasome inhibitor" }] };
const ASSOCIATION = { id: 7, disease_code: "MM", disease_title: "Multiple myeloma", round_code: "first_line_therapy", round_title: "First line", regimen_code: "VRd", regimen_title: "VRd" };

function renderPage() {
  return render(<MemoryRouter><TherapyMappingPage /></MemoryRouter>);
}

describe("TherapyMappingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockHub.fetchRegimens.mockResolvedValue([REGIMEN]);
    mockHub.fetchRegimenDetail.mockResolvedValue(REGIMEN);
    mockHub.fetchComponents.mockResolvedValue([COMPONENT, { code: "lenalidomide", title: "Lenalidomide", concept_id: 2, classes: [] }]);
    mockHub.fetchClasses.mockResolvedValue([{ code: "PI", title: "Proteasome inhibitor" }, { code: "IMiD", title: "Immunomodulator" }]);
    mockHub.fetchDiseaseTherapyRegimens.mockResolvedValue([ASSOCIATION]);
  });

  it("lets an org admin add, edit and remove reference rows on every tab", async () => {
    mockUseAuth.mockReturnValue({ currentUser: { id: 1, is_staff: false, is_org_admin: true } });
    renderPage();
    expect(await screen.findByText("VRd")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add regimen/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete VRd" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("VRd"));
    expect(await screen.findByRole("button", { name: "Remove Bortezomib" })).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveTextContent("+ Add component...");

    fireEvent.click(screen.getByRole("button", { name: /component → classes/i }));
    expect(await screen.findByText("Bortezomib")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add component/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add class/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove class Proteasome inhibitor" })).toBeInTheDocument();
    expect(screen.getAllByTitle("Click to edit concept ID").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /disease → regimen/i }));
    expect(await screen.findByText("Multiple myeloma")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add association/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove association" })).toBeInTheDocument();
  });

  it("shows a reader the reference data with no write controls (#2416)", async () => {
    mockUseAuth.mockReturnValue({ currentUser: READER });
    renderPage();
    expect(await screen.findByText("VRd")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Read-only: editing reference data needs the org admin role.");
    expect(screen.queryByRole("button", { name: /add regimen/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete VRd" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("VRd"));
    expect(await screen.findByText("Bortezomib")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove Bortezomib" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /component → classes/i }));
    expect(await screen.findByText("Proteasome inhibitor")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add component/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add class/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove class Proteasome inhibitor" })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Click to edit concept ID")).not.toBeInTheDocument();
    expect(screen.getByText("HemOnc:1")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /disease → regimen/i }));
    expect(await screen.findByText("Multiple myeloma")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add association/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove association" })).not.toBeInTheDocument();

    for (const write of [
      mockHub.createRegimen, mockHub.deleteRegimen, mockHub.addRegimenComponent, mockHub.removeRegimenComponent,
      mockHub.createComponent, mockHub.updateComponent, mockHub.addComponentClass, mockHub.removeComponentClass,
      mockHub.createClass, mockHub.addDiseaseTherapyRegimen, mockHub.removeDiseaseTherapyRegimen,
    ]) expect(write).not.toHaveBeenCalled();
  });
});
