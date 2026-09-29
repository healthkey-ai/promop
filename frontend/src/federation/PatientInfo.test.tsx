/**
 * ZIP autofill → autosave payload.
 *
 * `region` is written to OMOP Location.state, which the CDM caps at two characters, and CB's
 * federation PATCH refuses a longer value for the whole request. The lookup's `state` is the full
 * name, so the widget must send `state abbreviation` — and nothing, rather than a full name, when
 * the abbreviation is missing. The lookup is US-only, so it must not run for another country.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AxiosInstance } from "axios";

const mutate = vi.fn();
// Set before each render and not replaced during it: the component resets its edits whenever
// `data` changes identity.
let data: Record<string, unknown>;

vi.mock("./patientInfoHooks", () => ({
  usePatientInfoMe: () => ({
    data,
    isLoading: false,
    isError: false,
    error: null,
  }),
  usePatchPatientInfo: () => ({ mutate }),
}));

vi.mock("@/hooks/useVocabulary", () => ({
  useVocabulary: () => ({ options: [], loading: false, source: null }),
}));

import PatientInfo from "./PatientInfo";

// The autosave debounce is 2s of real time; leave room above it.
const TIMEOUT = 8000;

function mockLookup(place: Record<string, string>) {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ places: [place] }) }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function typeZipAndWaitForSave(country: string, zip: string) {
  data = { patient_info: { country }, user: null, patient_name: "Patient" };
  render(<PatientInfo apiClient={{} as AxiosInstance} federated />);
  fireEvent.change(screen.getByPlaceholderText(/5-digit US zip code/i), { target: { value: zip } });
  await waitFor(() => expect(mutate).toHaveBeenCalled(), { timeout: TIMEOUT - 1000 });
  return mutate.mock.calls[0][0] as Record<string, unknown>;
}

const BEVERLY_HILLS = { "place name": "Beverly Hills", state: "California", "state abbreviation": "CA" };

describe("PatientInfo ZIP autofill", () => {
  beforeEach(() => mutate.mockReset());
  afterEach(() => vi.unstubAllGlobals());

  it("sends the state abbreviation, not the full name", async () => {
    mockLookup(BEVERLY_HILLS);
    const sent = await typeZipAndWaitForSave("United States", "90210");
    expect(sent).toStrictEqual({ postal_code: "90210", city: "Beverly Hills", region: "CA" });
  }, TIMEOUT);

  it("leaves region out when the lookup has no abbreviation", async () => {
    mockLookup({ "place name": "Beverly Hills", state: "California" });
    const sent = await typeZipAndWaitForSave("United States", "90210");
    expect(sent).toStrictEqual({ postal_code: "90210", city: "Beverly Hills" });
  }, TIMEOUT);

  it("does not look up a 5-digit postcode outside the US", async () => {
    const fetch = mockLookup(BEVERLY_HILLS);
    const sent = await typeZipAndWaitForSave("Germany", "10115");
    expect(sent).toStrictEqual({ postal_code: "10115" });
    expect(fetch).not.toHaveBeenCalled();
  }, TIMEOUT);
});
