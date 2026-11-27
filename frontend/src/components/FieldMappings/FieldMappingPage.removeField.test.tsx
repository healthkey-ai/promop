/**
 * Removing a created field from the Field Mappings page: a Remove field action on a
 * created field's row for staff and org admins only, the global Remove-mapping X gone
 * from a created field (its mapping is removed with the field), the confirm dialog that
 * states what the server will do, and the Removed fields list with Restore.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();
const mockUseAuth = vi.fn();

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));
vi.mock("@/hooks/useWritableFields", () => ({ invalidateAllWritableFields: vi.fn() }));
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    put: vi.fn().mockResolvedValue({ data: {} }),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));
vi.mock("./FieldChoiceEditor", () => ({ FieldChoiceEditor: () => null }));
vi.mock("./FormulaEditDialog", () => ({ FormulaEditDialog: () => null }));

const mapping = (id: number) => ({
  id, concept_id: 1, concept_name: "Concept", vocabulary_id: "SNOMED", concept_code: "1", unit: "",
  omop_table: "Observation", status: "approved", reviewer: null, reviewed_at: null, notes: "",
});
const field = (field_name: string, extra: Record<string, unknown> = {}) => ({
  field_name, field_type: "text", category: "needs-concept-set", tab: "general",
  scope: "base", diseases: [], provenance: null, mapping: mapping(field_name === "built_in" ? 1 : 2),
  suggestion: null, mappable: true, locked_table: null, unit_options: [], choices: [],
  formula: null, explanation: null, derivation_error: null, ...extra,
});

const DESCRIPTORS = [field("built_in"), field("my_created_field", { created_field: true })];
const USAGE = {
  field_name: "my_created_field", retired: false, records_with_value: 4, has_facts: false,
  answers: 0, can_delete: false,
};

const renderPage = () => render(<MemoryRouter><FieldMappingPage /></MemoryRouter>);

describe("FieldMappingPage removing a created field", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({ currentUser: { is_staff: true, is_org_admin: false } });
    mockDelete.mockResolvedValue({ data: { state: "retired" } });
    mockGet.mockImplementation((url: string, config?: { params?: { state?: string } }) => {
      if (url === "/v1/field-mappings/") return Promise.resolve({ data: DESCRIPTORS });
      if (url === "/v1/custom-patient-fields/my_created_field/") return Promise.resolve({ data: USAGE });
      if (url === "/v1/custom-patient-fields/" && config?.params?.state === "retired") {
        return Promise.resolve({ data: [{
          field_name: "old_field", display_name: "Old field", tab: "general", field_type: "text",
          retired_at: "2026-11-27T10:00:00Z",
        }] });
      }
      return Promise.resolve({ data: [] });
    });
  });

  it("offers Remove field on a created field only, and no Remove-mapping X beside its concept", async () => {
    renderPage();
    await screen.findByText("my_created_field");

    expect(screen.getByRole("button", { name: "Remove field my_created_field" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove field built_in" })).not.toBeInTheDocument();
    // One Remove-mapping X, the built-in field's; the created field's mapping goes with the field.
    expect(screen.getAllByTitle("Remove mapping")).toHaveLength(1);
  });

  it("confirms with the record count, then reports the server's answer and reloads", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Remove field my_created_field" }));

    expect(await screen.findByTestId("usage-summary")).toHaveTextContent("4 records hold a value");
    const loads = mockGet.mock.calls.filter(([url]) => url === "/v1/field-mappings/").length;
    fireEvent.click(screen.getByRole("button", { name: "Remove field" }));

    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/v1/custom-patient-fields/my_created_field/", undefined));
    expect(await screen.findByText(/Removed my_created_field\. Its stored values are kept/)).toBeInTheDocument();
    await waitFor(() =>
      expect(mockGet.mock.calls.filter(([url]) => url === "/v1/field-mappings/").length).toBeGreaterThan(loads));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("is hidden for roles that cannot approve (analyst, doctor)", async () => {
    mockUseAuth.mockReturnValue({ currentUser: {
      id: 3, sub: "an", email: "an@example.com", name: "An", is_staff: false,
      org_accesses: [{ role: "analyst", org_name: "Org", org_slug: "org", expires_at: null }],
    } });
    renderPage();
    await screen.findByText("my_created_field");

    expect(screen.queryByRole("button", { name: /Remove field/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Removed fields" })).not.toBeInTheDocument();
    // The mapping X is not offered on a created field to anyone.
    expect(screen.getAllByTitle("Remove mapping")).toHaveLength(1);
  });

  it("lists removed fields and restores one", async () => {
    mockPost.mockResolvedValue({ data: {} });
    renderPage();
    await screen.findByText("my_created_field");

    fireEvent.click(screen.getByRole("button", { name: "Removed fields" }));
    expect(await screen.findByText("Old field")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));

    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/v1/custom-patient-fields/old_field/restore/"));
    expect(await screen.findByText("Restored old_field.")).toBeInTheDocument();
  });
});
