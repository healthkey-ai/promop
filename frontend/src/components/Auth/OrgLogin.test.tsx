import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import OrgLogin from "./OrgLogin";

// Mock publicApi
const mockGet = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: { get: (...args: unknown[]) => mockGet(...args) },
}));

// Mock useAuth
const mockLogin = vi.fn();
vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ login: mockLogin }),
}));

function renderOrgLogin(slug = "acme") {
  return render(
    <MemoryRouter initialEntries={[`/org/${slug}/login`]}>
      <Routes>
        <Route path="/org/:slug/login" element={<OrgLogin />} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrgLogin", () => {
  it("shows spinner while loading org info", () => {
    mockGet.mockReturnValue(new Promise(() => {})); // never resolves
    renderOrgLogin();
    const spinner = document.querySelector(".animate-spin");
    expect(spinner).toBeInTheDocument();
  });

  it("renders org name after fetching org info", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: false },
    });
    renderOrgLogin();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
  });

  it("shows error when org not found", async () => {
    mockGet.mockRejectedValue({ response: { status: 404 } });
    renderOrgLogin();
    await waitFor(() => {
      expect(screen.getByText("Organization not found.")).toBeInTheDocument();
    });
  });

  it("shows signup link when allows_patient_signup is true", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgLogin();
    await waitFor(() => {
      expect(screen.getByText("Sign up")).toBeInTheDocument();
    });
  });

  it("hides signup link when allows_patient_signup is false", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: false },
    });
    renderOrgLogin();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    expect(screen.queryByText("Sign up")).not.toBeInTheDocument();
  });

  it("shows error on login failure", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: false },
    });
    mockLogin.mockResolvedValue({ success: false, error: "Invalid credentials" });
    renderOrgLogin();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "test@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByText("Invalid credentials")).toBeInTheDocument();
    });
  });

  it("blocks non-patient user after successful login", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: false },
    });
    mockLogin.mockResolvedValue({ success: true, user: { is_patient: false } });
    renderOrgLogin();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "provider@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByText(/This login page is for patients/)).toBeInTheDocument();
    });
  });
});
