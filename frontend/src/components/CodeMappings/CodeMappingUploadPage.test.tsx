import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import CodeMappingPage from "./CodeMappingPage";

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ currentUser: { email: "signed-in@example.com", is_org_admin: true } }),
}));
const mockGet = vi.fn((url: string) => Promise.resolve({ data: url.includes("reference") ? {
  domains: [], source_code_systems_by_domain: {}, destination_vocabularies: [],
  omop_tables: {}, source_vocabulary_tabs: [],
} : [] }));
vi.mock("@/api/axios", () => ({
  default: {
    get: (url: string) => mockGet(url),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));

describe("CodeMappingPage upload action", () => {
  it("opens the upload dialog with signed-in email provenance", async () => {
    render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
    // Upload is offered once the page's reference data has arrived.
    await waitFor(() => expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("dialog", { name: "Upload source codes" })).toBeInTheDocument();
    expect(screen.getByLabelText("Provenance")).toHaveValue("signed-in@example.com");
  });
});
