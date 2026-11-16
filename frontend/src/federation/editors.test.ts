import { describe, expect, it } from 'vitest';

import {
  GenomicsEditor,
  PatientFieldEditorSlot,
  PROMOP_EDITORS_API,
  TherapyLinesEditor,
  usePromopFieldBindings,
} from './editors';

// The host detects an unavailable remote by this module being the error stub
// (`{ default: () => null }`) instead of the real one. Dropping an export or
// the marker would make that check pass while a slot renders nothing.
describe('the editor module hosts import', () => {
  it('injects the remote stylesheet for hosts that do not mount the full form', () => {
    // The React editors do not pass through PatientInfoProvider, so importing
    // the barrel is where the styles have to arrive.
    expect(document.head.querySelector('style[data-mf="labs-results-remote"]')).not.toBeNull();
  });

  it('exports every editor, the hook, and the availability marker', () => {
    expect(PROMOP_EDITORS_API).toBe(1);
    expect(typeof TherapyLinesEditor).toBe('function');
    expect(typeof GenomicsEditor).toBe('function');
    expect(typeof PatientFieldEditorSlot).toBe('function');
    expect(typeof usePromopFieldBindings).toBe('function');
  });
});
