import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import OrgDetail from "./OrgDetail";
import { MemoryRouter } from "react-router-dom";

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
  can_manage_access: true,
  created_at: "2024-01-01T00:00:00Z",
};

const VOCABULARY_DATA = {
  org_slug: "acme",
  org_name: "Acme Clinic",
  concept_count: 2,
  concepts: [
    {
      concept_id: 9800001,
      vocabulary_id: "SNOMED",
      concept_code: "254837009",
      concept_name: "Breast cancer",
      domain_id: "Condition",
      standard_concept: "S",
      source: null,
      group: "athena",
      group_label: "Athena / external",
      group_order: 0,
      patient_count: 2,
      instance_count: 3,
      usage: [
        {
          table: "condition_occurrence",
          column: "condition_concept_id",
          role: "concept",
          instance_count: 3,
        },
      ],
    },
    {
      concept_id: 2000000101,
      vocabulary_id: "HK-Wearable",
      concept_code: "HK-WEAR-STRIDE",
      concept_name: "HealthKey wearable stride",
      domain_id: "Measurement",
      standard_concept: "S",
      source: "HealthKey",
      group: "healthkey",
      group_label: "HealthKey: HK-Wearable",
      group_order: 1,
      patient_count: 1,
      instance_count: 1,
      usage: [
        {
          table: "measurement",
          column: "measurement_source_concept_id",
          role: "source_concept",
          instance_count: 1,
        },
      ],
    },
  ],
};

function setupMocks(overrides: Partial<typeof ORG_DATA> = {}) {
  const orgData = { ...ORG_DATA, ...overrides };
  mockGet.mockImplementation((url: string) => {
    if (url.includes("/orgs/acme/trusts/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/invitations/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: [] });
    if (url.includes("/orgs/acme/vocabulary/")) return Promise.resolve({ data: VOCABULARY_DATA });
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
    <MemoryRouter><OrgDetail slug="acme" isStaff={true} onBack={vi.fn()} {...props} /></MemoryRouter>
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
    const toggle = screen.getByLabelText("Allow public demo access");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toBeChecked();
  });

  it("renders allows_patient_signup toggle unchecked when false", async () => {
    setupMocks({ allows_patient_signup: false });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    const toggle = screen.getByLabelText("Allow public demo access");
    expect(toggle).not.toBeChecked();
  });

  it("calls PATCH when allows_patient_signup toggle is clicked", async () => {
    setupMocks({ allows_patient_signup: false });
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByLabelText("Allow public demo access"));
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

describe("OrgDetail — Vocabulary", () => {
  it("loads and renders organization vocabulary usage", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Vocabulary"));

    await waitFor(() => {
      expect(screen.getByText("Breast cancer")).toBeInTheDocument();
    });
    expect(mockGet).toHaveBeenCalledWith("/orgs/acme/vocabulary/");
    expect(screen.getByText("SNOMED")).toBeInTheDocument();
    expect(screen.getByText("HK-Wearable")).toBeInTheDocument();
    expect(screen.getByText(/condition_occurrence/)).toBeInTheDocument();
    expect(screen.getByText(/measurement/)).toBeInTheDocument();
  });

  it("filters vocabulary rows by search text", async () => {
    setupMocks();
    renderOrgDetail();
    await waitFor(() => {
      expect(screen.getByText("Acme Clinic")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Vocabulary"));
    await waitFor(() => {
      expect(screen.getByText("Breast cancer")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByPlaceholderText("Search vocabulary, code, name..."), {
      target: { value: "wearable" },
    });

    expect(screen.queryByText("Breast cancer")).not.toBeInTheDocument();
    expect(screen.getByText("HealthKey wearable stride")).toBeInTheDocument();
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


describe('OrgDetail access administration permissions', () => {
  it.each([false, undefined])('hides access administration and skips restricted requests when permission is %s', async (canManageAccess) => {
    setupMocks({ can_manage_access: canManageAccess });
    renderOrgDetail({ isStaff: false });
    await screen.findByText('Acme Clinic');
    for (const label of ['Access Rules', 'Access Grants', 'Invitations']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    for (const resource of ['trusts', 'access', 'invitations']) {
      expect(mockGet).not.toHaveBeenCalledWith(`/orgs/acme/${resource}/`);
    }
    expect(screen.queryByRole('button', { name: /send invite/i })).not.toBeInTheDocument();
  });
});

describe("role change into a role the user already holds (#2416)", () => {
  it("shows the server's instruction instead of a generic retry message", async () => {
    setupMocks();
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: [
        { id: 7, email: "both@test.com", name: "", is_premium: false, org_slug: "acme", group_name: null,
          role: "doctor", redirect_url: null, expires_at: null, granted_at: "2024-01-01" },
      ] });
      if (url.includes("/orgs/acme/trusts/") || url.includes("/orgs/acme/invitations/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/acme/vocabulary/")) return Promise.resolve({ data: VOCABULARY_DATA });
      if (url.includes("/orgs/acme/")) return Promise.resolve({ data: ORG_DATA });
      return Promise.resolve({ data: [] });
    });
    mockPatch.mockRejectedValueOnce({ response: { status: 409, data: {
      error: "This user already holds the analyst role here; revoke this grant instead.",
    } } });
    renderOrgDetail();
    await waitFor(() => { expect(screen.getByText("Acme Clinic")).toBeInTheDocument(); });
    fireEvent.click(screen.getByText("Access Grants"));
    // The invite form's role select also reads "Doctor"; the grant row's is in the table.
    const select = (await screen.findAllByDisplayValue("Doctor")).find((el) => el.closest("table"));
    expect(select).toBeDefined();
    fireEvent.change(select!, { target: { value: "analyst" } });
    expect(await screen.findByText(/already holds the analyst role here; revoke this grant instead/)).toBeInTheDocument();
  });
});

describe("adding a second role on the Access Grants tab (#2457)", () => {
  const grant = (id: number, email: string, role: string) => ({
    id, email, name: "", is_premium: false, org_slug: "acme", group_name: null,
    role, redirect_url: null, expires_at: null, granted_at: "2024-01-01",
  });
  const mockGrants = (grants: ReturnType<typeof grant>[]) => {
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: grants });
      if (url.includes("/orgs/acme/trusts/") || url.includes("/orgs/acme/invitations/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/acme/vocabulary/")) return Promise.resolve({ data: VOCABULARY_DATA });
      if (url.includes("/orgs/acme/")) return Promise.resolve({ data: ORG_DATA });
      return Promise.resolve({ data: [] });
    });
  };

  it("offers a doctor the analyst role and adds the row", async () => {
    setupMocks();
    mockGrants([grant(7, "doc@test.com", "doctor"), grant(8, "boss@test.com", "org_admin")]);
    mockPost.mockResolvedValueOnce({ data: grant(9, "doc@test.com", "analyst") });
    renderOrgDetail();
    await screen.findByText("Acme Clinic");
    fireEvent.click(screen.getByText("Access Grants"));
    const add = await screen.findByRole("button", { name: /also grant the analyst role to doc@test.com/i });
    // An org admin needs no other role, so its row offers none.
    expect(screen.queryByRole("button", { name: /boss@test.com/i })).not.toBeInTheDocument();
    fireEvent.click(add);
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/orgs/acme/access/", { email: "doc@test.com", role: "analyst" });
    });
    // Both rows are held now, so neither offers an addition.
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /also grant/i })).not.toBeInTheDocument();
    });
    expect(screen.getAllByText("doc@test.com")).toHaveLength(2);
  });

  it("shows the server's message when the add is refused", async () => {
    setupMocks();
    mockGrants([grant(7, "doc@test.com", "doctor")]);
    mockPost.mockRejectedValueOnce({ response: { status: 409, data: { error: "This user already holds the analyst role here." } } });
    renderOrgDetail();
    await screen.findByText("Acme Clinic");
    fireEvent.click(screen.getByText("Access Grants"));
    fireEvent.click(await screen.findByRole("button", { name: /also grant the analyst role/i }));
    expect(await screen.findByText(/already holds the analyst role here/)).toBeInTheDocument();
  });
});

