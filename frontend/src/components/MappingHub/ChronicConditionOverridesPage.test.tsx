import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChronicConditionOverridesPage from "./ChronicConditionOverridesPage";

const mockUseAuth = vi.fn();
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));

const mockHub = vi.hoisted(() => ({
  fetchChronicConditionOverrides: vi.fn(),
  createChronicConditionOverride: vi.fn(),
  updateChronicConditionOverride: vi.fn(),
  deleteChronicConditionOverride: vi.fn(),
}));
vi.mock("@/api/mappingHub", () => mockHub);

const READER = {
  id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
  org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
};
const ADMIN = { currentUser: { id: 1, is_staff: false, is_org_admin: true } };

function renderPage() {
  return render(<MemoryRouter><ChronicConditionOverridesPage /></MemoryRouter>);
}

describe("ChronicConditionOverridesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockHub.fetchChronicConditionOverrides.mockResolvedValue([
      { id: 1, code: "M85.8", is_chronic: true, label: "Osteopenia", notes: "Myeloma bone health" },
    ]);
  });

  it("lets an org admin add, change and delete an override", async () => {
    mockUseAuth.mockReturnValue(ADMIN);
    mockHub.createChronicConditionOverride.mockResolvedValue({ id: 2, code: "E03.9", is_chronic: false, label: "", notes: "" });
    mockHub.updateChronicConditionOverride.mockResolvedValue({ id: 1, code: "M85.8", is_chronic: false, label: "Osteopenia", notes: "Myeloma bone health" });
    renderPage();
    expect(await screen.findByText("Osteopenia")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ICD-10-CM code"), { target: { value: "E039" } });
    fireEvent.change(screen.getByLabelText("Counts as"), { target: { value: "not" } });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    await waitFor(() => expect(mockHub.createChronicConditionOverride).toHaveBeenCalledWith(
      { code: "E039", is_chronic: false, label: "", notes: "" }));

    fireEvent.change(screen.getByLabelText("Counts as for M85.8"), { target: { value: "not" } });
    await waitFor(() => expect(mockHub.updateChronicConditionOverride).toHaveBeenCalledWith(1, { is_chronic: false }));

    fireEvent.click(screen.getByRole("button", { name: "Delete M85.8" }));
    await waitFor(() => expect(mockHub.deleteChronicConditionOverride).toHaveBeenCalledWith(1));
  });

  it("shows the server's reason when an add is refused", async () => {
    mockUseAuth.mockReturnValue(ADMIN);
    mockHub.createChronicConditionOverride.mockRejectedValue({ response: { data: { detail: "An override for E03.9 already exists" } } });
    renderPage();
    await screen.findByText("Osteopenia");
    fireEvent.change(screen.getByLabelText("ICD-10-CM code"), { target: { value: "E03.9" } });
    fireEvent.click(screen.getByRole("button", { name: /^add$/i }));
    expect(await screen.findByText("An override for E03.9 already exists")).toBeInTheDocument();
  });

  it("shows a reader the overrides with no write controls", async () => {
    mockUseAuth.mockReturnValue({ currentUser: READER });
    renderPage();
    expect(await screen.findByText("Osteopenia")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Read-only: editing reference data needs the org admin role.");
    expect(screen.getByText("Chronic", { selector: "td > span" })).toBeInTheDocument();
    expect(screen.queryByLabelText("ICD-10-CM code")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^add$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete M85.8" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeInTheDocument();
  });
});
