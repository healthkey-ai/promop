import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import PatientConsents from "./PatientConsents";
import type { User } from "@/hooks/useAuth";

vi.mock("@/api/axios", () => ({
  default: {
    get: vi.fn(),
    patch: vi.fn(),
  },
}));

import api from "@/api/axios";

const makeUser = (overrides: Partial<User> = {}): User => ({
  id: 1,
  sub: "sub",
  email: "p@test.com",
  name: "Pat",
  is_patient: true,
  person_id: 7,
  ...overrides,
});

const mockConsents = [
  {
    id: 1,
    consent_type: "data_sharing",
    consent_granted: true,
    consent_date: "2025-06-15T10:00:00Z",
  },
  {
    id: 2,
    consent_type: "clinical_trial",
    consent_granted: false,
    consent_date: "2025-05-01T08:30:00Z",
  },
  {
    id: 3,
    consent_type: "research",
    consent_granted: true,
    consent_date: null,
  },
];

describe("PatientConsents", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders consent list after loading", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockConsents,
    });

    render(<PatientConsents user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Data Sharing")).toBeInTheDocument();
    });
    expect(screen.getByText("Clinical Trial Participation")).toBeInTheDocument();
    expect(screen.getByText("Research Use")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith("/v1/consents/");
  });

  it("toggles consent on click", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: mockConsents,
    });
    (api.patch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: {
        id: 2,
        consent_type: "clinical_trial",
        consent_granted: true,
        consent_date: "2025-07-24T12:00:00Z",
      },
    });

    render(<PatientConsents user={makeUser()} />);

    await waitFor(() => {
      expect(screen.getByText("Clinical Trial Participation")).toBeInTheDocument();
    });

    const toggleBtn = screen.getByLabelText("Toggle Clinical Trial Participation");
    fireEvent.click(toggleBtn);

    await waitFor(() => {
      expect(api.patch).toHaveBeenCalledWith("/v1/consents/2/", {
        consent_granted: true,
      });
    });
  });

  it("shows no-record message when user has no person_id", () => {
    render(<PatientConsents user={makeUser({ person_id: null })} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("shows no-record message when user is null", () => {
    render(<PatientConsents user={null} />);

    expect(
      screen.getByText(/no health record is linked/i)
    ).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("shows error message when fetch fails", async () => {
    (api.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("Network error")
    );

    render(<PatientConsents user={makeUser()} />);

    await waitFor(() => {
      expect(
        screen.getByText(/failed to load consent preferences/i)
      ).toBeInTheDocument();
    });
  });
});
