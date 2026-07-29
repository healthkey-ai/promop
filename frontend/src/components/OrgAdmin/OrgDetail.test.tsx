import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import OrgDetail from "./OrgDetail";

// Mock api
const mockGet = vi.fn();
const mockPatch = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/api/axios", () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    patch: (...args: unknown[]) => mockPatch(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const ORG_DATA = {
  id: 1,
  name: "Acme Clinic",
  slug: "acme",
  is_active: true,
  allows_public_aggregated_data: false,
  allows_patient_signup: false,
  created_at: "2024-01-01T00:00:00Z",
};

function setupMocks(overrides: Partial<typeof ORG_DATA> = {}) {
  const orgData = { ...ORG_DATA, ...overrides };
  mockGet.mockImplementation((url: string) => {
    if (url.includes("/orgs/acme/trusts/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/invitations/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/")) return Promise.resolve({ data: orgData });
    if (url.includes("/stats/org-disease/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/")) return Promise.resolve({ data: [] });
    if (url.includes("/v1/patient-records/"))
      return Promise.resolve({
        data: [
          { person_id: 42, patient_name: "Jane Doe" },
          { person_id: 99, patient_name: "John Smith" },
        ],
      });
    return Promise.resolve({ data: {} });
  });
  mockPatch.mockResolvedValue({ data: orgData });
  mockPost.mockResolvedValue({ data: { id: 10, email: "p@test.com", role: "patient", status: "pending" } });
  return orgData;
}

function renderOrgDetail(props: Partial<React.ComponentProps<typeof OrgDetail>> = {}) {
  return render(
    <OrgDetail slug="acme" isStaff={true} onBack={vi.fn()} {...props} />
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrgDetail — Settings", () => {
  it("renders allows_patient_signup toggle", async () => {
    setupMocks({ allows_patient_signup: true });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    const toggle = screen.getByLabelText("Allow direct patient signup");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toBeChecked();
  });

  it("renders allows_patient_signup toggle unchecked when false", async () => {
    setupMocks({ allows_patient_signup: false });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    const toggle = screen.getByLabelText("Allow direct patient signup");
    expect(toggle).not.toBeChecked();
  });

  it("calls PATCH when allows_patient_signup toggle is clicked", async () => {
    setupMocks({ allows_patient_signup: false });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByLabelText("Allow direct patient signup"));
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith("/orgs/acme/", { allows_patient_signup: true });
    });
  });
});

describe("OrgDetail — Invite form", () => {
  it("shows patient option in role dropdown", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    // Switch to Access Grants tab
    fireEvent.click(screen.getByText("Access Grants"));
    const select = screen.getByDisplayValue("Doctor");
    const options = select.querySelectorAll("option");
    const optionValues = Array.from(options).map((o) => o.getAttribute("value"));
    expect(optionValues).toContain("patient");
  });

  it("shows patient search field when patient role is selected", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Access Grants"));
    const roleSelect = screen.getByDisplayValue("Doctor");
    fireEvent.change(roleSelect, { target: { value: "patient" } });
    expect(screen.getByPlaceholderText("Search by name or ID...")).toBeInTheDocument();
    expect(screen.getByText(/Link to patient record/)).toBeInTheDocument();
  });

  it("searches patients and shows results", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Access Grants"));
    const roleSelect = screen.getByDisplayValue("Doctor");
    fireEvent.change(roleSelect, { target: { value: "patient" } });

    const searchInput = screen.getByPlaceholderText("Search by name or ID...");
    fireEvent.change(searchInput, { target: { value: "Jane" } });

    await waitFor(() => {
      expect(screen.getByText(/Jane Doe/)).toBeInTheDocument();
    });
  });

  it("sends person_id when patient invite has linked record", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Access Grants"));
    const roleSelect = screen.getByDisplayValue("Doctor");
    fireEvent.change(roleSelect, { target: { value: "patient" } });

    // Search and select a patient
    const searchInput = screen.getByPlaceholderText("Search by name or ID...");
    fireEvent.change(searchInput, { target: { value: "Jane" } });

    await waitFor(() => {
      expect(screen.getByText(/Jane Doe/)).toBeInTheDocument();
    });

    // Click the result to select
    fireEvent.click(screen.getByText(/Jane Doe/));

    // Fill email and send
    fireEvent.change(screen.getByPlaceholderText("Email address"), { target: { value: "jane@test.com" } });
    fireEvent.click(screen.getByText("Send Invite"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/orgs/acme/invite/", {
        email: "jane@test.com",
        role: "patient",
        person_id: 42,
      });
    });
  });

  it("hides patient search when switching away from patient role", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Access Grants"));
    const roleSelect = screen.getByDisplayValue("Doctor");
    fireEvent.change(roleSelect, { target: { value: "patient" } });
    expect(screen.getByPlaceholderText("Search by name or ID...")).toBeInTheDocument();

    fireEvent.change(roleSelect, { target: { value: "doctor" } });
    expect(screen.queryByPlaceholderText("Search by name or ID...")).not.toBeInTheDocument();
  });
});

describe("OrgDetail — Invitations list", () => {
  it("shows person_id for patient invitations", async () => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/orgs/acme/invitations/"))
        return Promise.resolve({
          data: [
            { id: 1, org_slug: "acme", email: "patient@test.com", role: "patient", person_id: 42, status: "pending", redirect_url: null, expires_at: "2099-01-01", created_at: "2024-01-01" },
            { id: 2, org_slug: "acme", email: "doc@test.com", role: "doctor", person_id: null, status: "confirmed", redirect_url: null, expires_at: "2099-01-01", created_at: "2024-01-01" },
          ],
        });
      if (url.includes("/orgs/acme/trusts/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/acme/")) return Promise.resolve({ data: ORG_DATA });
      if (url.includes("/stats/org-disease/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/")) return Promise.resolve({ data: [] });
      return Promise.resolve({ data: {} });
    });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Invitations"));
    await waitFor(() => {
      expect(screen.getByText("patient@test.com")).toBeInTheDocument();
    });
    // Patient invite shows linked person_id
    expect(screen.getByText("#42")).toBeInTheDocument();
    // Doctor invite does not show person_id
    expect(screen.queryByText("#null")).not.toBeInTheDocument();
  });
});
