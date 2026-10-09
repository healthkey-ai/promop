import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, it, expect, beforeEach } from 'vitest';
import api from '@/api/axios';
import UploadFHIR from './UploadFHIR';

vi.mock('@/api/axios', () => ({ default: { get: vi.fn(), post: vi.fn() } }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ currentUser: { is_org_admin: true } }) }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ data: [{ slug: 'clinic', name: 'Clinic', is_active: true }] });
});

it('submits the selected organization and renders partial errors and updated counts', async () => {
  vi.mocked(api.post).mockResolvedValue({ data: { created_count: 1, updated_count: 2,
    errors: ['Patient failed', { patient: 'Example', error: 'Access denied' }] } });
  render(<MemoryRouter><UploadFHIR /></MemoryRouter>);
  await waitFor(() => expect(screen.getByLabelText('Organization')).toHaveValue('clinic'));
  fireEvent.change(screen.getByLabelText('Select FHIR JSON File'), { target: { files: [new File(['{}'], 'bundle.JSON')] } });
  fireEvent.click(screen.getByRole('button', { name: 'Upload FHIR Bundle' }));
  expect(await screen.findByText(/Imported 3 patient/)).toBeInTheDocument();
  expect(screen.getByText('Example: Access denied')).toBeInTheDocument();
  expect(screen.getByText('Patient failed')).toBeInTheDocument();
  const form = vi.mocked(api.post).mock.calls[0][1] as FormData;
  expect(form.get('organization')).toBe('clinic');
  expect(form.get('file')).toBeInstanceOf(File);
});

it('shows API permission errors and clears stale results on invalid selection', async () => {
  vi.mocked(api.post).mockRejectedValue({ response: { data: { detail: 'Permission denied.' } } });
  render(<MemoryRouter><UploadFHIR /></MemoryRouter>);
  await waitFor(() => expect(screen.getByLabelText('Organization')).toHaveValue('clinic'));
  fireEvent.change(screen.getByLabelText('Select FHIR JSON File'), { target: { files: [new File(['{}'], 'bundle.json')] } });
  fireEvent.click(screen.getByRole('button', { name: 'Upload FHIR Bundle' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Permission denied.');
  fireEvent.change(screen.getByLabelText('Select FHIR JSON File'), { target: { files: [new File([''], 'wrong.csv')] } });
  expect(screen.getByRole('button', { name: 'Upload FHIR Bundle' })).toBeDisabled();
});
