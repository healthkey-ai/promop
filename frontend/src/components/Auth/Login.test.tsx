import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import { Login } from "./Login";

const mockGet = vi.fn();
const mockPost = vi.fn();
vi.mock("@/api/publicAxios", () => ({
  publicApi: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

const mockApiPost = vi.fn();
vi.mock("@/api/axios", () => ({
  default: { post: (...args: unknown[]) => mockApiPost(...args) },
}));

vi.mock("@/config/branding", () => ({
  getActiveBranding: () => ({ appName: "PRomop", tagline: "Test tagline" }),
}));

const ORGS = [
  { name: "Acme Clinic", slug: "acme" },
  { name: "Beta Health", slug: "beta" },
];

function renderLogin() {
  return render(
    <MemoryRouter>
      <Login />
    </MemoryRouter>
  );
}

/** Render and wait for the signup-directory fetch to settle. */
async function renderWithOrgs(orgs = ORGS) {
  mockGet.mockResolvedValue({ data: orgs });
  renderLogin();
  await waitFor(() => {
    expect(screen.getByRole("tab", { name: "Sign Up" })).toBeInTheDocument();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Login - sign-in mode", () => {
  it("shows the sign-in form by default", async () => {
    await renderWithOrgs();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Organization")).not.toBeInTheDocument();
  });

  it("posts credentials to the login endpoint", async () => {
    await renderWithOrgs();
    mockApiPost.mockResolvedValue({ data: { user: { id: 1 } } });

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "drwho" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "tardis" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(mockApiPost).toHaveBeenCalledWith("/auth/login/", {
        username: "drwho",
        password: "tardis",
      });
    });
  });
});

describe("Login - signup tab visibility", () => {
  it("hides the toggle when no org accepts self-signup", async () => {
    mockGet.mockResolvedValue({ data: [] });
    renderLogin();
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/v1/orgs/signup-directory/"));
    expect(screen.queryByRole("tab", { name: "Sign Up" })).not.toBeInTheDocument();
    // Sign-in still works as before.
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
  });

  it("hides the toggle when the directory request fails", async () => {
    mockGet.mockRejectedValue({ response: { status: 500 } });
    renderLogin();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(screen.queryByRole("tab", { name: "Sign Up" })).not.toBeInTheDocument();
  });

  it("shows the toggle when at least one org accepts self-signup", async () => {
    await renderWithOrgs();
    expect(screen.getByRole("tab", { name: "Sign In" })).toBeInTheDocument();
  });
});

describe("Login - signup mode", () => {
  it("switches to the signup form and lists the orgs", async () => {
    await renderWithOrgs();
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    expect(screen.getByLabelText("Organization")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.com" } });
    expect(await screen.findByRole("option", { name: "Acme Clinic" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Beta Health" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
  });

  it("preselects the only org when exactly one qualifies", async () => {
    await renderWithOrgs([{ name: "Solo Clinic", slug: "solo" }]);
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.com" } });
    await waitFor(() => expect((screen.getByLabelText("Organization") as HTMLSelectElement).value).toBe("solo"));
  });

  it("does not preselect when several orgs qualify", async () => {
    await renderWithOrgs();
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));
    expect((screen.getByLabelText("Organization") as HTMLSelectElement).value).toBe("");
  });

  it("rejects a password shorter than 12 characters without posting", async () => {
    await renderWithOrgs([{ name: "Solo Clinic", slug: "solo" }]);
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "p@example.com" } });
    await screen.findByRole("option", { name: "Solo Clinic" });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByText("Password must be at least 12 characters.")).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("rejects mismatched passwords without posting", async () => {
    await renderWithOrgs([{ name: "Solo Clinic", slug: "solo" }]);
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "p@example.com" } });
    await screen.findByRole("option", { name: "Solo Clinic" });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "correct-horse-batteryX" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("requires an organization when none is preselected", async () => {
    await renderWithOrgs();
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "p@example.com" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByText("Please choose an organization.")).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("posts to the selected org's patient-signup endpoint", async () => {
    await renderWithOrgs();
    mockPost.mockResolvedValue({ data: {} });
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText(/First name/), { target: { value: "Ada" } });
    fireEvent.change(screen.getByLabelText(/Last name/), { target: { value: "Lovelace" } });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@example.com" } });
    await screen.findByRole("option", { name: "Beta Health" });
    fireEvent.change(screen.getByLabelText("Organization"), { target: { value: "beta" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/v1/orgs/beta/patient-signup/", {
        email: "ada@example.com",
        password: "correct-horse-battery",
        given_name: "Ada",
        family_name: "Lovelace",
      });
    });
  });

  it("surfaces field errors returned by the backend", async () => {
    await renderWithOrgs([{ name: "Solo Clinic", slug: "solo" }]);
    mockPost.mockRejectedValue({
      response: { data: { errors: { email: ["An account with this email already exists."] } } },
    });
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "taken@example.com" } });
    await screen.findByRole("option", { name: "Solo Clinic" });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "correct-horse-battery" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(
        screen.getByText("An account with this email already exists.")
      ).toBeInTheDocument();
    });
  });

  it("clears the error when switching back to sign in", async () => {
    await renderWithOrgs([{ name: "Solo Clinic", slug: "solo" }]);
    fireEvent.click(screen.getByRole("tab", { name: "Sign Up" }));

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "p@example.com" } });
    await screen.findByRole("option", { name: "Solo Clinic" });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() =>
      expect(screen.getByText("Password must be at least 12 characters.")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("tab", { name: "Sign In" }));
    expect(
      screen.queryByText("Password must be at least 12 characters.")
    ).not.toBeInTheDocument();
  });
});
