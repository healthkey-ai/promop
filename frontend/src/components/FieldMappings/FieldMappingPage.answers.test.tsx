/**
 * The Answers column of the Field Mappings list (#1225): how many of a field's
 * curated answers have a concept decision, read from the list's own response.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import FieldMappingPage from "./FieldMappingPage";

const mockGet = vi.fn();
const mockUseAuth = vi.fn();
const editorOpened = vi.fn();

vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    put: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));
vi.mock("./FieldChoiceEditor", () => ({
  FieldChoiceEditor: ({ fieldName }: { fieldName: string }) => {
    editorOpened(fieldName);
    return <div>Editor for {fieldName}</div>;
  },
}));
vi.mock("./FormulaEditDialog", () => ({ FormulaEditDialog: () => null }));

const field = (field_name: string, extra: Record<string, unknown> = {}) => ({
  field_name, field_type: "text", category: "needs-concept-set", tab: "general",
  scope: "base", diseases: [], provenance: null, mapping: null, suggestion: null,
  mappable: true, locked_table: null, unit_options: [], choices: [], formula: null,
  explanation: null, derivation_error: null, answer_coverage: null, ...extra,
});

const DESCRIPTORS = [
  field("ecog", { answer_coverage: { choices: 5, mapped: 2, no_equivalent: 1, proposed: 1, unmapped: 1 } }),
  field("smoking_status", { answer_coverage: { choices: 3, mapped: 3, no_equivalent: 0, proposed: 0, unmapped: 0 } }),
  field("weight"),
  // A dropdown created through Add Field: its answers are the same rows, edited in the same place.
  field("gleason_grade_group", {
    field_type: "choice",
    answer_coverage: { choices: 3, mapped: 2, no_equivalent: 0, proposed: 0, unmapped: 1 },
  }),
];

const READER = { currentUser: {
  id: 2, sub: "doc", email: "doc@example.com", name: "Doc", is_staff: false,
  org_accesses: [{ role: "doctor", org_name: "Org", org_slug: "org", expires_at: null }],
} };

const renderPage = () =>
  render(<MemoryRouter initialEntries={["/field-mappings"]}><FieldMappingPage /></MemoryRouter>);

const row = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({ currentUser: { is_staff: true, is_org_admin: false } });
  mockGet.mockImplementation((url: string) => {
    if (url === "/v1/field-mappings/") return Promise.resolve({ data: DESCRIPTORS });
    if (url.startsWith("/v1/field-synonyms/batch/")) return Promise.resolve({ data: {} });
    return Promise.resolve({ data: [] });
  });
});

describe("Answers column", () => {
  it("says how many answers have a decision, and which are still open", async () => {
    renderPage();
    await screen.findByText("ecog");
    expect(within(row("ecog")).getByText("3 of 5 decided")).toHaveClass("text-amber-700");
    expect(within(row("ecog")).getByText("3 of 5 decided")).toHaveAttribute(
      "title", "2 with a concept, 1 with no equivalent, 1 waiting for approval, 1 with no proposal");
    expect(within(row("smoking_status")).getByText("3 of 3 decided")).toHaveClass("text-green-700");
  });

  it("opens a created dropdown's answers in the same editor", async () => {
    renderPage();
    await screen.findByText("gleason_grade_group");
    expect(within(row("gleason_grade_group")).getByText("2 of 3 decided")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open the answers of gleason_grade_group" }));
    expect(await screen.findByText("Editor for gleason_grade_group")).toBeInTheDocument();
  });

  it("shows a dash for a field with no curated answers", async () => {
    renderPage();
    await screen.findByText("weight");
    const cells = within(row("weight")).getAllByRole("cell");
    const column = within(screen.getAllByRole("table")[0]).getAllByRole("columnheader")
      .findIndex((th) => th.textContent === "Answers");
    expect(cells[column]).toHaveTextContent("—");
  });

  it("opens the field's answers from the figure for a curator", async () => {
    renderPage();
    await screen.findByText("ecog");
    fireEvent.click(screen.getByRole("button", { name: "Open the answers of ecog" }));
    expect(await screen.findByText("Editor for ecog")).toBeInTheDocument();
  });

  it("is plain text for a reader who cannot write mappings", async () => {
    mockUseAuth.mockReturnValue(READER);
    renderPage();
    await screen.findByText("ecog");
    expect(screen.getByText("3 of 5 decided")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open the answers of/ })).not.toBeInTheDocument();
  });
});
