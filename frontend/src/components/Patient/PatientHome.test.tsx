import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect } from "vitest";
import PatientHome from "./PatientHome";
import type { User } from "@/hooks/useAuth";

// Capture the props PatientHome hands to PatientDetail.
vi.mock("@/components/Patient/PatientDetail", () => ({
  default: (props: { personIdOverride?: string; patientMode?: boolean }) => (
    <div>{`PATIENT_DETAIL:${props.personIdOverride}:${String(props.patientMode)}`}</div>
  ),
}));

const makeUser = (overrides: Partial<User>): User => ({
  id: 1,
  sub: "sub",
  email: "p@test.com",
  name: "Pat",
  ...overrides,
});

describe("PatientHome", () => {
  it("renders the patient's own record via PatientDetail in patient mode", () => {
    render(
      <PatientHome
        user={makeUser({ is_patient: true, person_id: 7 })}
        onLogout={vi.fn()}
      />
    );
    expect(screen.getByText("PATIENT_DETAIL:7:true")).toBeInTheDocument();
  });

  it("shows a no-record message when no person is linked", () => {
    render(<PatientHome user={makeUser({ is_patient: true, person_id: null })} onLogout={vi.fn()} />);
    expect(screen.getByText(/no health record is linked/i)).toBeInTheDocument();
    expect(screen.queryByText(/PATIENT_DETAIL/)).not.toBeInTheDocument();
  });

  it("shows a no-record message when user is null", () => {
    render(<PatientHome user={null} onLogout={vi.fn()} />);
    expect(screen.getByText(/no health record is linked/i)).toBeInTheDocument();
  });
});
