import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import UserProfilePage from './UserProfilePage';
import { useAuth, type User } from '@/hooks/useAuth';

vi.mock('@/hooks/useAuth', () => ({ useAuth: vi.fn() }));

function showProfile(overrides: Partial<User>) {
  vi.mocked(useAuth).mockReturnValue({
    currentUser: { id: 1, sub: 'user-1', email: 'user@example.com', name: 'User', ...overrides },
    loading: false, login: vi.fn(), logout: vi.fn(), refresh: vi.fn(), fetchCurrentUser: vi.fn(),
  });
  render(<MemoryRouter><UserProfilePage /></MemoryRouter>);
}

describe('UserProfilePage', () => {
  it('shows concurrent patient, organization and group roles with their sources', () => {
    showProfile({ effective_roles: [
      { role: 'patient', scope: 'patient', person_id: 5552, source: 'patient_link', expires_at: null },
      { role: 'org_admin', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
      { role: 'doctor', scope: 'group', org_name: 'Clinic', group_name: 'Study group', source: 'group_grant', expires_at: null },
    ] });
    const roles = within(screen.getByRole('region', { name: 'Active roles' }));
    expect(roles.getByText('Patient')).toBeInTheDocument();
    expect(roles.getByText('Own patient record #5552')).toBeInTheDocument();
    expect(roles.getByText('Org Admin')).toBeInTheDocument();
    expect(roles.getByText('Organization grant')).toBeInTheDocument();
    expect(roles.getByText('Doctor')).toBeInTheDocument();
    expect(roles.getByText('Clinic — Study group')).toBeInTheDocument();
  });

  it('explains organization and email-domain admin trust without showing staff', () => {
    showProfile({ effective_roles: [
      { role: 'org_admin', scope: 'organization', org_name: 'Granting Hospital', source: 'organization_trust', source_org_name: 'Trusted Clinic', expires_at: null },
      { role: 'org_admin', scope: 'organization', org_name: 'Another Hospital', source: 'domain_trust', source_domain: 'example.com', expires_at: null },
    ] });
    expect(screen.getByText('Granting Hospital')).toBeInTheDocument();
    expect(screen.getByText('Organization trust from Trusted Clinic')).toBeInTheDocument();
    expect(screen.getByText('Email-domain trust for @example.com')).toBeInTheDocument();
    expect(screen.queryByText('Staff')).not.toBeInTheDocument();
  });

  it('keeps invitations and verified patient delegation apart from active roles', () => {
    showProfile({ effective_roles: [], patient_delegations: [{ person_id: 123, relationship: 'guardian' }],
      org_accesses: [{ org_name: 'Pending Clinic', org_slug: 'pending', role: null, pending_role: 'org_admin', expires_at: '2099-01-01T00:00:00Z', access_via: ['invitation_pending'] }],
    });
    expect(screen.getByText('No active application roles.')).toBeInTheDocument();
    expect(screen.getByText('Patient record #123 — verified guardian')).toBeInTheDocument();
    expect(screen.getByText(/Invitation pending \(Org Admin\); no role granted yet/)).toBeInTheDocument();
  });

  it('shows operational superusers as staff, without a superuser application role', () => {
    showProfile({ is_staff: true, is_superuser: true, effective_roles: [
      { role: 'staff', scope: 'platform', source: 'staff_flag', expires_at: null },
    ] });
    expect(screen.getByText('Staff')).toBeInTheDocument();
    expect(screen.getByText('All organizations')).toBeInTheDocument();
    expect(screen.queryByText(/superuser/i)).not.toBeInTheDocument();
  });

  it('explains each role with a link to the roles page, staff excepted (#2473)', () => {
    showProfile({ effective_roles: [
      { role: 'staff', scope: 'platform', source: 'staff_flag', expires_at: null },
      { role: 'doctor', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
    ] });
    const roles = within(screen.getByRole('region', { name: 'Active roles' }));
    expect(roles.getByText('Staff')).toBeInTheDocument();
    expect(roles.getByRole('link', { name: 'What the Doctor role can do' })).toHaveAttribute('href', '/roles');
    expect(roles.queryByRole('link', { name: /Staff/ })).not.toBeInTheDocument();
  });
  it('links the Patient role to the own record of an account that is also a professional (#2838)', () => {
    showProfile({ is_org_admin: true, is_patient: false, person_id: null, effective_roles: [
      { role: 'patient', scope: 'patient', person_id: 5552, source: 'patient_link', expires_at: null },
      { role: 'patient', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
      { role: 'org_admin', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
    ] });
    const roles = within(screen.getByRole('region', { name: 'Active roles' }));
    expect(roles.getByRole('link', { name: 'Own patient record #5552' })).toHaveAttribute('href', '/patient/5552');
    expect(roles.getByRole('link', { name: 'Patient membership in Clinic (own record only)' })).toHaveAttribute('href', '/patient/5552');
    // Only the Patient role is a link to a record.
    expect(roles.getByText('Clinic').closest('a')).toBeNull();
  });

  it("links a patient-only account's Patient role to its home, which is its record (#2838)", () => {
    showProfile({ is_patient: true, person_id: 77, effective_roles: [
      { role: 'patient', scope: 'patient', person_id: 77, source: 'patient_link', expires_at: null },
    ] });
    expect(screen.getByRole('link', { name: 'Own patient record #77' })).toHaveAttribute('href', '/');
  });

  it('leaves the Patient role plain where the account cannot open a record (#2838)', () => {
    // An analyst-only account is turned away from the patient pages, its own included.
    showProfile({ is_patient: false, person_id: null,
      org_accesses: [{ org_name: 'Clinic', org_slug: 'clinic', role: 'analyst', expires_at: null }],
      effective_roles: [
        { role: 'patient', scope: 'patient', person_id: 9, source: 'patient_link', expires_at: null },
        { role: 'analyst', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
      ] });
    expect(screen.getByText('Own patient record #9').closest('a')).toBeNull();
    // A patient membership with no linked record names no record to open.
    cleanup();
    showProfile({ effective_roles: [
      { role: 'patient', scope: 'organization', org_name: 'Clinic', source: 'org_grant', expires_at: null },
    ] });
    expect(screen.getByText('Patient membership in Clinic (own record only)').closest('a')).toBeNull();
  });
});
