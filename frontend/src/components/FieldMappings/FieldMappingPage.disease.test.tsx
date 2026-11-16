/**
 * The per-disease view of the Field Mappings page (#2332): the disease
 * filter, the disease-specific / full record toggle, each row's scope, the
 * two CSV downloads, and the scope control the dialog is opened with.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockPut = vi.fn();
const mockUseAuth = vi.fn();

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    put: (...args: unknown[]) => mockPut(...args),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

const field = (field_name: string, tab: string, diseases: string[], extra: Record<string, unknown> = {}) => ({
  field_name,
  field_type: "text",
  category: "needs-concept-set",
  tab,
  scope: diseases.length ? "disease" : "base",
  diseases,
  provenance: null,
  mapping: null,
  suggestion: null,
  mappable: true,
  locked_table: null,
  unit_options: [],
  choices: [],
  formula: null,
  explanation: null,
  derivation_error: null,
  ...extra,
});

const DESCRIPTORS = [
  field("weight", "general", []),
  field("hemoglobin_g_dl", "blood", []),
  field("stage", "disease", ["FL", "MM"]),
  field("her2_status", "disease", ["BC"]),
  field("kappa_flc", "labs", ["MM"], {
    mapping: {
      id: 7, concept_id: 3000, concept_name: "Kappa light chains", vocabulary_id: "LOINC",
      concept_code: "36916-5", unit: "mg/L", omop_table: "Measurement", status: "proposed",
      reviewer: null, reviewed_at: null, notes: "",
    },
  }),
];

const CATEGORIES = [
  { value: "BC", label: "Breast Cancer" },
  { value: "CLL", label: "Chronic Lymphocytic Leukemia" },
  { value: "FL", label: "Follicular Lymphoma" },
  { value: "MM", label: "Multiple Myeloma" },
  { value: "Other", label: "Other" },
];

const STAFF = { currentUser: { is_staff: true, is_org_admin: false } };
// A doctor without the analyst grant reads every mapping and writes none (#2416).
const DOCTOR = { currentUser: {
  id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
  org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
} };

const renderPage = (search = "") =>
  render(
    <MemoryRouter initialEntries={[`/field-mappings${search}`]}>
      <FieldMappingPage />
    </MemoryRouter>
  );

const listRequests = () => mockGet.mock.calls.filter(([url]) => url === "/v1/field-mappings/");
const diseaseSelect = () => screen.getByRole("combobox", { name: "Disease" });
const chooseDisease = (value: string) => fireEvent.change(diseaseSelect(), { target: { value } });
/** A tab of the tab bar: its name is the tab's label and its count. */
const tabName = (label: string) => new RegExp(`^${label}\\s*\\(\\d+\\)$`);
const tab = (label: string) => screen.getByRole("button", { name: tabName(label) });
const queryTab = (label: string) => screen.queryByRole("button", { name: tabName(label) });
const fieldNames = () => screen.queryAllByRole("row")
  .map((row) => within(row).queryAllByRole("cell")[1]?.textContent)
  .filter(Boolean);

let saved: string[];

