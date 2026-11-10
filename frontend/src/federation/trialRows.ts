/**
 * Trial-catalog row names that PROMOP owns rather than a single record column.
 *
 * Kept in step with CB's `trialDetailsHelpers`, which drove the same slots
 * before both hosts shared this adapter. Therapy history is inferred from
 * recorded lines, so its editor is the line editor; genomic findings are a set
 * of rows with their own dialog. Everything else is a scalar the descriptor can
 * resolve (canonical key or one of its declared `bindings`).
 */
const THERAPY_ROWS = new Set([
  'therapyLinesCount',
  'therapyLinesCountMin',
  'therapyLinesCountMax',
  'therapyTypesRequired',
  'therapyTypesExcluded',
  'therapiesRequired',
  'therapiesExcluded',
  'therapyComponentsRequired',
  'therapyComponentsExcluded',
  'firstRoundTherapy',
  'laterTherapy',
  'priorTherapy',
]);

const GENOMICS_ROWS = new Set([
  'mutationGenesRequired',
  'mutationInterpretationsRequired',
  'mutationVariantsRequired',
]);

export function isTherapyTrialRow(name: string): boolean {
  return THERAPY_ROWS.has(name);
}

export function isGenomicsTrialRow(name: string): boolean {
  return GENOMICS_ROWS.has(name) || name === 'geneticMutations';
}

