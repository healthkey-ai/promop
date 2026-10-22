import { render, screen, within } from '@testing-library/react';
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
});