beforeEach(() => {
  vi.clearAllMocks();
  saved = [];
  mockUseAuth.mockReturnValue(STAFF);
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/field-mappings/") return Promise.resolve({ data: DESCRIPTORS });
    if (url === "/v1/disease-categories/") return Promise.resolve({ data: CATEGORIES });
    if (url === "/v1/field-mappings/export/") {
      return Promise.resolve({
        data: new Blob(["field_name\n"], { type: "text/csv" }),
        headers: { "content-disposition": 'attachment; filename="from-the-server.csv"' },
      });
    }
    if (url.startsWith("/v1/field-synonyms/batch/")) return Promise.resolve({ data: {} });
    return Promise.resolve({ data: [] });
  });
  mockPatch.mockResolvedValue({ data: {} });
  mockPut.mockResolvedValue({ data: {} });
  Object.assign(URL, { createObjectURL: vi.fn(() => "blob:csv"), revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("disease filter", () => {
  it("offers All, Base record and every Disease Category but Other, each with its count", async () => {
    renderPage();
    await screen.findByText("weight");
    const options = within(diseaseSelect()).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual([
      "All diseases (5)",
      "Base record (2)",
      "BC — Breast Cancer (1)",
      "CLL — Chronic Lymphocytic Leukemia (0)",
      "FL — Follicular Lymphoma (1)",
      "MM — Multiple Myeloma (2)",
    ]);
    expect(diseaseSelect()).toHaveValue("");
    // No disease chosen: nothing to toggle between.
    expect(screen.queryByRole("group", { name: "Fields shown" })).not.toBeInTheDocument();
  });

  it("lists exactly a disease's extension, with no further request", async () => {
    renderPage();
    await screen.findByText("weight");
    expect(listRequests()).toHaveLength(1);

    chooseDisease("MM");
    expect(screen.getByRole("status", { name: "Disease view" }))
      .toHaveTextContent("Showing MM's disease-specific fields (2).");
    // General has no MM field, so the first tab that has one is shown.
    expect(fieldNames()).toEqual(["stage"]);
    // Only the extension's tabs remain, counted for this view.
    expect(tab("Disease")).toHaveTextContent("(1)");
    expect(queryTab("General")).not.toBeInTheDocument();
    expect(queryTab("Blood")).not.toBeInTheDocument();
    fireEvent.click(tab("Labs"));
    expect(fieldNames()).toEqual(["kappa_flc"]);
    expect(screen.getByText("Total: 2")).toBeInTheDocument();

    expect(listRequests()).toHaveLength(1);
  });

  it("switches to the full record: the base record plus the extension, nothing of another disease", async () => {
    renderPage();
    await screen.findByText("weight");
    chooseDisease("MM");
    const toggle = screen.getByRole("group", { name: "Fields shown" });
    expect(within(toggle).getByRole("button", { name: "Disease-specific" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(within(toggle).getByRole("button", { name: "Full record" }));
    expect(within(toggle).getByRole("button", { name: "Full record" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status", { name: "Disease view" })).toHaveTextContent("(4)");
    expect(fieldNames()).toEqual(["weight"]);
    fireEvent.click(tab("Disease"));
    // her2_status is BC's alone.
    expect(fieldNames()).toEqual(["stage"]);
    expect(tab("Blood")).toHaveTextContent("(1)");
    expect(listRequests()).toHaveLength(1);
  });

  it("lists the base record alone", async () => {
    renderPage();
    await screen.findByText("weight");
    chooseDisease("__base__");
    expect(screen.getByRole("status", { name: "Disease view" }))
      .toHaveTextContent("Showing the base record: the 2 fields every patient has.");
    expect(queryTab("Disease")).not.toBeInTheDocument();
    expect(fieldNames()).toEqual(["weight"]);
    expect(screen.queryByRole("group", { name: "Fields shown" })).not.toBeInTheDocument();
  });

  it("opens on the view the URL names", async () => {
    renderPage("?disease=MM&set=profile");
    await screen.findByText("weight");
    expect(diseaseSelect()).toHaveValue("MM");
    expect(screen.getByRole("button", { name: "Full record" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status", { name: "Disease view" })).toHaveTextContent("(4)");
  });

  it("reads Other as the base record, which is what its patients have", async () => {
    renderPage("?disease=Other");
    await screen.findByText("weight");
    expect(diseaseSelect()).toHaveValue("__base__");
  });

  it("still filters by the diseases the fields name when the categories do not load", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") return Promise.resolve({ data: DESCRIPTORS });
      if (url === "/v1/disease-categories/") return Promise.reject(new Error("down"));
      return Promise.resolve({ data: {} });
    });
    renderPage();
    await screen.findByText("weight");
    expect(within(diseaseSelect()).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "All diseases (5)", "Base record (2)", "BC (1)", "FL (1)", "MM (2)",
    ]);
  });
});

describe("scope column", () => {
  it("shows Base, or the Disease Categories the field belongs to", async () => {
    renderPage();
    const base = (await screen.findByText("weight")).closest("tr")!;
    expect(within(base).getAllByRole("cell")[2]).toHaveTextContent(/^Base$/);

    fireEvent.click(tab("Disease"));
    const stage = screen.getByText("stage").closest("tr")!;
    expect(within(stage).getAllByRole("cell")[2]).toHaveTextContent(/^FLMM$/);
    expect(within(stage).getByText("FL")).toBeInTheDocument();
    expect(within(stage).getByText("MM")).toBeInTheDocument();
    const her2 = screen.getByText("her2_status").closest("tr")!;
    expect(within(her2).getAllByRole("cell")[2]).toHaveTextContent(/^BC$/);
  });
});

describe("CSV downloads", () => {
  it("offers only the base record while no disease is chosen", async () => {
    renderPage();
    await screen.findByText("weight");
    expect(screen.queryByRole("button", { name: "Full record (CSV)" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disease-specific (CSV)" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Base record (CSV)" }));
    await waitFor(() => expect(saved).toEqual(["from-the-server.csv"]));
    expect(mockGet).toHaveBeenCalledWith("/v1/field-mappings/export/", { params: {}, responseType: "blob" });
  });

  it("offers the base record for the Base record view too", async () => {
    renderPage();
    await screen.findByText("weight");
    chooseDisease("__base__");
    fireEvent.click(screen.getByRole("button", { name: "Base record (CSV)" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(mockGet).toHaveBeenCalledWith("/v1/field-mappings/export/", { params: {}, responseType: "blob" });
  });

  it("downloads the chosen disease's full record under the server's filename", async () => {
    renderPage();
    await screen.findByText("weight");
    chooseDisease("MM");
    expect(screen.queryByRole("button", { name: "Base record (CSV)" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Full record (CSV)" }));
    await waitFor(() => expect(saved).toEqual(["from-the-server.csv"]));
    expect(mockGet).toHaveBeenCalledWith(
      "/v1/field-mappings/export/", { params: { disease: "MM", set: "profile" }, responseType: "blob" },
    );
    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
  });

  it("downloads the chosen disease's own fields, whichever set is listed", async () => {
    renderPage();
    await screen.findByText("weight");
    chooseDisease("BC");
    fireEvent.click(screen.getByRole("button", { name: "Full record" }));

    fireEvent.click(screen.getByRole("button", { name: "Disease-specific (CSV)" }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(mockGet).toHaveBeenCalledWith(
      "/v1/field-mappings/export/", { params: { disease: "BC", set: "extension" }, responseType: "blob" },
    );
  });

  it("names the file itself when the server sends no filename", async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve(url === "/v1/field-mappings/"
      ? { data: DESCRIPTORS }
      : url === "/v1/field-mappings/export/" ? { data: new Blob(["x"]), headers: {} } : { data: CATEGORIES }));
    renderPage();
    await screen.findByText("weight");
    chooseDisease("MM");
    fireEvent.click(screen.getByRole("button", { name: "Disease-specific (CSV)" }));
    await waitFor(() => expect(saved).toEqual(["field-mappings-MM-extension.csv"]));
  });

  it("says why a download was refused", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === "/v1/field-mappings/") return Promise.resolve({ data: DESCRIPTORS });
      if (url === "/v1/field-mappings/export/") {
        // With responseType "blob" the error body is a Blob as well.
        return Promise.reject({ response: { status: 400, data: new Blob([JSON.stringify({ detail: "Unknown disease 'MM'." })]) } });
      }
      return Promise.resolve({ data: CATEGORIES });
    });
    renderPage();
    await screen.findByText("weight");
    chooseDisease("MM");
    fireEvent.click(screen.getByRole("button", { name: "Full record (CSV)" }));
    expect(await screen.findByText("Unknown disease 'MM'.")).toBeInTheDocument();
    expect(saved).toEqual([]);
    // The page is usable again.
    expect(screen.getByRole("button", { name: "Full record (CSV)" })).toBeEnabled();
  });
});

describe("scope control from the page", () => {
  it("opens the dialog on the field's scope, offering every category but Other", async () => {
    renderPage();
    await screen.findByText("weight");
    fireEvent.click(tab("Disease"));
    fireEvent.click(within(screen.getByText("stage").closest("tr")!).getByTitle("Click to assign or edit concept"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("radio", { name: "Disease-specific" })).toBeChecked();
    const categories = within(dialog).getByRole("group", { name: "Disease Categories" });
    expect(within(categories).getAllByRole("checkbox").map((box) => box.closest("label")?.textContent)).toEqual([
      "BCBreast Cancer", "CLLChronic Lymphocytic Leukemia", "FLFollicular Lymphoma", "MMMultiple Myeloma",
    ]);
    expect(within(categories).getByRole("checkbox", { name: /^FL/ })).toBeChecked();
    expect(within(categories).getByRole("checkbox", { name: /^MM/ })).toBeChecked();
    expect(within(categories).getByRole("checkbox", { name: /^BC/ })).not.toBeChecked();
  });

  it("saves a scope for a field with no mapping row by the field's name, then reloads", async () => {
    renderPage();
    await screen.findByText("weight");
    fireEvent.click(within(screen.getByText("weight").closest("tr")!).getByTitle("Click to assign or edit concept"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("radio", { name: "Base record (every patient)" })).toBeChecked();

    fireEvent.click(within(dialog).getByRole("radio", { name: "Disease-specific" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /^CLL/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save Scope" }));

    await waitFor(() => expect(mockPut).toHaveBeenCalledWith(
      "/v1/field-mappings/weight/diseases/", { diseases: ["CLL"] },
    ));
    expect(mockPost).not.toHaveBeenCalled();
    await waitFor(() => expect(listRequests()).toHaveLength(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("a reader without mapping write access", () => {
  beforeEach(() => {
    mockUseAuth.mockReturnValue(DOCTOR);
  });

  it("sees each field's scope and may filter and download, but gets no scope control", async () => {
    renderPage();
    await screen.findByText("weight");
    expect(screen.getByText("Read-only: mapping codes needs the analyst role.")).toBeInTheDocument();

    chooseDisease("MM");
    const stage = screen.getByText("stage").closest("tr")!;
    expect(within(stage).getAllByRole("cell")[2]).toHaveTextContent(/^FLMM$/);

    // Whoever may read the mappings may download them.
    fireEvent.click(screen.getByRole("button", { name: "Disease-specific (CSV)" }));
    await waitFor(() => expect(saved).toEqual(["from-the-server.csv"]));

    // Nothing on the row opens the dialog, so there is no scope control to reach.
    expect(within(stage).queryByTitle("Click to assign or edit concept")).not.toBeInTheDocument();
    fireEvent.click(within(stage).getByText("stage"));
    fireEvent.click(within(stage).getByText("FL"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Disease-specific" })).not.toBeInTheDocument();
    expect(mockPut).not.toHaveBeenCalled();
    expect(mockPatch).not.toHaveBeenCalled();
  });
});
