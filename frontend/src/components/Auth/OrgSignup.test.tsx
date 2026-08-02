import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import OrgSignup from "./OrgSignup";

// Mock publicApi
const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

function renderOrgSignup(slug = "acme") {
  return render(
    <MemoryRouter initialEntries={[`/org/${slug}/signup`]}>
      <Routes>
        <Route path="/org/:slug/signup" element={<OrgSignup />} />
        <Route path="/org/:slug/login" element={<div>ORG_LOGIN</div>} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrgSignup", () => {
  it("shows error when org not found", async () => {
    mockGet.mockRejectedValue({ response: { status: 404 } });
    renderOrgSignup();
    await waitFor(() => {
      expect(screen.getByText("Organization not found.")).toBeInTheDocument();
    });
  });

  it("shows signup-disabled message when org does not allow signup", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: false },
    });
    renderOrgSignup();
    await waitFor(() => {
      expect(screen.getByText("Direct signup is not available for this organization.")).toBeInTheDocument();
    });
    expect(screen.getByText("Back to sign in")).toBeInTheDocument();
  });

  it("renders signup form when org allows signup", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgSignup();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create account" })).toBeInTheDocument();
  });

  it("shows error when password is too short", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgSignup();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "test@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(screen.getByText("Password must be at least 12 characters.")).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("shows error when passwords do not match", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgSignup();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "test@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-alpha-1" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "password-bravo-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("shows backend error on signup failure", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    mockPost.mockRejectedValue({
      response: { data: { error: "An account with this email already exists. Please log in instead." }, status: 409 },
    });
    renderOrgSignup();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "exists@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password-alpha-1" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "password-alpha-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByText(/already exists/)).toBeInTheDocument();
    });
  });

  it("handles array error from backend password validation", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    mockPost.mockRejectedValue({
      response: { data: { error: ["This password is too common.", "This password is entirely numeric."] }, status: 400 },
    });
    renderOrgSignup();

    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "test@test.com" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "123456789012" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "123456789012" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByText("This password is too common. This password is entirely numeric.")).toBeInTheDocument();
    });
  });

  it("shows name fields as optional", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgSignup();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    expect(screen.getAllByText("(optional)")).toHaveLength(2);
  });

  it("shows password requirements hint", async () => {
    mockGet.mockResolvedValue({
      data: { name: "Acme Clinic", slug: "acme", allows_patient_signup: true },
    });
    renderOrgSignup();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    expect(screen.getByText(/not a common password/)).toBeInTheDocument();
  });
});
