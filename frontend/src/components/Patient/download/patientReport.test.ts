import { describe, it, expect } from 'vitest';
import { buildPatientReport, formatValue, ageFrom, type ReportExtras } from './patientReport';
import { renderReportPdf } from './renderReportPdf';
import { textWidth, toWinAnsi, wrapText } from './pdfWriter';
import { formatShortDate } from '@/lib/format';

const EXTRAS: ReportExtras = { genomics: { ok: true, rows: [] } };

const build = (record: Record<string, unknown>, overrides: Partial<Parameters<typeof buildPatientReport>[0]> = {}) =>
  buildPatientReport({
    record, name: 'Ada Lovelace', personId: 42, diseaseType: 'other', patientMode: false,
    extras: EXTRAS, generatedAt: new Date('2026-10-21T12:00:00Z'), ...overrides,
  });

const tab = (report: ReturnType<typeof build>, title: string) => report.tabs.find((t) => t.title === title);
const section = (report: ReturnType<typeof build>, tabTitle: string, title: string) =>
  tab(report, tabTitle)?.sections.find((s) => s.title === title);

describe('formatValue', () => {
  it('prints what has a value and nothing for what does not', () => {
    expect(formatValue(null)).toBeNull();
    expect(formatValue('  ')).toBeNull();
    expect(formatValue([])).toBeNull();
    expect(formatValue({ a: 1 })).toBeNull();
    expect(formatValue(true)).toBe('Yes');
    expect(formatValue(false)).toBe('No');
    expect(formatValue(0)).toBe('0');
    expect(formatValue(['autologous SCT', 'tandem SCT'])).toBe('autologous SCT, tandem SCT');
    expect(formatValue('2024-03-05')).toBe(formatShortDate('2024-03-05'));
  });

  it('computes age on the report date', () => {
    expect(ageFrom('1980-10-22', new Date('2026-10-21T00:00:00Z'))).toBe(45);
    expect(ageFrom('1980-10-21', new Date('2026-10-21T00:00:00Z'))).toBe(46);
    expect(ageFrom('not a date')).toBeNull();
  });
});

describe('buildPatientReport', () => {
  it('uses the tab labels and sections, and leaves out empty fields', () => {
    const report = build({ date_of_birth: '1980-01-02', weight: 61.5, hemoglobin_g_dl: 12.1, smoking_status: '' });
    expect(report.tabs.map((t) => t.title)).toEqual(['General', 'Treatment', 'Labs', 'Genomics', 'Behavior', 'Wearables']);
    expect(section(report, 'General', 'Physical Measurements')?.fields).toEqual([{ label: 'Weight (kg)', value: '61.5' }]);
    expect(section(report, 'Labs', 'Blood Counts')?.fields).toEqual([{ label: 'Hemoglobin (g/dL)', value: '12.1' }]);
    expect(tab(report, 'Behavior')?.sections).toEqual([]);
    expect(section(report, 'General', 'Patient Details')?.fields).toEqual([
      { label: 'Patient Name', value: 'Ada Lovelace' },
      { label: 'Date of Birth', value: formatShortDate('1980-01-02') },
      { label: 'Age', value: '46' },
    ]);
  });

  it('prints only the disease sections the Disease tab shows for that disease', () => {
    const record = { disease: 'Multiple Myeloma', myeloma_type: 'IgG', stage: 'II', histologic_type: 'Ductal', cytogenetic_markers: 't(4;14)' };
    const report = build(record, { diseaseType: 'myeloma', diseaseLabel: 'Multiple Myeloma' });
    const disease = tab(report, 'Multiple Myeloma')!;
    expect(disease.sections.map((s) => s.title)).toEqual(['Disease Characteristics', 'Cytogenetics']);
    expect(disease.sections[0].fields).toContainEqual({ label: 'Recorded ISS / R-ISS Stage', value: 'II' });
    // histologic_type is a breast-cancer field; it does not leak onto the myeloma tab.
    expect(JSON.stringify(disease)).not.toContain('Ductal');
  });

  it('labels region and places postal code the way the General tab does', () => {
    const us = section(build({ country: 'United States', region: 'Utah', postal_code: '84101' }), 'General', 'Location');
    expect(us?.fields).toEqual([
      { label: 'Country', value: 'United States' },
      { label: 'Postal Code / Zip Code', value: '84101' },
      { label: 'State', value: 'Utah' },
    ]);
    const abroad = section(build({ country: 'Canada', region: 'Ontario' }), 'General', 'Location');
    expect(abroad?.fields).toContainEqual({ label: 'Region/State', value: 'Ontario' });
  });

  it('hides staff-only sections from the patient themself', () => {
    const record = { validated: true };
    expect(section(build(record), 'General', 'Clinician Validation')).toBeDefined();
    expect(section(build(record, { patientMode: true }), 'General', 'Clinician Validation')).toBeUndefined();
  });

  it('tabulates therapy lines in line order', () => {
    const report = build({
      lines_of_therapy: [
        { line: 2, regimen: 'KRd', start_date: '2024-01-01' },
        { line: 1, regimen: 'VRd', start_date: '2023-01-01', end_date: '2023-12-01', outcome: 'VGPR' },
      ],
    });
    const rows = section(report, 'Treatment', 'Lines of Therapy')!.table!.rows;
    expect(rows.map((r) => r.slice(0, 2))).toEqual([['1', 'VRd'], ['2', 'KRd']]);
    expect(rows[1][4]).toBe('Present');
  });

  it('says when a list could not be loaded rather than leaving it out', () => {
    const report = build({}, { extras: { genomics: { ok: false } } });
    expect(section(report, 'Genomics', 'Genomic Findings')?.note).toMatch(/could not be loaded/);
  });

  it('adds allergies and immunizations in patient mode', () => {
    const report = build({}, {
      patientMode: true,
      extras: {
        genomics: { ok: true, rows: [{ id: 1, genomic_feature: 'TP53', variant_name: 'R175H', interpretation: 'Pathogenic' }] },
        allergies: { ok: true, rows: [{ allergen_name: 'Penicillin', criticality: 'high' }] },
        immunizations: { ok: true, rows: [{ vaccine_name: 'Influenza', date: '2025-10-01' }] },
      },
    });
    expect(section(report, 'Allergies', 'Allergies and Intolerances')?.table?.rows[0][0]).toBe('Penicillin');
    expect(section(report, 'Treatment', 'Immunizations')?.table?.rows[0]).toEqual(['Influenza', formatShortDate('2025-10-01'), '']);
    expect(section(report, 'Genomics', 'Genomic Findings')?.table?.rows[0].slice(0, 3)).toEqual(['TP53', 'R175H', 'Present']);
  });
});

