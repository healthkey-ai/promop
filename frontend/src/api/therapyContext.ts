/** Existing treatment-picker disease routing, shared by both form surfaces. */
export function diseaseToDiseaseCode(disease: unknown, diseaseType = 'other'): string | undefined {
  if (typeof disease === 'string') {
    const d = disease.toLowerCase();
    if (['c3242', 'c3209', 'c9335', 'c2987', 'mcl', 'dlbcl'].includes(d)) return disease.toUpperCase();
    if (d.includes('mantle')) return 'MCL';
    if (d.includes('follicular')) return 'C3209';
    if (d.includes('myeloma') || d === 'mm') return 'C3242';
    if (d.includes('cll') || d.includes('chronic lymphocytic') || d.includes('chronic lymphoid')) return 'C2987';
    if (d.includes('breast')) return 'C9335';
    if (d.includes('diffuse large b-cell') || d.includes('dlbcl')) return 'DLBCL';
  }
  return ({ myeloma: 'C3242', cll: 'C2987', breast: 'C9335' } as Record<string, string>)[diseaseType];
}

/** The highest line number the record's line rows carry; 0 when there are none.
 *  A row without a readable `line` (missing, null, non-numeric, or a null row)
 *  counts as no line rather than turning the result into NaN. */
export function lastRecordedTherapyLine(lines: unknown): number {
  if (!Array.isArray(lines)) return 0;
  return lines.reduce<number>((max, row) => {
    const line = Number((row as { line?: unknown } | null)?.line);
    return Number.isFinite(line) ? Math.max(max, line) : max;
  }, 0);
}

/**
 * The therapy-regimen round for the next planned line, shared by the Treatment
 * tab and the federated store so the two surfaces ask the catalog the same
 * question. The derived `therapy_lines_count` still counts: an imported
 * patient can carry a count of 1 without readable line rows, and the next
 * planned round is then second-line.
 */
export function plannedTherapyRound(therapyLinesCount: unknown, lines: unknown): string {
  const raw = String(therapyLinesCount ?? '');
  const count = raw === '3+' ? 3 : parseInt(raw) || 0;
  const next = Math.max(count, lastRecordedTherapyLine(lines)) + 1;
  return next === 1 ? 'first_line_therapy'
    : next === 2 ? 'second_line_therapy'
    : 'later_line_therapy';
}
