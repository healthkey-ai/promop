import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ServiceApplicationsPage from './ServiceApplicationsPage';
const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), staff: true }));
vi.mock('@/api/axios', () => ({ default: mocks }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ currentUser: { is_staff: mocks.staff } }) }));
const app = { id: 1, name: 'HK-Labs', service_id: 'hk-labs', owner_contact: 'Vlad', description: '',
  scopes: 'patient/*.read', is_active: true,
  tokens: [{ id: 7, label: 'Initial token', suffix: 'abcd', created_at: '2026-09-12T10:00:00Z',
    expires_at: null, last_used_at: null, revoked_at: null }] };
function renderPage() { return render(<MemoryRouter><ServiceApplicationsPage /></MemoryRouter>); }
async function selectApp() { fireEvent.click(await screen.findByRole('button', { name: /HK-Labs/ })); }
beforeEach(() => {
  vi.clearAllMocks(); mocks.staff = true;
  mocks.get.mockResolvedValue({ data: [app] }); mocks.patch.mockResolvedValue({ data: app });
  mocks.post.mockResolvedValue({ data: { id: 8, token: 'newly-generated-test-secret' } });
});
describe('Service applications', () => {
  it('only fetches and renders administration for staff', () => {
    mocks.staff = false; renderPage();
    expect(screen.getByText('Staff access is required.')).toBeInTheDocument();
    expect(mocks.get).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'New application' })).not.toBeInTheDocument();
  });
  it('edits app metadata without changing the service principal', async () => {
    renderPage(); await selectApp(); expect(screen.getByLabelText('Service ID')).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Application name'), { target: { value: 'Lab integration' } });
    fireEvent.change(screen.getByLabelText('Owner / contact'), { target: { value: 'Vlad updated' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save application' }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalledWith('/v1/service-applications/1/',
      expect.objectContaining({ name: 'Lab integration', owner_contact: 'Vlad updated', service_id: 'hk-labs' })));
  });
  it('shows a newly issued secret once and removes it when dismissed', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem'); renderPage(); await selectApp();
    expect(screen.queryByLabelText('New service token')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Token label'), { target: { value: 'Replacement' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }));
    expect(await screen.findByLabelText('New service token')).toHaveValue('newly-generated-test-secret');
    expect(mocks.post).toHaveBeenCalledWith('/v1/service-applications/1/tokens/', { label: 'Replacement', expires_at: null });
    expect(storage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss token' }));
    expect(screen.queryByDisplayValue('newly-generated-test-secret')).not.toBeInTheDocument(); storage.mockRestore();
  });
  it('shows the bound the server refused rather than a generic failure', async () => {
    mocks.post.mockRejectedValueOnce({ response: { data: {
      expires_at: ['Expiration cannot be more than 365 days away.'] } } });
    renderPage(); await selectApp();
    fireEvent.change(screen.getByLabelText(/Token label/), { target: { value: 'Too long' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }));
    expect(await screen.findByText('Expiration cannot be more than 365 days away.')).toBeInTheDocument();
  });
  it('falls back to a readable message when the server says nothing specific', async () => {
    mocks.post.mockRejectedValueOnce(new Error('network'));
    renderPage(); await selectApp();
    fireEvent.change(screen.getByLabelText(/Token label/), { target: { value: 'Whatever' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }));
    expect(await screen.findByText(/Could not create the token/)).toBeInTheDocument();
  });
  it('warns about a token approaching its expiry, where the operator already looks', async () => {
    const soon = new Date(Date.now() + 9 * 24 * 60 * 60 * 1000).toISOString();
    mocks.get.mockResolvedValue({ data: [{ ...app, tokens: [{ ...app.tokens[0], expires_at: soon }] }] });
    renderPage(); await selectApp();
    expect(await screen.findByText(/expires in 9 days — rotate it/)).toBeInTheDocument();
  });
  it('requires explicit confirmation to revoke a specific token', async () => {
    renderPage(); await selectApp(); fireEvent.click(screen.getByRole('button', { name: 'Revoke Initial token' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument(); expect(mocks.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm revocation' }));
    await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/v1/service-applications/1/tokens/7/revoke/'));
  });
});
