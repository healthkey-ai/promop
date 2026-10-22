import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RelevanceSignalsPage from "./RelevanceSignalsPage";

const mocks = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: mocks }));
const ADMIN = { can_accept: true, can_reject: true };
const DOCTOR = { can_accept: false, can_reject: true };
const ANALYST = { can_accept: false, can_reject: false };

const signal = (overrides: Record<string, unknown>) => ({
  id: 1, source: "cdew", disease: "MM", concept_id: 4123456, concept_name: "Bone marrow aspirate",
  period: "2026-09", document_count: 30, document_total: 100, status: "proposed", reviewed_at: null, ...overrides,
});

function renderPage(path = "/relevance-signals") {
  return render(<MemoryRouter initialEntries={[path]}><RelevanceSignalsPage /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.get.mockResolvedValue({ data: { permissions: ADMIN, results: [
    signal({}),
    signal({ id: 2, period: "2026-10", document_count: 41, document_total: 120 }),
    signal({ id: 3, disease: "BC", concept_id: 77, concept_name: "Mammogram", document_count: 9, document_total: null }),
  ] } });
  mocks.patch.mockResolvedValue({ data: {} });
});

describe("Relevance signals", () => {
  it("shows one row per disease and concept, with its latest period", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: "Relevance signals" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Breast cancer");
    expect(rows[0]).toHaveTextContent("Mammogram");
    expect(rows[1]).toHaveTextContent("Multiple myeloma");
    expect(rows[1]).toHaveTextContent("41 of 120 (34%)");
    expect(rows[1]).toHaveTextContent("2026-10");
    expect(mocks.get).toHaveBeenCalledWith("/v1/relevance/signals/", { params: { status: "proposed" } });
  });

  it("accepts a pair and reloads the queue", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Accept Bone marrow aspirate for MM" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith("/v1/relevance/signals/2/", { status: "accepted" }));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
  });

  it("filters by status and disease", async () => {
    renderPage("/relevance-signals?status=accepted&disease=MM");
    await waitFor(() => expect(mocks.get).toHaveBeenCalledWith("/v1/relevance/signals/",
      { params: { status: "accepted", disease: "MM" } }));
    fireEvent.click(screen.getByRole("tab", { name: "Rejected" }));
    await waitFor(() => expect(mocks.get).toHaveBeenLastCalledWith("/v1/relevance/signals/",
      { params: { status: "rejected", disease: "MM" } }));
  });

  it("lets a decided pair return to proposed", async () => {
    mocks.get.mockResolvedValue({ data: { permissions: ADMIN, results: [signal({ status: "rejected" })] } });
    renderPage("/relevance-signals?status=rejected");
    fireEvent.click(await screen.findByRole("button", { name: "Return Bone marrow aspirate for MM to proposed" }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith("/v1/relevance/signals/1/", { status: "proposed" }));
  });

  it("is read-only for analysts", async () => {
    mocks.get.mockResolvedValue({ data: { permissions: ANALYST, results: [signal({})] } });
    renderPage();
    await screen.findByText("Bone marrow aspirate");
    expect(screen.queryByRole("button", { name: /Accept|Reject|Return/ })).not.toBeInTheDocument();
    expect(screen.getByText(/You can view the queue/)).toBeInTheDocument();
  });

  it("lets a doctor reject and reopen, but not accept or reopen an accepted pair", async () => {
    mocks.get.mockResolvedValue({ data: { permissions: DOCTOR, results: [signal({})] } });
    const { unmount } = renderPage();
    expect(await screen.findByRole("button", { name: "Reject Bone marrow aspirate for MM" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
    unmount();
    mocks.get.mockResolvedValue({ data: { permissions: DOCTOR, results: [signal({ status: "rejected" })] } });
    const second = renderPage("/relevance-signals?status=rejected");
    expect(await screen.findByRole("button", { name: /Return .* to proposed/ })).toBeInTheDocument();
    second.unmount();
    mocks.get.mockResolvedValue({ data: { permissions: DOCTOR, results: [signal({ status: "accepted" })] } });
    renderPage("/relevance-signals?status=accepted");
    await screen.findByText("Bone marrow aspirate");
    expect(screen.queryByRole("button", { name: /Return/ })).not.toBeInTheDocument();
  });

  it("says when the queue is empty", async () => {
    mocks.get.mockResolvedValue({ data: { results: [] } });
    renderPage("/relevance-signals?disease=FL");
    expect(await screen.findByText("No proposed signals for Follicular lymphoma.")).toBeInTheDocument();
  });

  it("reports a failed load", async () => {
    mocks.get.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load relevance signals.");
  });
});
