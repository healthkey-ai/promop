/**
 * Public voting on the Code Mapping page (#2678): where Share shows, and the
 * tally a voted row carries.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CodeMappingPage from "./CodeMappingPage";
import { emptyReference, type CodeMappingRow } from "./codeMappingTypes";

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
  { vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 2, approved: 0, athena: 0 },
  { vocabulary_id: "OpenWearables", label: "Wearables", is_standard: false, proposed: 2, approved: 0, athena: 0 },
  { vocabulary_id: "__overall__", label: "Overall", is_standard: false, proposed: 4, approved: 0, athena: 0 },
];
const mapping = (id: number, code: string, extra: Partial<CodeMappingRow> = {}): CodeMappingRow => ({
  mapping_id: id, source_vocabulary_id: "Apple", source_code: code, source_code_description: "",
  destination_concept_id: 3000 + id, destination_concept_name: "Number of steps",
  destination_concept_code: "55423-8", destination_vocabulary_id: "LOINC", destination_concept_class_id: "",
  destination_omop_table: "measurement", status: "proposed", notes: "", origin: "", origin_system: "",
  suggest_strategy: "", umls_cui: "", created_by: "", occurrence_count: 0, destination_count: 1,
  has_mapping: true, category: "Activity", metric_key: "steps", ...extra,
} as CodeMappingRow);
const ROWS = [
  mapping(1, "VOTED", { votes: { yes: 3, no: 1, comments: 2 } }),
  mapping(2, "QUIET", { votes: { yes: 0, no: 0, comments: 0 } }),
];

function renderOn(opensOn: string) {
  mockGet.mockImplementation((url: string, config?: { params?: { source?: string } }) => {
    if (url === "/v1/code-mappings/") {
      const source = config?.params?.source ?? opensOn;
      const page = { page: 1, page_size: 100, total: 2 };
      return Promise.resolve({ data: {
        results: ROWS.map((row) => (source === "OpenWearables" ? row : { ...row, source_vocabulary_id: "EPIC" })),
        duplicates: [], tabs: TABS, selected_source: source,
        pages: { Unmapped: page, Proposed: page, Approved: { ...page, total: 0 }, Rejected: { ...page, total: 0 } },
        rejected_count: 0, provenances: [], selected_provenance: "", organizations: [],
        selected_organization: "", groups: {}, rollup: false,
      } });
    }
    return Promise.resolve({ data: url.includes("reference") ? emptyReference : {} });
  });
  render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
}

const share = () => screen.queryByRole("button", { name: "Share" });
const download = () => screen.queryByRole("group", { name: "Download all wearable mappings" });

/** An org admin as the server describes one: the grant, and the flag it sets. */
const orgAdmin = () => ({ currentUser: { ...role("org_admin").currentUser, is_org_admin: true } });

beforeEach(() => {
  mockGet.mockReset();
  mockUseAuth.mockReturnValue(orgAdmin());
});

describe("Share on the Wearables tab", () => {
  it("is offered beside the download to a mapping administrator", async () => {
    renderOn("OpenWearables");
    await waitFor(() => expect(share()).toBeInTheDocument());
    expect(download()).toBeInTheDocument();
    // On the tab's "All" line, after the download.
    expect(share()!.parentElement).toContainElement(download());
    expect(download()!.compareDocumentPosition(share()!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("is offered to staff", async () => {
    mockUseAuth.mockReturnValue({ currentUser: { id: 1, is_staff: true } });
    renderOn("OpenWearables");
    await waitFor(() => expect(share()).toBeInTheDocument());
  });

  // A share sends mail and gathers voters' addresses: writing mappings is
  // not enough, and reading them certainly is not.
  it.each(["analyst", "doctor"])("is hidden from a %s, who still gets the download", async (name) => {
    mockUseAuth.mockReturnValue(role(name));
    renderOn("OpenWearables");
    await waitFor(() => expect(download()).toBeInTheDocument());
    expect(share()).not.toBeInTheDocument();
  });

  it("belongs to the Wearables tab only", async () => {
    renderOn("EPIC");
    const wearables = await screen.findByRole("tab", { name: /Wearables/ });
    expect(share()).not.toBeInTheDocument();
    fireEvent.click(wearables);
    await waitFor(() => expect(share()).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(share()).not.toBeInTheDocument());
  });
});

describe("the votes indicator on a mapping row", () => {
  it("shows the tally on a voted row and nothing on the others", async () => {
    renderOn("OpenWearables");
    const voted = (await screen.findAllByText("VOTED"))[0].closest("tr")!;
    expect(within(voted).getByTestId("vote-tally")).toHaveTextContent("3 yes · 1 no · 2 comments");
    const quiet = screen.getAllByText("QUIET")[0].closest("tr")!;
    expect(within(quiet).queryByTestId("vote-tally")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("vote-tally")).toHaveLength(1);
  });

  it.each(["analyst", "doctor"])("is read by a %s too", async (name) => {
    mockUseAuth.mockReturnValue(role(name));
    renderOn("OpenWearables");
    expect(await screen.findByTestId("vote-tally")).toHaveTextContent("3 yes · 1 no · 2 comments");
  });

  it("adds no column: a voted row has the cells of any other row", async () => {
    renderOn("OpenWearables");
    const voted = (await screen.findAllByText("VOTED"))[0].closest("tr")!;
    const quiet = screen.getAllByText("QUIET")[0].closest("tr")!;
    expect(voted.children).toHaveLength(quiet.children.length);
    expect(voted.closest("table")!.querySelectorAll("thead th")).toHaveLength(10);
  });

  it("leaves the other tabs at their eight columns", async () => {
    renderOn("EPIC");
    const row = (await screen.findAllByText("QUIET"))[0].closest("tr")!;
    expect(row.closest("table")!.querySelectorAll("thead th")).toHaveLength(8);
  });
});
