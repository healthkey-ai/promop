import { describe, expect, it } from 'vitest';
import { pendingClause } from './wearableResult';

describe('pendingClause', () => {
  it('is empty when nothing is provisional or waiting', () => {
    expect(pendingClause({})).toBe('');
    expect(pendingClause({ provisional_samples: 0, unresolved_samples: 0 })).toBe('');
    expect(pendingClause({ provisional_samples: null, unresolved_samples: undefined })).toBe('');
  });

  it('says how many are provisional, in the singular for one', () => {
    expect(pendingClause({ provisional_samples: 3 })).toBe('3 provisional until their mapping is approved.');
    expect(pendingClause({ provisional_samples: 1 })).toBe('1 provisional until its mapping is approved.');
  });

  it('says how many are stored and waiting', () => {
    expect(pendingClause({ unresolved_samples: 2 })).toBe('2 stored and waiting for a mapping.');
  });

  it('takes the provisional ones out of the unresolved count, which includes them', () => {
    expect(pendingClause({ provisional_samples: 3, unresolved_samples: 5 })).toBe(
      '3 provisional until their mapping is approved. 2 stored and waiting for a mapping.');
  });

  it('never shows a zero or negative remainder', () => {
    expect(pendingClause({ provisional_samples: 4, unresolved_samples: 4 })).toBe(
      '4 provisional until their mapping is approved.');
    expect(pendingClause({ provisional_samples: 6, unresolved_samples: 4 })).toBe(
      '6 provisional until their mapping is approved.');
  });

  it('ignores a count that is not a positive number', () => {
    expect(pendingClause({ provisional_samples: -2, unresolved_samples: Number.NaN })).toBe('');
  });
});
