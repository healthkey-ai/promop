import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TreatingInstitutionField from './TreatingInstitutionField';

const get = vi.hoisted(() => vi.fn());
vi.mock('@/api/clinicalTransport', () => ({ clinicalClient: () => ({ get }), clinicalUrl: (s: string) => s }));
const descriptor = { writable: true, kind: 'direct' as const };
beforeEach(() => {
  get.mockReset().mockResolvedValue({ data: { institutions: [
    { id: 'a', label: 'Example Cancer Center — Boston, Massachusetts', state_code: 'MA' },
    { id: 'b', label: 'Another Cancer Center — Houston, Texas', state_code: 'TX' },
  ] } });
});

it('filters by location and commits a keyboard selection without saving search text', async () => {
  const onChange = vi.fn();
  render(<TreatingInstitutionField value="" descriptor={descriptor} onChange={onChange} />);
  const input = screen.getByRole('combobox', { name: 'Treating Institution' });
  fireEvent.focus(input);
  await screen.findByRole('option', { name: /Houston/ });
  fireEvent.change(input, { target: { value: 'Boston' } });
  expect(screen.queryByRole('option', { name: /Houston/ })).toBeNull();
  expect(onChange).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(onChange).toHaveBeenCalledWith('facility_name', 'Example Cancer Center — Boston, Massachusetts');
  expect(screen.queryByRole('listbox')).toBeNull();
});

it('preserves existing free text, cancels search, and supports a custom center and clearing', async () => {
  const onChange = vi.fn();
  const { rerender } = render(<TreatingInstitutionField value="Local hospital" descriptor={descriptor} onChange={onChange} />);
  const input = screen.getByRole('combobox');
  fireEvent.change(input, { target: { value: 'search' } });
  fireEvent.keyDown(input, { key: 'Escape' });
  expect(input).toHaveValue('Local hospital');
  fireEvent.change(input, { target: { value: 'New hospital' } });
  fireEvent.click(await screen.findByRole('option', { name: 'Use entered name: New hospital' }));
  expect(onChange).toHaveBeenCalledWith('facility_name', 'New hospital');
  rerender(<TreatingInstitutionField value="New hospital" descriptor={descriptor} onChange={onChange} />);
  fireEvent.click(screen.getByRole('button', { name: 'Clear treating institution' }));
  expect(onChange).toHaveBeenLastCalledWith('facility_name', '');
});

it('honors the caller permission and leaves the current institution visible', async () => {
  render(<TreatingInstitutionField value="Local hospital" descriptor={{ ...descriptor, writable: false, reason: 'Read access only' }} onChange={vi.fn()} />);
  await waitFor(() => expect(get).toHaveBeenCalled());
  expect(screen.getByRole('combobox')).toHaveAttribute('readonly');
  expect(screen.getByRole('combobox')).toHaveValue('Local hospital');
  expect(screen.getByText('Read access only')).toBeInTheDocument();
  expect(screen.queryByRole('button')).toBeNull();
});

it.each(['offline', 'invalid'])('allows a custom center when the directory is %s', async (failure) => {
  if (failure === 'offline') get.mockRejectedValue(new Error('offline'));
  else get.mockResolvedValue({ data: {} });
  const onChange = vi.fn();
  render(<TreatingInstitutionField value="" descriptor={descriptor} onChange={onChange} />);
  await screen.findByText(/Directory unavailable/);
  const input = screen.getByRole('combobox');
  fireEvent.change(input, { target: { value: 'Local hospital' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(onChange).toHaveBeenCalledWith('facility_name', 'Local hospital');
});
