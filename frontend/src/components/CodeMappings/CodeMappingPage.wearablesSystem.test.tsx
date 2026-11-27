/**
 * The Wearables tab's Source System controls on the Code Mapping page: the
 * dropdown beside "All", the first column, sorting by it (which groups the
 * section by system, collapsed), and the controls that tab does not have.
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
  { vocabulary_id: "EPIC", label: "Epic", is_standard: false, proposed: 3, approved: 0, athena: 0 },
  { vocabulary_id: "OpenWearables", label: "Wearables", is_standard: false, proposed: 3, approved: 0, athena: 0 },
  { vocabulary_id: "__overall__", label: "Overall", is_standard: false, proposed: 6, approved: 0, athena: 0 },
];
const SYSTEMS = [
  { id: "Apple", label: "HealthKit" },
  { id: "Garmin", label: "Garmin" },
];
const reference = { ...emptyReference, wearable_source_systems: SYSTEMS };
const row = (id: number, code: string, vocabulary: string): CodeMappingRow => ({
  mapping_id: id, source_vocabulary_id: vocabulary, source_code: code, source_code_description: "",
  destination_concept_id: 3000 + id, destination_concept_name: "Number of steps",
  destination_concept_code: "55423-8", destination_vocabulary_id: "LOINC", destination_concept_class_id: "",
  destination_omop_table: "measurement", status: "proposed", notes: "", origin: "", origin_system: "",
  suggest_strategy: "", umls_cui: "", created_by: "", occurrence_count: 0, destination_count: 1,
  has_mapping: true, category: "Activity", metric_key: "steps",
} as CodeMappingRow);
const page = { page: 1, page_size: 100, total: 3 };
const browseCalls = () => mockGet.mock.calls
  .filter(([url]) => url === "/v1/code-mappings/").map(([, config]) => config.params as Record<string, unknown>);

/** The page opens on `opensOn`; a click on a tab is answered for that tab. */
function renderOn(opensOn: string) {
  mockGet.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url === "/v1/code-mappings/") {
      const source = (config?.params?.source as string | undefined) ?? opensOn;
      const bySystem = typeof config?.params?.order_1 === "string" && config.params.order_1.endsWith("source_system");
      return Promise.resolve({ data: {
        results: bySystem ? [] : [row(1, "steps", "Apple"), row(2, "hr", "Garmin")], duplicates: [], tabs: TABS,
        selected_source: source,
        pages: { Unmapped: page, Proposed: page, Approved: page, Rejected: page },
        rejected_count: 0, provenances: [], selected_provenance: "", organizations: [],
        selected_organization: "", groups: {}, rollup: false,
        system_groups: bySystem ? { Proposed: [
          { source_vocabulary_id: "Apple", label: "HealthKit", members: 2, seen: 0, proposed: 2 },
          { source_vocabulary_id: "Garmin", label: "Garmin", members: 1, seen: 0, proposed: 1 },
        ] } : {},
      } });
    }
    if (url === "/v1/code-mappings/group/") {
      return Promise.resolve({ data: { results: [row(1, "steps", "Apple"), row(3, "floors", "Apple")] } });
    }
    return Promise.resolve({ data: url.includes("reference") ? reference : {} });
  });
  render(<MemoryRouter initialEntries={["/code-mappings?direction=forward"]}><CodeMappingPage /></MemoryRouter>);
}

const download = () => screen.queryByRole("group", { name: "Download all wearable mappings" });
const dropdown = () => screen.queryByRole("button", { name: /^Source code systems:/ });

beforeEach(() => {
  mockGet.mockReset();
  mockUseAuth.mockReturnValue(role("analyst"));
});

