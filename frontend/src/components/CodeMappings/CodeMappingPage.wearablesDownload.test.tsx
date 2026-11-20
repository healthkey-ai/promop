/**
 * Where the whole-tab download shows on the Code Mapping page: the Wearables
 * tab only, for anyone who can read the page.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CodeMappingPage from "./CodeMappingPage";
import { emptyReference } from "./codeMappingTypes";

const mockGet = vi.fn();
const role = (name: string) => ({
  currentUser: { id: 7, org_accesses: [{ org_slug: "acme", org_name: "Acme", role: name, expires_at: null }] },
});
const mockUseAuth = vi.hoisted(() => vi.fn((): { currentUser: unknown } => ({ currentUser: null })));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));
vi.mock("@/api/axios", () => ({
  default: { get: (...args: unknown[]) => mockGet(...args), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const TABS = [
  { vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 3, approved: 0, athena: 0 },
  { vocabulary_id: "OpenWearables", label: "Wearables", is_standard: false, proposed: 3, approved: 0, athena: 0 },
  { vocabulary_id: "__overall__", label: "Overall", is_standard: false, proposed: 6, approved: 0, athena: 0 },
];
const page = { page: 1, page_size: 100, total: 0 };

/** The page opens on `opensOn`; a click on a tab is answered for that tab. */
function renderOn(opensOn: string) {
  mockGet.mockImplementation((url: string, config?: { params?: { source?: string } }) => {
    if (url === "/v1/code-mappings/") {
      return Promise.resolve({ data: {
        results: [], duplicates: [], tabs: TABS, selected_source: config?.params?.source ?? opensOn,
        pages: { Unmapped: page, Proposed: page, Approved: page, Rejected: page },
        rejected_count: 0, provenances: [], selected_provenance: "", organizations: [],
        selected_organization: "", groups: {}, rollup: false,
      } });
    }
    return Promise.resolve({ data: url.includes("reference") ? emptyReference : {} });
  });
  render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
}

const download = () => screen.queryByRole("group", { name: "Download all wearable mappings" });

beforeEach(() => {
  mockGet.mockReset();
  mockUseAuth.mockReturnValue(role("analyst"));
});

describe("the Wearables tab download", () => {
  it("shows when the page opens on the Wearables tab", async () => {
    renderOn("OpenWearables");
    await waitFor(() => expect(download()).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Download all wearable mappings as CSV" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Download all wearable mappings as a Word document" })).toBeEnabled();
  });

  it("is absent on another tab, appears on Wearables and goes again on leaving", async () => {
    renderOn("EPIC");
    const wearables = await screen.findByRole("tab", { name: /Wearables/ });
    expect(screen.getByRole("tab", { name: /Epic/ })).toHaveAttribute("aria-selected", "true");
    expect(download()).not.toBeInTheDocument();

    fireEvent.click(wearables);
    await waitFor(() => expect(download()).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(download()).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: /Overall/ }));
    await waitFor(() => expect(screen.getByRole("tab", { name: /Overall/ })).toHaveAttribute("aria-selected", "true"));
    expect(download()).not.toBeInTheDocument();
  });

  it("is offered to a reader who cannot write mappings", async () => {
    mockUseAuth.mockReturnValue(role("doctor"));
    renderOn("OpenWearables");
    await waitFor(() => expect(download()).toBeInTheDocument());
    // Read-only: no way to add a mapping, but the file is theirs to take.
    expect(screen.queryByRole("button", { name: /New Mapping/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download all wearable mappings as a Word document" })).toBeEnabled();
  });
});