describe('pdf text', () => {
  it('encodes to WinAnsi and transliterates what it lacks', () => {
    expect(toWinAnsi('é')).toEqual([0xe9]);
    expect(toWinAnsi('—')).toEqual([0x97]);
    expect(String.fromCharCode(...toWinAnsi('SpO₂ ≥ 10⁹'))).toBe('SpO2 >= 10^9');
    expect(String.fromCharCode(...toWinAnsi('李'))).toBe('?');
  });

  it('wraps to the width and hard-breaks a word that cannot fit', () => {
    const lines = wrapText('alpha beta gamma delta', 'regular', 10, textWidth('gamma delta', 'regular', 10));
    expect(lines).toEqual(['alpha beta', 'gamma delta']);
    const long = wrapText('NM_000546.6:c.524G>A', 'regular', 10, 40);
    expect(long.length).toBeGreaterThan(1);
    expect(long.join('')).toBe('NM_000546.6:c.524G>A');
  });
});

describe('renderReportPdf', () => {
  const decode = (bytes: Uint8Array) => String.fromCharCode(...bytes);

  it('writes a well-formed PDF whose xref offsets point at its objects', () => {
    const pdf = decode(renderReportPdf(build({ weight: 70, preexisting_conditions: 'Hypertension (diagnosed 2019)' })));
    expect(pdf.startsWith('%PDF-1.4')).toBe(true);
    expect(pdf.trimEnd().endsWith('%%EOF')).toBe(true);
    const startxref = Number(/startxref\n(\d+)/.exec(pdf)![1]);
    expect(pdf.slice(startxref, startxref + 4)).toBe('xref');
    const offsets = [...pdf.slice(startxref).matchAll(/^(\d{10}) 00000 n $/gm)].map((m) => Number(m[1]));
    offsets.forEach((offset, i) => expect(pdf.slice(offset).startsWith(`${i + 1} 0 obj`)).toBe(true));
    // Parentheses in a value are escaped, or the content stream would end early.
    expect(pdf).toContain('(Hypertension \\(diagnosed 2019\\)) Tj');
    expect(pdf).toContain('(Weight \\(kg\\)) Tj');
  });

  it('flows onto more pages and numbers every one', () => {
    const lines = Array.from({ length: 80 }, (_, i) => ({ line: i + 1, regimen: `Regimen ${i + 1}` }));
    const pdf = decode(renderReportPdf(build({ lines_of_therapy: lines })));
    const pages = Number(/\/Type \/Pages \/Kids \[[^\]]*\] \/Count (\d+)/.exec(pdf)![1]);
    expect(pages).toBeGreaterThan(1);
    expect(pdf).toContain(`(Page ${pages} of ${pages}) Tj`);
    // The table header repeats on the page the table continues onto.
    expect(pdf.split('(REGIMEN) Tj').length - 1).toBeGreaterThan(1);
  });
});
