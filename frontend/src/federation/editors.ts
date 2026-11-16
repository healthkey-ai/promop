// The React editors for a host that shares this React tree, exposed as ONE
// module on purpose.
//
// The federation runtime answers a failed remote load with a stub that has a
// default export and nothing else. Importing the editors one module at a time
// let a stub pass a `typeof default === "function"` check — a host would mount
// a slot that renders nothing and lose its own editor. A single module means
// the host either gets the whole set or nothing, and the named marker below is
// what tells the two apart.
//
// Consume:
//   const mod = await import("labs_results_remote/editors");
//   if (mod.PROMOP_EDITORS_API !== 1) throw … // remote unavailable
export { default as TherapyLinesEditor } from './TherapyLinesEditor';
export { default as GenomicsEditor } from './GenomicsEditor';
export { default as PatientFieldEditorSlot } from './PatientFieldEditorSlot';
export { usePromopFieldBindings } from './usePromopFieldBindings';
export { default as TrialEditors } from './TrialEditors';
export { isGenomicsTrialRow, isTherapyTrialRow } from './trialRows';
export type { SaveState } from './patientFormStore';

/** Bumped only on a breaking change to this set's props or behavior. */
export const PROMOP_EDITORS_API = 1;
