import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi } from 'vitest';
import UserProfilePage from './UserProfilePage';

vi.mock('@/hooks/useAuth', () => ({
  useAuth: vi.fn(),
}));

import { useAuth } from '@/hooks/useAuth';

describe('UserProfilePage', () => {
  it('shows invited and trusted-domain organizations below the email', () => {
    vi.mocked(useAuth).mockReturnValue({
      currentUser: {
        id: 1,
        sub: 'user-1',
        email: 'user@example.com',
        name: 'User',
        org_accesses: [
          {
            org_name: 'Invited Org',
            org_slug: 'invited-org',
            role: 'doctor',
            expires_at: null,
            access_via: ['invitation'],
          },
          {
            org_name: 'Trusted Domain Org',
            org_slug: 'trusted-domain-org',
            role: null,
            expires_at: null,
            access_via: ['trusted_domain'],
          },
        ],
      },
      loading: false,
      login: vi.fn(),
      logout: vi.fn(),
      refresh: vi.fn(),
      fetchCurrentUser: vi.fn(),
    });

    render(<MemoryRouter><UserProfilePage /></MemoryRouter>);

    expect(screen.getByText('Invited Org')).toBeInTheDocument();
    expect(screen.getByText('Trusted Domain Org')).toBeInTheDocument();
    expect(screen.getByText('Invited')).toBeInTheDocument();
    expect(screen.getByText('Trusted domain')).toBeInTheDocument();
    expect(screen.getByText('Org Access').compareDocumentPosition(screen.getByText('System Rights')))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });
});
