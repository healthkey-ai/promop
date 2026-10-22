/**
 * Live round trip for LOINC axes and axis variants: the real CodeMappingPage
 * against a running backend, nothing mocked.
 *
 * Skipped unless CODE_MAPPING_LIVE_URL and CODE_MAPPING_LIVE_TOKEN are set. Seed promop_dev first with a
 * hospital glucose code seen in mg/dL and mmol/L (vocabulary AXRT, code GLU)
 * and LoincCodeClass loaded from a LOINC release; servers as in
 * CodeMappingPage.live.test.tsx.
 */
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, beforeAll } from "vitest";
import api from "../../api/axios";
import CodeMappingPage from "./CodeMappingPage";

const LIVE_URL = process.env.CODE_MAPPING_LIVE_URL;
const LIVE_TOKEN = process.env.CODE_MAPPING_LIVE_TOKEN;
const SOURCE_CODE = process.env.CODE_MAPPING_LIVE_AXES_CODE || "GLU";

const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

describeLive("LOINC axes against a live backend", () => {
  beforeAll(() => {
    // The app authenticates by session cookie; a bearer token on the app's own
    // axios instance stands in for the login. Requests still go to "/api" on
    // jsdom's origin and reach Django through the Vite dev proxy.
    api.interceptors.request.use(config => {
      config.headers.Authorization = `Bearer ${LIVE_TOKEN}`;
      return config;
    });
  });

  const openRow = async () => {
    render(<MemoryRouter><CodeMappingPage /></MemoryRouter>);
    // The queue opens on its first vocabulary tab; the seeded code has its own.
    fireEvent.click(await screen.findByRole("tab", { name: /AXRT/ }, { timeout: 30000 }));
    // Unmapped codes are rolled up by description; the group carries the flag too.
    const expand = await screen.findByRole("button", { name: "Expand Glucose" }, { timeout: 30000 });
    const group = expand.closest("tr")!;
    expect(within(group).getByText("2 axis values").className).toMatch(/blue/);
    fireEvent.click(expand);
    const code = await screen.findByText(SOURCE_CODE, {}, { timeout: 30000 });
    const row = code.closest("tr")!;
    return row;
  };

  it("flags the code, infers its axes, saves an entry and approves a variant", async () => {
    const row = await openRow();
    // The flag the queue shows for a code whose units imply two LOINC tests.
    const flag = within(row).getByText("2 axis values");
    expect(flag.className).toMatch(/blue/);

    fireEvent.click(row);
    const panel = await screen.findByRole("region", { name: "LOINC axes" }, { timeout: 20000 });

    // Inferred from the unit in the stored metadata, not typed by anyone.
    await waitFor(() => expect(within(panel).getByTestId("axis-value-scale_type")).toHaveTextContent("Qn"));
    const variants = within(panel).getByRole("table", { name: "Axis variants" });
    expect(variants).toHaveTextContent("mg/dL");
    expect(variants).toHaveTextContent("mmol/L");

    // A curator settles the component; the server stores it and says so.
    fireEvent.change(within(panel).getByLabelText("Component value"), { target: { value: "Glucose" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save axes" }));
    await waitFor(() => expect(within(panel).getByTestId("axis-why-component")).toHaveTextContent(/Set by curator$/));

    // Point the mmol/L variant at the substance-concentration LOINC and approve it.
    // The panel re-renders after each write, so the row is looked up afresh.
    const mmolRow = () => within(within(panel).getByRole("table", { name: "Axis variants" }))
      .getByText("mmol/L").closest("tr")!;
    fireEvent.click(within(mmolRow()).getByRole("button", { name: /^Set destination/ }));
    const search = await within(panel).findByLabelText(/^Search destination/);
    fireEvent.change(search, { target: { value: "14749-6" } });
    const option = await within(panel).findByText(/14749-6/, {}, { timeout: 20000 });
    fireEvent.click(option);
    await waitFor(() => expect(within(mmolRow()).getByText(/14749-6/)).toBeInTheDocument(), { timeout: 20000 });
    // Approve stays disabled until the destination write settles.
    await waitFor(() => expect(within(mmolRow()).getByRole("button", { name: /^Approve/ })).toBeEnabled(),
      { timeout: 20000 });
    fireEvent.click(within(mmolRow()).getByRole("button", { name: /^Approve/ }));
    await waitFor(() => expect(mmolRow()).toHaveTextContent(/approved/i), { timeout: 20000 });

    // And ingest now routes by unit: the ETL lookup sends mmol/L to 14749-6.
    const lookup = await api.post("/v1/code-mappings/lookup/", { codes: [
      { source_vocabulary_id: "AXRT", source_code: SOURCE_CODE, omop_table: "measurement", source_unit: "mmol/L" },
    ] });
    expect(lookup.data.mappings[`AXRT|${SOURCE_CODE}|mmol/L`].target_concept_code).toBe("14749-6");
  }, 120000);
});
