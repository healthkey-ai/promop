/**
 * Live round trip: the real CodeMappingPage against a running backend.
 *
 * Nothing is mocked. This drives the real component through the real axios
 * instance, over real HTTP, to a real Django server on a real Postgres
 * database. Unit tests with a mocked API prove the component talks to the shape
 * it was told about; they cannot prove that shape is what the server sends,
 * which is the failure this exists to catch.
 *
 * Skipped unless CODE_MAPPING_LIVE_URL is set, so CI (which has no server) is
 * unaffected. The last test approves the seeded mapping, which consumes it, so
 * reset the fixture between runs:
 *
 *   DATABASE_URL=... manage.py shell < scratchpad/reset_roundtrip.py
 *
 * The component's axios instance is hardcoded to the '/api' prefix, exactly as
 * it is in the browser, so requests resolve against jsdom's origin
 * (http://localhost:3000) and reach Django through the Vite dev proxy. Both
 * servers must be up; without the proxy every request 404s at :3000 and the
 * page renders "Failed to load code mappings."
 *
 * To run it:
 *
 *   DATABASE_URL=postgresql://postgres@localhost:5432/promop_dev DEBUG=True \
 *     .venv/bin/python manage.py runserver 9200 --noreload
 *   npm --prefix frontend run dev          # the proxy this depends on
 *   CODE_MAPPING_LIVE_URL=http://localhost:9200/api \
 *     CODE_MAPPING_LIVE_TOKEN=<bearer> \
 *     CODE_MAPPING_LIVE_SOURCE_CODE=SFLC-K \
 *     npm test -- --run src/components/CodeMappings/CodeMappingPage.live.test.tsx
 */
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import axios from "axios";
import CodeMappingPage from "./CodeMappingPage";

const LIVE_URL = process.env.CODE_MAPPING_LIVE_URL;
const LIVE_TOKEN = process.env.CODE_MAPPING_LIVE_TOKEN;
const SOURCE_CODE = process.env.CODE_MAPPING_LIVE_SOURCE_CODE || "SFLC-K";

/** LOINC 33358-3, "Protein.monoclonal [Mass/volume] in Serum or Plasma". */
const DESTINATION_CONCEPT_ID = "3046299";

const describeLive = LIVE_URL && LIVE_TOKEN ? describe : describe.skip;

describeLive("CodeMappingPage against a live backend", () => {
  beforeAll(() => {
    // The component's axios reads its token from sessionStorage, exactly as it
    // does in the browser after an OAuth exchange.
    sessionStorage.setItem("access_token", LIVE_TOKEN!);
  });

  beforeEach(() => {
    axios.defaults.baseURL = LIVE_URL;
  });

  const openQueueItem = async () => {
    render(
      <MemoryRouter>
        <CodeMappingPage />
      </MemoryRouter>,
    );
    const cell = await screen.findByText(SOURCE_CODE, { selector: "td" }, { timeout: 20000 });
    const row = cell.closest("tr")!;
    fireEvent.click(row);
    await screen.findByText("Edit Mapping");
    return row;
  };

  it("serves a queue item whose every field came from the server", async () => {
    const row = await openQueueItem();

    // The import proposed this; none of it is a fixture.
    expect(row).toHaveTextContent("proposed");
    expect(screen.getByText(/Proposed by import/)).toHaveTextContent("fhir-sync");

    // Domain was derived server-side from the OMOP table by the 0192 backfill.
    expect(screen.getByLabelText("Domain")).toHaveValue("Measurement");
    // Destination table follows from the domain, and is not editable.
    const table = screen.getByLabelText("Destination Table") as HTMLInputElement;
    expect(table.value).toBe("measurement");
    expect(table.readOnly).toBe(true);
  }, 60000);

  it("labels every field in the dialog", async () => {
    // The regression test for #840's unlabelled input, run against the real
    // reference payload rather than a fixture that might not populate a select.
    await openQueueItem();
    const dialog = screen.getByText("Edit Mapping").closest("form")!;
    const controls = within(dialog).getAllByRole(
      "textbox",
    ).concat(
      within(dialog).getAllByRole("combobox"),
      within(dialog).getAllByRole("spinbutton"),
    );
    expect(controls.length).toBeGreaterThan(8);
    for (const control of controls) {
      expect(control).toHaveAccessibleName();
      expect(control.getAttribute("title")).toBeTruthy();
    }
  }, 60000);

  it("scopes the source code systems to the chosen domain, blank first", async () => {
    await openQueueItem();
    const systems = screen.getByLabelText("Source Code System") as HTMLSelectElement;
    const values = Array.from(systems.options).map((o) => o.value);

    expect(values[0]).toBe("");                       // uncoded is a real answer
    expect(values).toContain("LOINC");                // Measurement domain
    expect(values).not.toContain("NDC");              // that is a Drug system
    expect(values.some((v) => v.startsWith("HK-"))).toBe(false);

    // Switching domain re-scopes the list against the live catalogue.
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "Drug" } });
    await waitFor(() => {
      const drug = Array.from(
        (screen.getByLabelText("Source Code System") as HTMLSelectElement).options,
      ).map((o) => o.value);
      expect(drug).toContain("NDC");
      expect(drug).toContain("ATC");
    });
    expect(screen.getByLabelText("Destination Table")).toHaveValue("drug_exposure");
  }, 60000);

  it("re-points the mapping and moves the rows already stored", async () => {
    await openQueueItem();

    // Type a concept id; the live /v1/concepts/{id}/ endpoint fills the rest.
    fireEvent.change(screen.getByLabelText("Destination Concept ID"), {
      target: { value: DESTINATION_CONCEPT_ID },
    });
    fireEvent.blur(screen.getByLabelText("Destination Concept ID"));

    await waitFor(() => {
      const name = screen.getByLabelText("Destination Concept Name") as HTMLInputElement;
      expect(name.value.toLowerCase()).toContain("monoclonal");
    }, { timeout: 20000 });

    // Everything below the id is derived by the server, not typed.
    expect((screen.getByLabelText("Destination Concept Code") as HTMLInputElement).value)
      .toBe("33358-3");
    expect((screen.getByLabelText("Destination Vocabulary ID") as HTMLInputElement).value)
      .toBe("LOINC");
    expect((screen.getByLabelText("Standard Concept") as HTMLInputElement).value)
      .toBe("S");

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
    fireEvent.click(await screen.findByRole("button", { name: "Update & Approve" }));

    const outcome = await screen.findByText(/Updated \d+ row/, {}, { timeout: 40000 });
    expect(outcome).toHaveTextContent("Updated 1 row(s)");
  }, 90000);
});
