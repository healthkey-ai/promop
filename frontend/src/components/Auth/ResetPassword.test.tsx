import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import ResetPassword from './ResetPassword';

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));
vi.mock('axios', () => ({ default: { create: () => ({ post: mockPost }) } }));

const mockNavigate = vi.fn();
const mockUseSearchParams = vi.fn();
vi.mock('react-router-dom', () => ({
  useSearchParams: () => mockUseSearchParams(),
  useNavigate: () => mockNavigate,
}));

const withLink = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams('uid=abc&token=tok-123'), vi.fn()]);
const withoutLink = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams(''), vi.fn()]);

beforeEach(() => vi.clearAllMocks());

describe('ResetPassword', () => {
  it('shows an error when the link is missing uid/token', () => {
    withoutLink();
    render(<ResetPassword />);
    expect(screen.getByText(/missing information/i)).toBeInTheDocument();
  });

  it('rejects mismatched passwords without calling the API', async () => {
    withLink();
    render(<ResetPassword />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('New password'), 'Cq2-badger-mint');
    await user.type(screen.getByLabelText('Confirm password'), 'different-one');
    await user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText(/passwords do not match/i)).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('submits uid+token+password and shows success', async () => {
    withLink();
    mockPost.mockResolvedValueOnce({ data: { detail: 'Password has been reset.' } });
    render(<ResetPassword />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('New password'), 'Cq2-badger-mint');
    await user.type(screen.getByLabelText('Confirm password'), 'Cq2-badger-mint');
    await user.click(screen.getByRole('button', { name: /reset password/i }));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith('/v1/auth/reset-password/', {
        uid: 'abc', token: 'tok-123', new_password: 'Cq2-badger-mint',
      })
    );
    expect(await screen.findByText(/password has been reset/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to sign in/i })).toBeInTheDocument();
  });

  it('shows the API error on failure', async () => {
    withLink();
    mockPost.mockRejectedValueOnce({ response: { data: { error: 'Invalid or expired reset link.' } } });
    render(<ResetPassword />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('New password'), 'Cq2-badger-mint');
    await user.type(screen.getByLabelText('Confirm password'), 'Cq2-badger-mint');
    await user.click(screen.getByRole('button', { name: /reset password/i }));
    expect(await screen.findByText('Invalid or expired reset link.')).toBeInTheDocument();
  });
});
