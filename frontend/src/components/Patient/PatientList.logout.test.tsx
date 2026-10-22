import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import PatientList from './PatientList';

const { logout } = vi.hoisted(() => ({ logout: vi.fn().mockResolvedValue(undefined) }));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ currentUser: { is_staff: true }, logout }),
}));
vi.mock('@/api/axios', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: { results: [], count: 0 } }) },
}));

it('ends the server session when signing out from the patient list', async () => {
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: 'Logout' }));
  expect(logout).toHaveBeenCalledOnce();
});
