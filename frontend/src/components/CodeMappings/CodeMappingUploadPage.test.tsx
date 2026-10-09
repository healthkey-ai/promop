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
} : url.includes("accuracy") ? { overall: {}, by_source_vocabulary: {} } : [] }));
vi.mock("@/api/axios", () => ({
  default: {
    get: (url: string) => mockGet(url),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}));

describe("CodeMappingPage upload action", () => {
  it("opens the upload dialog with signed-in email provenance", async () => {
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThanOrEqual(5));
    fireEvent.click(await screen.findByRole("button", { name: "Upload" }));
    expect(await screen.findByRole("dialog", { name: "Upload source codes" })).toBeInTheDocument();
    expect(screen.getByLabelText("Provenance")).toHaveValue("signed-in@example.com");
  });
});
