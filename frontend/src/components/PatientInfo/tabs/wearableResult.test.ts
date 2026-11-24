import { describe, expect, it } from 'vitest';
import { pendingClause } from './wearableResult';

describe('pendingClause', () => {
  it('is empty when nothing is provisional or waiting', () => {
    expect(pendingClause({})).toBe('');
    expect(pendingClause({ provisional_samples: 0, unresolved_samples: 0 })).toBe('');
    expect(pendingClause({ provisional_samples: null, unresolved_samples: undefined })).toBe('');
  });

  it('says how many are provisional, in the singular for one', () => {
    expect(pendingClause({ provisional_samples: 3 })).toBe('3 are provisional: stored, and not yet confirmed.');
    expect(pendingClause({ provisional_samples: 1 })).toBe('1 is provisional: stored, and not yet confirmed.');
  });

  it('says how many are stored and waiting', () => {
    expect(pendingClause({ unresolved_samples: 2 })).toBe(
      '2 are stored and waiting to be matched to a standard code.');
    expect(pendingClause({ unresolved_samples: 1 })).toBe(
      '1 is stored and waiting to be matched to a standard code.');
  });

  it('says both as the server gives them: they are separate readings, never one inside the other', () => {
    expect(pendingClause({ provisional_samples: 6, unresolved_samples: 4 })).toBe(
      '6 are provisional: stored, and not yet confirmed. '
      + '4 are stored and waiting to be matched to a standard code.');
  });

  it('is worded for a patient: no mapping, no approval', () => {
    const text = pendingClause({ provisional_samples: 2, unresolved_samples: 2 });
    expect(text).not.toMatch(/mapping|approv/i);
  });

  it('ignores a count that is not a positive number', () => {
    expect(pendingClause({ provisional_samples: -2, unresolved_samples: Number.NaN })).toBe('');
  });
});
