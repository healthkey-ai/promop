import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import * as AcceptInviteModule from './AcceptInvite';

// vi.hoisted runs before any module is imported, so the fns are available in factory closures
const { mockAxiosPost, mockAxiosGet } = vi.hoisted(() => ({
  mockAxiosPost: vi.fn(),
  mockAxiosGet: vi.fn(),
}));

vi.mock('axios', () => ({
  default: {
    create: () => ({ post: mockAxiosPost, get: mockAxiosGet }),
  },
}));

const mockNavigate = vi.fn();
const mockUseSearchParams = vi.fn();

vi.mock('react-router-dom', () => ({
  useSearchParams: () => mockUseSearchParams(),
  useNavigate: () => mockNavigate,
}));

beforeEach(() => {
  vi.clearAllMocks();
  // Best-effort lookup for org/role display.
  mockAxiosGet.mockResolvedValue({
    data: { email: 'a@b.com', org_name: 'Acme Oncology', role: 'Analyst' },
  });
});

const withToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams('token=abc123def456'), vi.fn()]);
const withoutToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams(''), vi.fn()]);

const acceptButton = () => screen.getByRole('button', { name: /accept & set password/i });
const type = async (u: ReturnType<typeof userEvent.setup>, pw: string, confirm = pw) => {
  await u.type(screen.getByLabelText('Password'), pw);
  await u.type(screen.getByLabelText(/confirm password/i), confirm);
};

describe('AcceptInvite', () => {
  it('shows error message immediately when no token is in the URL', () => {
    withoutToken();
    render(<AcceptInviteModule.default />);
    expect(screen.getByText(/No invitation token found/i)).toBeInTheDocument();
  });

  it('always shows the password fields when a token is present', () => {
    withToken();
    render(<AcceptInviteModule.default />);
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    expect(acceptButton()).toBeInTheDocument();
  });

  it('still shows the password fields when the org lookup fails', async () => {
    withToken();
    mockAxiosGet.mockRejectedValueOnce(new Error('endpoint missing'));
    render(<AcceptInviteModule.default />);
    // Field is present regardless of the lookup result.
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    await waitFor(() => expect(mockAxiosGet).toHaveBeenCalled());
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('submits the token and password to confirm-invitation on accept', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'Str0ng-pass-42');
    await user.click(acceptButton());
    await waitFor(() =>
      expect(mockAxiosPost).toHaveBeenCalledWith('/orgs/confirm-invitation/', {
        token: 'abc123def456',
        password: 'Str0ng-pass-42',
      })
    );
  });

  it('blocks submission when the password is too short and does not call the API', async () => {
    withToken();
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'short');
    await user.click(acceptButton());
    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('blocks submission when the passwords do not match', async () => {
    withToken();
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'Str0ng-pass-42', 'Different-99');
    await user.click(acceptButton());
    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('redirects to the supplied analyst site after a successful accept', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({
      data: {
        detail: 'Access granted to Acme Oncology.',
        redirect_url: 'https://analytics.healthkey.ai/custom',
      },
    });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'Str0ng-pass-42');
    await user.click(acceptButton());
    await waitFor(() => screen.getByRole('link', { name: /continue/i }));
    expect(screen.getByRole('link', { name: /continue/i })).toHaveAttribute(
      'href',
      'https://analytics.healthkey.ai/custom'
    );
  });

  it('shows success + Go to PROMOP when there is no redirect', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'Str0ng-pass-42');
    await user.click(acceptButton());
    await waitFor(() => screen.getByRole('button', { name: /go to promop/i }));
    await user.click(screen.getByRole('button', { name: /go to promop/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('shows the API error message when accept fails', async () => {
    withToken();
    mockAxiosPost.mockRejectedValueOnce({
      response: { data: { error: 'Invitation has expired.' } },
    });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await type(user, 'Str0ng-pass-42');
    await user.click(acceptButton());
    await waitFor(() =>
      expect(screen.getByText('Invitation has expired.')).toBeInTheDocument()
    );
  });

  it('navigates to /login when Back to Login is clicked from error state', async () => {
    withoutToken();
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /back to login/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });
});
