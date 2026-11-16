import { describe, expect, it } from 'vitest';
import { hasNewSaveError } from './saveErrors';

describe('hasNewSaveError', () => {
  it('ignores an old error carried into a new errors object', () => {
    const before = { weight: 'Rejected.' };
    expect(hasNewSaveError(before, { ...before })).toBe(false);
  });

  it('fires for a new field error or a changed message', () => {
    expect(hasNewSaveError({}, { weight: 'Rejected.' })).toBe(true);
    expect(hasNewSaveError({ weight: 'Rejected.' }, { weight: 'Other.' })).toBe(true);
    expect(hasNewSaveError({ weight: 'Rejected.' }, { weight: 'Rejected.', height: 'No.' })).toBe(true);
  });

  it('fires again for a retry that failed after its error was cleared', () => {
    const cleared = {};
    expect(hasNewSaveError(cleared, { weight: 'Rejected.' })).toBe(true);
  });

  it('does not fire when errors are only removed', () => {
    expect(hasNewSaveError({ weight: 'Rejected.' }, {})).toBe(false);
  });
});