describe("roles reference (#2473)", () => {
  it("links the Access Grants tab to the roles page and explains each shown role", async () => {
    setupMocks();
    mockGet.mockImplementation((url: string) => {
      if (url.includes("/orgs/acme/access/")) return Promise.resolve({ data: [
        { id: 7, email: "doc@test.com", name: "", is_premium: false, org_slug: "acme", group_name: null,
          role: "doctor", redirect_url: null, expires_at: null, granted_at: "2024-01-01" },
        { id: 8, email: "boss@test.com", name: "", is_premium: false, org_slug: "acme", group_name: null,
          role: "org_admin", redirect_url: null, expires_at: null, granted_at: "2024-01-01" },
      ] });
      if (url.includes("/orgs/acme/trusts/") || url.includes("/orgs/acme/invitations/")) return Promise.resolve({ data: [] });
      if (url.includes("/orgs/acme/vocabulary/")) return Promise.resolve({ data: VOCABULARY_DATA });
      if (url.includes("/orgs/acme/")) return Promise.resolve({ data: ORG_DATA });
      return Promise.resolve({ data: [] });
    });
    renderOrgDetail();
    await screen.findByText("Acme Clinic");
    fireEvent.click(screen.getByText("Access Grants"));
    const links = await screen.findAllByRole("link", { name: "What each role can do" });
    expect(links.length).toBeGreaterThanOrEqual(2);
    expect(links[0]).toHaveAttribute("href", "/roles");
    // The invite form's role select also says "Org Admin"; the grant row's label carries the tooltip.
    const badge = screen.getAllByText("Org Admin").find((el) => el.closest("table"))!;
    fireEvent.mouseEnter(badge);
    expect(screen.getByRole("tooltip")).toHaveTextContent(/Includes what Doctor and Analyst can do/);
    // Beside the Doctor select only the info link, not a second "Doctor".
    expect(screen.getByRole("link", { name: "What the Doctor role can do" })).toHaveAttribute("href", "/roles");
    expect(screen.getAllByText("Doctor").every((el) => el.tagName === "OPTION")).toBe(true);
  });
});
