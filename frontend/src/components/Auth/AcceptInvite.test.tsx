import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import AcceptInvite from './AcceptInvite';

// vi.hoisted runs before any module is imported, so the fn is available in factory closures
const { mockAxiosPost } = vi.hoisted(() => ({ mockAxiosPost: vi.fn() }));

vi.mock('axios', () => ({
  default: {
    create: () => ({ post: mockAxiosPost }),
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
});

const withToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams('token=abc123def456'), vi.fn()]);
const withoutToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams(''), vi.fn()]);

describe('AcceptInvite', () => {
  it('shows error message immediately when no token is in the URL', () => {
    withoutToken();
    render(<AcceptInvite />);
    expect(screen.getByText(/No invitation token found/i)).toBeInTheDocument();
  });

  it('shows the Accept button when a token is present', () => {
    withToken();
    render(<AcceptInvite />);
    expect(screen.getByRole('button', { name: /accept invitation/i })).toBeInTheDocument();
  });

  it('calls the confirm-invitation endpoint with the token on accept', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() =>
      expect(mockAxiosPost).toHaveBeenCalledWith('/orgs/confirm-invitation/', { token: 'abc123def456' })
    );
  });

  it('shows success message and Go to PROMOP button after a successful accept', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Access granted to Acme Oncology.' } });
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() =>
      expect(screen.getByText('Access granted to Acme Oncology.')).toBeInTheDocument()
    );
    expect(screen.getByRole('button', { name: /go to promop/i })).toBeInTheDocument();
  });

  it('navigates to / when Go to PROMOP is clicked after success', async () => {
    withToken();
    mockAxiosPost.mockResolvedValueOnce({ data: { detail: 'Done.' } });
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() => screen.getByRole('button', { name: /go to promop/i }));
    await user.click(screen.getByRole('button', { name: /go to promop/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/');
  });

  it('shows the API error message when accept fails', async () => {
    withToken();
    mockAxiosPost.mockRejectedValueOnce({
      response: { data: { error: 'Invitation has expired.' } },
    });
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() =>
      expect(screen.getByText('Invitation has expired.')).toBeInTheDocument()
    );
  });

  it('shows a fallback error message when the response has no error field', async () => {
    withToken();
    mockAxiosPost.mockRejectedValueOnce(new Error('Network Error'));
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /accept invitation/i }));
    await waitFor(() =>
      expect(screen.getByText(/failed to accept invitation/i)).toBeInTheDocument()
    );
  });

  it('navigates to /login when Back to Login is clicked from error state', async () => {
    withoutToken();
    render(<AcceptInvite />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /back to login/i }));
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });
});
