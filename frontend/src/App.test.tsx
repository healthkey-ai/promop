import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import App from "./App";

// Controllable useAuth mock.
const { mockUseAuth } = vi.hoisted(() => ({ mockUseAuth: vi.fn() }));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => mockUseAuth() }));

// Replace route targets with identifiable markers so we assert on routing only.
vi.mock("@/components/Patient/PatientList", () => ({ default: () => <div>PROVIDER_LIST</div> }));
vi.mock("@/components/Patient/PatientHome", () => ({ default: () => <div>PATIENT_HOME</div> }));
vi.mock("@/components/Patient/PatientDetail", () => ({ default: () => <div>PATIENT_DETAIL</div> }));
vi.mock("@/components/Patient/UploadFHIR", () => ({ default: () => <div>UPLOAD_FHIR</div> }));
vi.mock("@/components/Patient/UploadCSV", () => ({ default: () => <div>UPLOAD_CSV</div> }));
vi.mock("@/components/OrgAdmin/OrgAdminPage", () => ({ default: () => <div>ORG_ADMIN</div> }));
vi.mock("@/components/User/UserProfilePage", () => ({ default: () => <div>USER_PROFILE</div> }));
vi.mock("@/components/Auth/Login", () => ({ Login: () => <div>LOGIN</div> }));
vi.mock("@/components/Auth/AuthCallback", () => ({ AuthCallback: () => <div>AUTH_CALLBACK</div> }));
vi.mock("@/components/Auth/AcceptInvite", () => ({ default: () => <div>ACCEPT_INVITE</div> }));
vi.mock("@/components/Auth/AcceptPatientInvite", () => ({ default: () => <div>ACCEPT_PATIENT_INVITE</div> }));
vi.mock("@/components/Auth/ResetPassword", () => ({ default: () => <div>RESET_PASSWORD</div> }));

const baseAuth = { loading: false, refresh: vi.fn(), logout: vi.fn(), login: vi.fn() };

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("App role-gated routing", () => {
  it("routes a patient to PatientHome at /", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: { id: 1, is_patient: true, person_id: 5 } });
    renderAt("/");
    expect(screen.getByText("PATIENT_HOME")).toBeInTheDocument();
    expect(screen.queryByText("PROVIDER_LIST")).not.toBeInTheDocument();
  });

  it("routes a provider to the provider patient list at /", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: { id: 2, is_org_admin: true } });
    renderAt("/");
    expect(screen.getByText("PROVIDER_LIST")).toBeInTheDocument();
    expect(screen.queryByText("PATIENT_HOME")).not.toBeInTheDocument();
  });

  it("redirects a logged-out user to login at /", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: null });
    renderAt("/");
    expect(screen.getByText("LOGIN")).toBeInTheDocument();
  });

  it("blocks a patient from a provider-only route (org-admin -> home)", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: { id: 1, is_patient: true, person_id: 5 } });
    renderAt("/org-admin");
    expect(screen.getByText("PATIENT_HOME")).toBeInTheDocument();
    expect(screen.queryByText("ORG_ADMIN")).not.toBeInTheDocument();
  });

  it("blocks a patient from the provider patient-detail route", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: { id: 1, is_patient: true, person_id: 5 } });
    renderAt("/patient/999");
    expect(screen.getByText("PATIENT_HOME")).toBeInTheDocument();
    expect(screen.queryByText("PATIENT_DETAIL")).not.toBeInTheDocument();
  });

  it("allows a provider on a provider-only route (org-admin)", () => {
    mockUseAuth.mockReturnValue({ ...baseAuth, currentUser: { id: 2, is_org_admin: true } });
    renderAt("/org-admin");
    expect(screen.getByText("ORG_ADMIN")).toBeInTheDocument();
  });
});
