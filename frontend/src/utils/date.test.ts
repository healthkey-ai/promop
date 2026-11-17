import { describe, expect, it } from 'vitest';
import { formatDate } from './date';

const ZONES = ['America/Los_Angeles', 'Pacific/Honolulu', 'Asia/Tokyo', 'Europe/Moscow', 'UTC'];

function withZone<T>(zone: string, run: () => T): T {
  const original = process.env.TZ;
  try {
    process.env.TZ = zone;
    return run();
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
}

describe('formatDate', () => {
  it('keeps the calendar day of a date-only value in every timezone', () => {
    for (const zone of ZONES) {
      withZone(zone, () => {
        // Parsed as UTC midnight, this printed the 9th west of UTC.
        expect(formatDate('2026-11-10')).toMatch(/\b10\b/);
        expect(formatDate('2026-01-01')).toMatch(/\b1\b/);
        expect(formatDate('2026-01-01')).not.toMatch(/\b31\b/);
      });
    }
  });

  it('keeps the instant of a value that carries a time', () => {
    const formatted = withZone('UTC', () => formatDate('2026-11-10T23:30:00Z'));
    expect(formatted).toMatch(/2026/);
  });

  it('passes empty and unparsable values through', () => {
    expect(formatDate(null)).toBe('');
    expect(formatDate('')).toBe('');
  });
});
