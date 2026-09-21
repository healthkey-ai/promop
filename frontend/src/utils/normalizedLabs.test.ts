import { expect, it } from 'vitest';
import { normalizedLabValue } from './normalizedLabs';
import type { LabResultValue } from '@/federation/types';
const raw: LabResultValue = { measurement_id: 1, value: 20, value_string: null, unit: 'g/L', status: 'in_range', measured_at: '2026-09-20', range_low: 10, range_high: 30, source: null, lab_name: null, report_filename: null };
it('uses converted values and ranges together while retaining the original edit contract', () => {
  const source = { ...raw, normalized: { value: '2', unit: 'g/dL', revision: 1, range_low: '1', range_high: '3', error: null } };
  const result = normalizedLabValue(source);
  expect(result).toMatchObject({ value: 2, unit: 'g/dL', range_low: 1, range_high: 3, status: 'in_range' });
  expect(result.original).toBe(source);
  expect(result.original?.value).toBe(20);
  expect(normalizedLabValue(raw)).toBe(raw);
});
it('never labels an unconvertible numeric string as a canonical measurement', () => {
  const result = normalizedLabValue({ ...raw, value_string: '20', normalized: { value: null, unit: 'g/dL', revision: 1, range_low: null, range_high: null, error: 'Unknown source unit' } });
  expect(result.value).toBeNull();
  expect(result.value_string).toBeNull();
  expect(result.status).toBe('unknown');
  expect(result.original?.value).toBe(20);
});
