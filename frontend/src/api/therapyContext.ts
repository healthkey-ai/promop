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