describe("the Wearables tab's source system dropdown", () => {
  it("replaces the downloads beside All, on the Wearables tab only", async () => {
    renderOn("EPIC");
    const wearables = await screen.findByRole("tab", { name: /Wearables/ });
    expect(dropdown()).not.toBeInTheDocument();
    expect(screen.getByText("Downloads include loaded rows only.")).toBeInTheDocument();

    fireEvent.click(wearables);
    await waitFor(() => expect(dropdown()).toBeInTheDocument());
    expect(dropdown()).toHaveAccessibleName("Source code systems: All source systems");
    expect(download()).not.toBeInTheDocument();
    expect(screen.queryByText("Downloads include loaded rows only.")).not.toBeInTheDocument();
    // The All line's own Download menu is gone; the sections keep theirs.
    expect(within(screen.getByTestId("all-mappings-count").parentElement!).queryByText("Download")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(dropdown()).not.toBeInTheDocument());
  });

  it("is offered to a reader who cannot write mappings", async () => {
    mockUseAuth.mockReturnValue(role("doctor"));
    renderOn("OpenWearables");
    await waitFor(() => expect(dropdown()).toBeInTheDocument());
  });

  it("limits the list to the systems ticked, and back to all", async () => {
    renderOn("OpenWearables");
    fireEvent.click(await waitFor(() => { const button = dropdown(); expect(button).toBeInTheDocument(); return button!; }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Garmin" }));
    await waitFor(() => expect(browseCalls().at(-1)).toHaveProperty("source_systems", "Garmin"));
    fireEvent.click(screen.getByRole("checkbox", { name: "All source systems" }));
    await waitFor(() => expect(browseCalls().at(-1)).not.toHaveProperty("source_systems"));
  });

  it("is not sent on another tab", async () => {
    renderOn("OpenWearables");
    fireEvent.click(await waitFor(() => { const button = dropdown(); expect(button).toBeInTheDocument(); return button!; }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Garmin" }));
    await waitFor(() => expect(browseCalls().at(-1)).toHaveProperty("source_systems", "Garmin"));
    mockGet.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: /Epic/ }));
    await waitFor(() => expect(browseCalls().length).toBeGreaterThan(0));
    browseCalls().forEach((params) => expect(params).not.toHaveProperty("source_systems"));
  });
});

describe("the Source System column", () => {
  const headerNames = (table: HTMLElement) => within(table).getAllByRole("columnheader")
    .map((header) => header.getAttribute("aria-label") ?? header.textContent?.replace(/[↕↑↓]/g, "").trim());

  it("leads the table and names each row's system as the dropdown does", async () => {
    renderOn("OpenWearables");
    const table = await screen.findByRole("table", { name: "Proposed mappings" });
    await waitFor(() => expect(headerNames(table)[0]).toBe("Source System"));
    const systems = await within(table).findAllByTestId("source-system");
    expect(systems.map((cell) => cell.textContent)).toEqual(["HealthKit", "Garmin"]);
  });

  it("sorts, and a section sorted by it is its systems, collapsed until opened", async () => {
    renderOn("OpenWearables");
    const table = await screen.findByRole("table", { name: "Proposed mappings" });
    fireEvent.click(await within(table).findByRole("button", { name: /^Source System/ }));
    await waitFor(() => expect(browseCalls().at(-1)?.order_1).toBe("source_system"));
    expect(within(table).getByRole("columnheader", { name: /Source System/ })).toHaveAttribute("aria-sort", "ascending");

    const healthKit = await within(table).findByRole("button", { name: "Expand HealthKit" });
    expect(healthKit).toHaveAttribute("aria-expanded", "false");
    expect(within(table).getByRole("button", { name: "Expand Garmin" })).toHaveAttribute("aria-expanded", "false");
    expect(healthKit).toHaveTextContent("2 codes");
    expect(within(table).queryByText("steps", { selector: "[data-testid=source-code]" })).not.toBeInTheDocument();

    // Opening one asks for that system's rows only.
    fireEvent.click(healthKit);
    expect(await within(table).findByText("floors", { selector: "[data-testid=source-code]" })).toBeInTheDocument();
    const asked = mockGet.mock.calls.find(([url]) => url === "/v1/code-mappings/group/")![1].params;
    expect(asked).toMatchObject({ group_system: "Apple", section: "Proposed" });
    expect(asked).not.toHaveProperty("label");
    fireEvent.click(within(table).getByRole("button", { name: "Collapse HealthKit" }));
    expect(within(table).queryByText("floors", { selector: "[data-testid=source-code]" })).not.toBeInTheDocument();

    fireEvent.click(within(table).getByRole("button", { name: /^Source System/ }));
    await waitFor(() => expect(browseCalls().at(-1)?.order_1).toBe("-source_system"));
  });

  it("is not on another tab", async () => {
    renderOn("EPIC");
    const table = await screen.findByRole("table", { name: "Proposed mappings" });
    expect(headerNames(table)).not.toContain("Source System");
  });
});

describe("the auto-approved (Suggest) filter", () => {
  const checkbox = () => screen.queryAllByRole("checkbox", { name: /only codes Suggest approved by itself/ });

  it("is on the other tabs and not on Wearables", async () => {
    renderOn("EPIC");
    await screen.findByRole("table", { name: "Proposed mappings" });
    expect(checkbox().length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(dropdown()).toBeInTheDocument());
    expect(checkbox()).toHaveLength(0);
  });

  it("does not narrow Wearables when ticked on another tab", async () => {
    renderOn("EPIC");
    await screen.findByRole("table", { name: "Proposed mappings" });
    fireEvent.click(checkbox()[0]);
    await waitFor(() => expect(browseCalls().at(-1)).toHaveProperty("auto_approved", "1"));
    fireEvent.click(screen.getByRole("tab", { name: /Wearables/ }));
    await waitFor(() => expect(dropdown()).toBeInTheDocument());
    await waitFor(() => expect(browseCalls().at(-1)).toHaveProperty("source", "OpenWearables"));
    expect(browseCalls().at(-1)).not.toHaveProperty("auto_approved");
  });
});
