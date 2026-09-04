/**
 * Renaming a patient must actually persist and re-render the header.
 *
 * scheduleAutoSave carried the edited name alongside the field data, but doSave
 * only ever sent data.info — so `patient_name` never reached the server. The
 * rename looked accepted, the header kept the old name, and a reload confirmed
 * the old name because Person.given_name/family_name had never changed.
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";
import PatientDetail from "./PatientDetail";

vi.mock("@/api/axios", () => ({
  default: { get: vi.fn(), patch: vi.fn(), post: vi.fn() },
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({ personId: "3542" }),
  useNavigate: () => vi.fn(),
}));

vi.mock("@/hooks/useVocabulary", () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

import api from "@/api/axios";

const PATIENT = {
  id: 1,
  person_id: 3542,
  patient_name: "Alishia Tawny Howell",
  disease: "Malignant tumor of breast",
  email: "alishia@example.com",
};

beforeEach(() => {
  vi.clearAllMocks();
  (api.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
    if (url.includes("/patient-info/")) {
      return Promise.resolve({
        data: { patient_info: PATIENT, user: null, patient_name: PATIENT.patient_name },
      });
    }
    return Promise.resolve({ data: [] });
  });
  (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
});

afterEach(() => {
  vi.useRealTimers();
});

async function renderAndWaitForLoad() {
  render(<PatientDetail />);
  await waitFor(() =>
    expect(screen.getByDisplayValue("Alishia Tawny Howell")).toBeInTheDocument(),
  );
}

describe("PatientDetail - renaming a patient", () => {
  it("sends patient_name on the PATCH when the name changes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAndWaitForLoad();

    fireEvent.change(screen.getByDisplayValue("Alishia Tawny Howell"), {
      target: { value: "Adam Blum" },
    });
    // Autosave is debounced by 2s.
    await act(async () => {
      vi.advanceTimersByTime(2100);
    });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const [, body] = (api.patch as ReturnType<typeof vi.fn>).mock.calls.at(-1)!;
    expect(body).toMatchObject({ patient_name: "Adam Blum" });
  });

  it("updates the header so the old name stops being displayed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAndWaitForLoad();
    // The header renders the loaded name before any edit.
    expect(screen.getAllByText("Alishia Tawny Howell").length).toBeGreaterThan(0);

    fireEvent.change(screen.getByDisplayValue("Alishia Tawny Howell"), {
      target: { value: "Adam Blum" },
    });
    await act(async () => {
      vi.advanceTimersByTime(2100);
    });

    await waitFor(() =>
      expect(screen.queryByText("Alishia Tawny Howell")).not.toBeInTheDocument(),
    );
    expect(screen.getAllByText("Adam Blum").length).toBeGreaterThan(0);
  });

  it("omits patient_name when only a clinical field changed", async () => {
    // Person.given_name/family_name are written unconditionally when the key is
    // present, so sending it on every autosave would rewrite the row on each
    // keystroke elsewhere in the form.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await renderAndWaitForLoad();

    fireEvent.change(screen.getByDisplayValue("alishia@example.com"), {
      target: { value: "adam@example.com" },
    });
    await act(async () => {
      vi.advanceTimersByTime(2100);
    });

    await waitFor(() => expect(api.patch).toHaveBeenCalled());
    const [, body] = (api.patch as ReturnType<typeof vi.fn>).mock.calls.at(-1)!;
    expect(body).not.toHaveProperty("patient_name");
  });
});
