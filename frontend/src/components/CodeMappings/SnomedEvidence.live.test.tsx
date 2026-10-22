/**
 * Live round trip for SNOMED destinations: the real CodeMappingPage against a
 * running backend, nothing mocked.
 *
 * Skipped unless CODE_MAPPING_LIVE_URL and CODE_MAPPING_LIVE_TOKEN are set.
 * Needs the ICD-10-CM M17.11 mapping and the real SNOMED neighbourhood of
 * "Osteoarthritis of knee" (concepts, attribute relationships, concept
 * ancestors) on the database; servers as in CodeMappingPage.live.test.tsx.
 * The test restores the row's SNOMED settings when it finishes.
 */
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import api from "../../api/axios";
import CodeMappingPage from "./CodeMappingPage";

const LIVE_URL = process.env.CODE_MAPPING_LIVE_URL;
const LIVE_TOKEN = process.env.CODE_MAPPING_LIVE_TOKEN;
const CODE = "M17.11";

const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

describeLive("SNOMED evidence against a live backend", () => {
  let mappingId: number | null = null;

  beforeAll(() => {
    // The app authenticates by session cookie; a bearer token on the app's
    // own axios instance stands in for the login.
    api.interceptors.request.use(config => {
      config.headers.Authorization = `Bearer ${LIVE_TOKEN}`;
      // The row's id, from the panel's own request, so cleanup needs no lookup.
      const match = config.url?.match(/code-mappings\/(\d+)\/snomed-attributes/);
      if (match) mappingId = Number(match[1]);
      return config;
    });
  });

  afterAll(async () => {
    if (mappingId) await api.put(`/v1/code-mappings/${mappingId}/snomed-attributes/`, { snomed_attributes: {} });
  });

  it("shows the reference and side, holds back what the source does not support, and saves a setting", async () => {
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Search mappings", {}, { timeout: 30000 }),
      { target: { value: CODE } });
    // An Athena row is approved, and the Approved section starts collapsed.
    fireEvent.click(await screen.findByRole("button", { name: /^Approved/ }, { timeout: 30000 }));
    // Rolled up by description, like every section.
    fireEvent.click(await screen.findByRole("button",
      { name: "Expand Unilateral primary osteoarthritis, right knee" }, { timeout: 30000 }));
    const code = await screen.findByText(CODE, { selector: "[data-testid=source-code]" }, { timeout: 30000 });
    fireEvent.click(code.closest("tr")!);

    const panel = await screen.findByRole("region", { name: "SNOMED evidence" }, { timeout: 20000 });
    // From the server: Athena's "Maps to", and the side named in the code's text.
    await waitFor(() => expect(within(panel).getByTestId("snomed-reference")).toHaveTextContent("Osteoarthritis of knee"));
    expect(within(panel).getByTestId("snomed-laterality")).toHaveTextContent("Right");
    expect(within(panel).getByTestId("snomed-laterality-why")).toHaveTextContent(/source text/i);

    // Suggest: the lateralised concept first, the other side and siblings held back.
    // The dialog's Suggest, not the page's batch run.
    fireEvent.click(within(panel.closest('[role="dialog"]') as HTMLElement).getByRole("button", { name: /^Suggest$/ }));
    const winner = await screen.findByText("Osteoarthritis of right knee joint", {}, { timeout: 60000 });
    expect(await screen.findByText(/Source means at least/)).toBeInTheDocument();
    expect(screen.getByText(/contradict the source/)).toBeInTheDocument();
    expect(screen.getByText(/more specific than the source supports/)).toBeInTheDocument();
    // In the candidate table, the lateralised concept comes before the
    // unlateralised one Athena maps to.
    const list = screen.getByRole("region", { name: "Individual suggestion candidates" });
    const names = within(list).getAllByRole("row").map(row => row.textContent || "");
    const right = names.findIndex(text => text.includes("Osteoarthritis of right knee joint"));
    const plain = names.findIndex(text => /Osteoarthritis of knee(?! joint)/.test(text) && !text.includes("right"));
    expect(right).toBeGreaterThanOrEqual(0);
    expect(plain).toBeGreaterThan(right);
    expect(within(list).getAllByText(/record laterality separately/).length).toBeGreaterThan(0);
    void winner;

    // A curator setting goes to the server and comes back as the curator's.
    fireEvent.change(within(panel).getByLabelText("Laterality"), { target: { value: "Left" } });
    fireEvent.click(within(panel).getByRole("button", { name: /Save SNOMED evidence/ }));
    // "(unsaved)" until the PUT returns; wait for the saved state.
    await waitFor(() => {
      const why = within(panel).getByTestId("snomed-laterality-why").textContent || "";
      expect(why).toMatch(/curator/i);
      expect(why).not.toMatch(/unsaved/i);
    }, { timeout: 20000 });
    const saved = await api.get(`/v1/code-mappings/${mappingId}/snomed-attributes/`);
    expect(saved.data.snomed_attributes).toEqual({ laterality: "Left" });
    expect(saved.data.source.laterality).toMatchObject({ value: "Left", basis: "curator" });
  }, 180000);
});
