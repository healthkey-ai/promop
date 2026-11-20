import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import api from '@/api/axios';
import CanonicalUnitEditor from './CanonicalUnitEditor';
vi.mock('@/api/axios', () => ({ default: { get: vi.fn(), put: vi.fn() } }));
const setting = { unit: '', revision: 0, property: 'MCnc', available_units: ['g/L', 'g/dL'], example_units: ['g/L', 'g/dL'], can_edit: true };
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.get).mockResolvedValue({ data: setting }); });
it('shows example units and saves a separate instance choice with its revision', async () => {
  vi.mocked(api.put).mockResolvedValue({ data: { ...setting, unit: 'g/dL', revision: 1 } });
  render(<CanonicalUnitEditor conceptId={12} />);
  expect(await screen.findByText('LOINC example units: g/L, g/dL')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Canonical unit'), { target: { value: 'g/dL' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save unit setting' }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/v1/concepts/12/canonical-unit/', { unit: 'g/dL', revision: 0 }));
  expect(await screen.findByText(/Unit setting saved/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save unit setting' })).toBeDisabled();
});
it('does not let ordinary curators change instance settings', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { ...setting, can_edit: false } });
  render(<CanonicalUnitEditor conceptId={12} />);
  expect(await screen.findByLabelText('Canonical unit')).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Save unit setting' })).not.toBeInTheDocument();
});
it('shows a concurrent-edit error without pretending the save succeeded', async () => {
  vi.mocked(api.put).mockRejectedValue({ response: { data: { detail: 'This unit setting changed. Reload it before saving.' } } });
  render(<CanonicalUnitEditor conceptId={12} />);
  fireEvent.change(await screen.findByLabelText('Canonical unit'), { target: { value: 'g/L' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save unit setting' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Reload it before saving');
  expect(screen.queryByText(/Unit setting saved/)).not.toBeInTheDocument();
});
