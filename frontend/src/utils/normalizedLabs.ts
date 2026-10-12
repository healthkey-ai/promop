import type { LabResultValue } from '@/federation/types';

export type NormalizedMeasurement = {
  unit: string; revision: number; value: number | string | null;
  range_low: number | string | null; range_high: number | string | null; error: string | null;
};

/** The raw API fields remain the edit contract; normalization is a read model. */
export function normalizedLabValue(source: LabResultValue): LabResultValue {
  const n = source.normalized;
  if (!n) return source;
  const value = n.value === null ? null : Number(n.value);
  const low = n.range_low === null ? null : Number(n.range_low);
  const high = n.range_high === null ? null : Number(n.range_high);
  return { ...source, original: source.original ?? source,
    value, range_low: low, range_high: high, unit: n.unit,
    // A numeric string with unknown units must not appear under the target unit.
    value_string: null,
    status: n.error || value === null || (low === null && high === null) ? 'unknown'
      : low !== null && value < low ? 'below' : high !== null && value > high ? 'above' : 'in_range',
  };
}
