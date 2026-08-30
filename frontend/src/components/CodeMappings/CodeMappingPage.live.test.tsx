/**
 * Live round trip: the real CodeMappingPage against a running backend.
 *
 * Nothing is mocked here -- this drives the real component through the real
 * axios instance, over real HTTP, to a real Django server on a real Postgres
 * database. Passing unit tests with a mocked API prove the component talks to
 * the shape it was told about; they cannot prove that shape is what the server
 * sends, which is the failure this exists to catch.
 *
 * Skipped unless CODE_MAPPING_LIVE_URL is set, so CI (which has no server) is
 * unaffected. To run it:
 *
 *   DATABASE_URL=postgresql://postgres@localhost:5432/promop_dev DEBUG=True \
 *     .venv/bin/python manage.py runserver 9200
 *   CODE_MAPPING_LIVE_URL=http://localhost:9200/api \
 *     CODE_MAPPING_LIVE_TOKEN=<bearer> \
 *     npm test -- --run src/components/CodeMappings/CodeMappingPage.live.test.tsx
 */
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, beforeAll, beforeEach } from "vitest";
import axios from "axios";
import CodeMappingPage from "./CodeMappingPage";

const LIVE_URL = process.env.CODE_MAPPING_LIVE_URL;
const LIVE_TOKEN = process.env.CODE_MAPPING_LIVE_TOKEN;

// The source code the seeded FHIR import left in the review queue.
const SOURCE_CODE = "SFLC-K";
const SOURCE_TEXT = "SERUM FREE LIGHT CHAIN KAPPA";

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

  it("shows the import's proposal, re-points it, and moves the stored rows", async () => {
    // --- 1. the queue the import filled -------------------------------------
    render(
      <MemoryRouter>
        <CodeMappingPage />
      </MemoryRouter>,
    );

    // The page reaches the real /v1/code-mappings/ and /reference/ endpoints.
    const cell = await screen.findByText(SOURCE_CODE, { selector: "td" }, { timeout: 15000 });
    const row = cell.closest("tr")!;

    // Everything below comes from the server, not a fixture.
    expect(within(row).getAllByRole("cell")[1]).toHaveTextContent("uncoded");
    expect(row).toHaveTextContent(SOURCE_TEXT);   // the minted HK-Labs concept
    expect(row).toHaveTextContent("proposed");

    // The tab strip is built from the live reference endpoint, so SNOMED and
    // LOINC being present proves the server offers standard destinations.
    const tabs = within(screen.getByRole("tablist", { name: "Destination vocabularies" }));
    expect(tabs.getByRole("button", { name: /LOINC/ })).toBeInTheDocument();
    expect(tabs.getByRole("button", { name: /HK-Labs/ })).toBeInTheDocument();

    // --- 2. the curator re-points it at a real Athena concept ---------------
    fireEvent.click(row);
    await screen.findByText("Edit Mapping");

    // Provenance comes back from the server: this is a machine's guess.
    expect(screen.getByText(/Proposed by import/)).toHaveTextContent("fhir-sync");

    // Source Code System is a real select fed by the live reference endpoint.
    const systemSelect = screen.getByLabelText("Source Code System") as HTMLSelectElement;
    expect(Array.from(systemSelect.options).map((o) => o.value)).toContain("ICD10CM");

    // LOINC 33358-3 "Protein.monoclonal [Mass/volume] in Serum or Plasma".
    fireEvent.change(screen.getByLabelText("Destination Concept ID"), {
      target: { value: "3046299" },
    });
    // onBlur resolves the id against the live /v1/concepts/{id}/ endpoint and
    // back-fills the name, vocabulary and class -- none of which is typed.
    fireEvent.blur(screen.getByLabelText("Destination Concept ID"));

    await waitFor(() => {
      expect(screen.getByTestId("destination-concept-class")).not.toHaveTextContent("—");
    }, { timeout: 15000 });
    expect(
      (screen.getByLabelText("Destination Concept Name") as HTMLInputElement).value,
    ).toMatch(/monoclonal/i);

    // --- 3. approve, which re-points the rows already stored ----------------
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "approved" } });
    fireEvent.click(await screen.findByRole("button", { name: "Update & Approve" }));

    const outcome = await screen.findByText(/Updated \d+ row/, {}, { timeout: 30000 });
    // One measurement carried this code, and it moved.
    expect(outcome).toHaveTextContent("Updated 1 row(s)");
  }, 60000);
});
