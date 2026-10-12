import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { EditMeasurementDialog } from './EditMeasurementDialog';
import { normalizedLabValue } from '@/utils/normalizedLabs';
it('edits the preserved source result rather than writing a normalized number in source units', () => {
  const onSave = vi.fn();
  const measurement = normalizedLabValue({ measurement_id: 5, value: 20, value_string: null, unit: 'g/L', status: 'in_range', measured_at: '2026-09-20', range_low: 10, range_high: 30, source: null, lab_name: null, report_filename: null,
    normalized: { value: 2, unit: 'g/dL', revision: 1, range_low: 1, range_high: 3, error: null } });
  render(<EditMeasurementDialog open onOpenChange={() => {}} measurement={measurement} onSave={onSave} isPending={false} />);
  expect(screen.getByText(/Edit the original result in g\/L/)).toBeInTheDocument();
  expect(screen.getByDisplayValue('20')).toBeInTheDocument();
  const valueInput = screen.getByDisplayValue('20');
  fireEvent.change(valueInput, { target: { value: '30' } });
  fireEvent.submit(valueInput.closest('form')!);
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ value: 30, range_low: 10, range_high: 30 }));
});
