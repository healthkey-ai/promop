/**
 * ZIP autofill → autosave payload.
 *
 * `region` is written to OMOP Location.state, which the CDM caps at two characters, and CB's
 * federation PATCH refuses a longer value for the whole request. The lookup's `state` is the full
 * name, so the widget must send `state abbreviation` — and nothing, rather than a full name, when
 * the abbreviation is missing.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AxiosInstance } from "axios";

const mutate = vi.fn();
// One object for every render: the component resets its edits whenever `data` changes identity.
const data = { patient_info: { country: "United States" }, user: null, patient_name: "Patient" };

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

function mockLookup(place: Record<string, string>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ places: [place] }) })),
  );
}

async function typeZipAndWaitForSave(zip: string) {
  render(<PatientInfo apiClient={{} as AxiosInstance} federated />);
  fireEvent.change(screen.getByPlaceholderText(/5-digit US zip code/i), { target: { value: zip } });
  await waitFor(() => expect(mutate).toHaveBeenCalled(), { timeout: 4000 });
  return mutate.mock.calls[0][0] as Record<string, unknown>;
}

describe("PatientInfo ZIP autofill", () => {
  beforeEach(() => mutate.mockReset());
  afterEach(() => vi.unstubAllGlobals());

  it("sends the state abbreviation, not the full name", async () => {
    mockLookup({ "place name": "Beverly Hills", state: "California", "state abbreviation": "CA" });
    const sent = await typeZipAndWaitForSave("90210");
    expect(sent).toEqual({ postal_code: "90210", city: "Beverly Hills", region: "CA" });
  });

  it("leaves region out when the lookup has no abbreviation", async () => {
    mockLookup({ "place name": "Beverly Hills", state: "California" });
    const sent = await typeZipAndWaitForSave("90210");
    expect(sent).toEqual({ postal_code: "90210", city: "Beverly Hills" });
  });
});
