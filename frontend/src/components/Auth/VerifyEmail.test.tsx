import { MemoryRouter } from 'react-router-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import VerifyEmail from './VerifyEmail';
import VerifyEmailBanner from './VerifyEmailBanner';

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));
vi.mock('axios', () => ({
  default: {
    create: () => ({
      post: mockPost,
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    }),
  },
}));

const page = (search: string) =>
  render(
    <MemoryRouter initialEntries={[`/verify-email${search}`]}>
      <VerifyEmail />
    </MemoryRouter>,
  );

beforeEach(() => vi.clearAllMocks());

describe('VerifyEmail', () => {
  it('posts the token from the link and confirms', async () => {
    mockPost.mockResolvedValue({ data: { detail: 'Your email address is confirmed.' } });
    page('?token=signed-value');
    expect(await screen.findByText('Your email address is confirmed.')).toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledWith('/v1/auth/verify-email/', { token: 'signed-value' });
    expect(screen.getByRole('link', { name: /continue/i })).toHaveAttribute('href', '/');
  });

  it('shows the server error for a bad or expired link', async () => {
    mockPost.mockRejectedValue({ response: { data: { error: 'This verification link is invalid or has expired.' } } });
    page('?token=stale');
    expect(await screen.findByText(/invalid or has expired/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /continue/i })).not.toBeInTheDocument();
  });

  it('does not call the API when the link has no token', () => {
    page('');
    expect(screen.getByText(/missing information/i)).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });
});

describe('VerifyEmailBanner', () => {
  it('names the address and resends on request, once', async () => {
    mockPost.mockResolvedValue({ data: {} });
    render(<VerifyEmailBanner email="user@example.com" />);
    expect(screen.getByRole('status')).toHaveTextContent('user@example.com');
    await userEvent.setup().click(screen.getByRole('button', { name: 'Resend email' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/v1/auth/verify-email/resend/'));
    expect(await screen.findByRole('button', { name: 'Email sent' })).toBeDisabled();
  });

  it('says so when the resend fails, and lets the user try again', async () => {
    mockPost.mockRejectedValue(new Error('503'));
    render(<VerifyEmailBanner />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Resend email' }));
    expect(await screen.findByText(/could not send/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resend email' })).toBeEnabled();
  });
});
