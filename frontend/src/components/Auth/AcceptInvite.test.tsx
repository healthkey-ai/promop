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

// Default lookup: an existing account (no password step) so the plain accept-flow
// tests reach the Accept button directly.
const lookup = (over = {}) =>
  mockAxiosGet.mockResolvedValue({
    data: { email: 'a@b.com', org_name: 'Acme Oncology', role: 'Analyst', needs_password: false, ...over },
  });

beforeEach(() => {
  vi.clearAllMocks();
  lookup();
});

const withToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams('token=abc123def456'), vi.fn()]);
const withoutToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams(''), vi.fn()]);

const acceptButton = () => screen.findByRole('button', { name: /accept invitation/i });

describe('AcceptInvite', () => {
  it('shows error message immediately when no token is in the URL', () => {
    withoutToken();
    render(<AcceptInviteModule.default />);
    expect(screen.getByText(/No invitation token found/i)).toBeInTheDocument();
  });

  it('shows the Accept button (with org/role) after the invitation loads', async () => {
    withToken();
    render(<AcceptInviteModule.default />);
    expect(await acceptButton()).toBeInTheDocument();
    expect(screen.getByText(/Acme Oncology/)).toBeInTheDocument();
  });

  it('shows an error when the invitation lookup fails', async () => {
    withToken();
    mockAxiosGet.mockRejectedValueOnce({ response: { data: { error: 'Invitation has expired.' } } });
    render(<AcceptInviteModule.default />);
    await waitFor(() => expect(screen.getByText('Invitation has expired.')).toBeInTheDocument());
  });

  it('calls confirm-invitation with just the token when no password is needed', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await user.click(await acceptButton());
    await waitFor(() =>
      expect(mockAxiosPost).toHaveBeenCalledWith('/orgs/confirm-invitation/', { token: 'abc123def456' })
    );
  });

  it('collects and submits a password when the invite needs one', async () => {
    withToken();
    lookup({ needs_password: true });
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await acceptButton();
    await user.type(screen.getByLabelText('Password'), 'Str0ng-pass-42');
    await user.type(screen.getByLabelText(/confirm password/i), 'Str0ng-pass-42');
    await user.click(await acceptButton());
    await waitFor(() =>
      expect(mockAxiosPost).toHaveBeenCalledWith('/orgs/confirm-invitation/', {
        token: 'abc123def456',
        password: 'Str0ng-pass-42',
      })
    );
  });

  it('blocks submission when the password is too short and does not call the API', async () => {
    withToken();
    lookup({ needs_password: true });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await acceptButton();
    await user.type(screen.getByLabelText('Password'), 'short');
    await user.type(screen.getByLabelText(/confirm password/i), 'short');
    await user.click(await acceptButton());
    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('blocks submission when the passwords do not match', async () => {
    withToken();
    lookup({ needs_password: true });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await acceptButton();
    await user.type(screen.getByLabelText('Password'), 'Str0ng-pass-42');
    await user.type(screen.getByLabelText(/confirm password/i), 'Different-99');
    await user.click(await acceptButton());
    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('shows success message and Go to PROMOP button after a successful accept', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Access granted to Acme Oncology.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await user.click(await acceptButton());
    await waitFor(() =>
      expect(screen.getByText('Access granted to Acme Oncology.')).toBeInTheDocument()
    );
    expect(screen.getByRole('button', { name: /go to promop/i })).toBeInTheDocument();
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
    await user.click(await acceptButton());
    await waitFor(() => screen.getByRole('link', { name: /continue/i }));
    expect(screen.getByRole('link', { name: /continue/i })).toHaveAttribute(
      'href',
      'https://analytics.healthkey.ai/custom'
    );
  });

  it('navigates to / when Go to PROMOP is clicked after success', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInviteModule.default />);
    const user = userEvent.setup();
    await user.click(await acceptButton());
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
    await user.click(await acceptButton());
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
