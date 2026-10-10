import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PatientSourceCodesTab, { SECTION_ROW_LIMIT } from "./PatientSourceCodesTab";
import api from "../../api/axios";

vi.mock("../../hooks/useAuth", () => ({
  useAuth: () => ({ currentUser: { email: "curator@example.com", is_staff: true } }),
}));

vi.mock("../../api/axios", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const code = (i: number) => ({
  source_value: `CODE-${i}`,
  omop_table: "measurement",
  concept_id: 0,
  concept_name: null,
  row_count: 1,
  mapping_id: null,
  mapping_status: "unmapped",
  mapping_target_concept_id: null,
  mapping_target_concept_name: null,
  source_vocabulary_id: "",
  source_code: `CODE-${i}`,
  source_unit: "",
  example_quantity: "",
  source_metadata: {},
  source_code_description: "",
});

const response = (n: number) => ({
  person_id: 1,
  summary: { total: n, unmapped: n, proposed: 0, approved: 0 },
  source_codes: Array.from({ length: n }, (_, i) => code(i)),
});

beforeEach(() => {
  vi.mocked(api.get).mockReset();
});

describe("PatientSourceCodesTab loading (#1789)", () => {
  it("says it is loading source codes until they arrive", async () => {
    let resolve: (v: unknown) => void = () => {};
    vi.mocked(api.get).mockImplementation((url: string) => (url.includes("source-codes")
      ? new Promise((r) => { resolve = r; })
      : Promise.resolve({ data: {} })) as never);
    render(<PatientSourceCodesTab personId="1" />);
    expect(screen.getByText("Loading source codes…")).toBeInTheDocument();
    resolve({ data: response(1) });
    expect(await screen.findByText("CODE-0")).toBeInTheDocument();
    expect(screen.queryByText("Loading source codes…")).not.toBeInTheDocument();
  });

  it(`renders ${SECTION_ROW_LIMIT} rows per section until asked for all`, async () => {
    const total = SECTION_ROW_LIMIT + 50;
    vi.mocked(api.get).mockImplementation((url: string) =>
      Promise.resolve({ data: url.includes("source-codes") ? response(total) : {} }) as never);
    render(<PatientSourceCodesTab personId="1" />);
    expect(await screen.findByText("CODE-0")).toBeInTheDocument();
    expect(screen.queryByText(`CODE-${SECTION_ROW_LIMIT}`)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: `Show all ${total} (50 more)` }));
    expect(screen.getByText(`CODE-${total - 1}`)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show all/ })).not.toBeInTheDocument();
  });

  it("keeps the table on screen while it refreshes", async () => {
    vi.mocked(api.get).mockImplementation((url: string) =>
      Promise.resolve({ data: url.includes("source-codes") ? response(1) : {} }) as never);
    vi.mocked(api.post).mockResolvedValue({ data: { resolved: 0, skipped: 0, already_resolved: 1 } } as never);
    render(<PatientSourceCodesTab personId="1" />);
    expect(await screen.findByText("CODE-0")).toBeInTheDocument();
    vi.mocked(api.get).mockImplementation((url: string) => (url.includes("source-codes")
      ? new Promise(() => {})
      : Promise.resolve({ data: {} })) as never);
    fireEvent.click(screen.getByRole("button", { name: "Generate OMOP" }));
    expect(await screen.findByText("Refreshing…")).toBeInTheDocument();
    expect(screen.getByText("CODE-0")).toBeInTheDocument();
  });
});
