import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SupportiveTherapyDialog from './SupportiveTherapyDialog';
const { get, post, patch } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }));
vi.mock('@/api/axios', () => ({ default: { get, post, patch } }));
const course = { id: 7, person: 262, regimen_code: 'ivig', regimen_title: 'IVIG', start_date: '2025-01-01', end_date: '2025-02-01', intent: 'Supportive', discontinuation_reason: 'Toxicity' };
beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ data: [{ code: 'ivig', title: 'IVIG' }] });
  post.mockResolvedValue({ data: { course, patient_info: { supportive_therapy_courses: [course] } } });
  patch.mockResolvedValue({ data: { course, patient_info: { supportive_therapy_courses: [course] } } });
});
it('adds a catalog therapy with dates, intent and discontinuation reason', async () => {
  const onSaved = vi.fn();
  render(<SupportiveTherapyDialog personId={262} diseaseCode="C3242" onClose={vi.fn()} onSaved={onSaved} />);
  await screen.findByRole('option', { name: 'IVIG' });
  fireEvent.change(screen.getByLabelText('Supportive therapy'), { target: { value: 'ivig' } });
  fireEvent.change(screen.getByLabelText('Start date'), { target: { value: '2025-01-01' } });
  fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2025-02-01' } });
  fireEvent.change(screen.getByLabelText('Therapy intent'), { target: { value: 'Supportive' } });
  fireEvent.change(screen.getByLabelText('Reason for discontinuation'), { target: { value: 'Toxicity' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save supportive therapy' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/v1/supportive-therapies/', {
    person: 262, regimen_code: 'ivig', start_date: '2025-01-01', end_date: '2025-02-01', intent: 'Supportive', discontinuation_reason: 'Toxicity',
  }));
  expect(onSaved).toHaveBeenCalled();
});
it('edits an existing course, including clearing the end date', async () => {
  render(<SupportiveTherapyDialog personId={262} diseaseCode="C3242" course={course} onClose={vi.fn()} onSaved={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save supportive therapy' })).toBeEnabled());
  expect(screen.getByLabelText('Therapy intent')).toHaveValue('Supportive');
  fireEvent.change(screen.getByLabelText('End date'), { target: { value: '' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save supportive therapy' }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith('/v1/supportive-therapies/7/', expect.objectContaining({ end_date: null })));
  expect(post).not.toHaveBeenCalled();
});
it('keeps the dialog open when saving fails', async () => {
  const onClose = vi.fn();
  patch.mockRejectedValue({ response: { data: { end_date: ['End date cannot precede start date.'] } } });
  render(<SupportiveTherapyDialog personId={262} course={course} onClose={onClose} onSaved={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save supportive therapy' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Save supportive therapy' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('End date cannot precede start date');
  expect(onClose).not.toHaveBeenCalled();
});
