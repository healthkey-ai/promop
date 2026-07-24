import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';
import AcceptPatientInvite from './AcceptPatientInvite';

const { mockGet, mockPost } = vi.hoisted(() => ({ mockGet: vi.fn(), mockPost: vi.fn() }));

vi.mock('axios', () => ({
  default: { create: () => ({ get: mockGet, post: mockPost }) },
}));

const mockNavigate = vi.fn();
const mockUseSearchParams = vi.fn();
vi.mock('react-router-dom', () => ({
  useSearchParams: () => mockUseSearchParams(),
  useNavigate: () => mockNavigate,
}));

const withToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams('token=' + 'a'.repeat(64)), vi.fn()]);
const withoutToken = () =>
  mockUseSearchParams.mockReturnValue([new URLSearchParams(''), vi.fn()]);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('AcceptPatientInvite', () => {
  it('shows an error immediately when the link has no token', async () => {
    withoutToken();
    render(<AcceptPatientInvite />);
    expect(await screen.findByText(/no invitation token found/i)).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('looks up the token and prefills the invitee email', async () => {
    withToken();
    mockGet.mockResolvedValueOnce({ data: { email: 'rae@example.com', patient_name: 'Rae Reed' } });
    render(<AcceptPatientInvite />);
    expect(await screen.findByDisplayValue('rae@example.com')).toBeInTheDocument();
    expect(screen.getByText(/welcome, rae reed/i)).toBeInTheDocument();
  });

  it('shows an error when the token lookup fails', async () => {
    withToken();
    mockGet.mockRejectedValueOnce({ response: { data: { error: 'Invitation has expired.' } } });
    render(<AcceptPatientInvite />);
    expect(await screen.findByText('Invitation has expired.')).toBeInTheDocument();
  });

  it('rejects mismatched passwords without calling the API', async () => {
    withToken();
    mockGet.mockResolvedValueOnce({ data: { email: 'rae@example.com', patient_name: 'Rae Reed' } });
    render(<AcceptPatientInvite />);
    await screen.findByDisplayValue('rae@example.com');
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Password'), 'sup3rsecret');
    await user.type(screen.getByLabelText('Confirm password'), 'different1');
    await user.click(screen.getByRole('button', { name: /create account/i }));
    expect(await screen.findByText(/passwords do not match/i)).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('submits the password and shows success', async () => {
    withToken();
    mockGet.mockResolvedValueOnce({ data: { email: 'rae@example.com', patient_name: 'Rae Reed' } });
    mockPost.mockResolvedValueOnce({ data: { detail: 'Account created. You can now sign in.' } });
    render(<AcceptPatientInvite />);
    await screen.findByDisplayValue('rae@example.com');
    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Password'), 'sup3rsecret');
    await user.type(screen.getByLabelText('Confirm password'), 'sup3rsecret');
    await user.click(screen.getByRole('button', { name: /create account/i }));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith('/v1/patient-invitations/accept/', {
        token: 'a'.repeat(64),
        password: 'sup3rsecret',
      })
    );
    expect(await screen.findByText(/account created/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /go to sign in/i })).toBeInTheDocument();
  });
});
