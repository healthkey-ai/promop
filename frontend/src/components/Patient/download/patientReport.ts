/**
 * The patient record as a printable document: the tabs, in their order, under
 * their labels, holding only what has a value.
 *
 * Labels and sections come from `TAB_LAYOUT`, which is read out of the tab
 * components themselves (see `scripts/tab-layout.mjs`), so a field added to a
 * tab reaches the PDF without anyone remembering to. What a tab renders as a
 * list rather than a field — therapy lines, supportive courses, genomic
 * findings, allergies, immunizations — is assembled here.
 *
 * This module is pure: `loadReportExtras` fetches what the record does not carry
 * and `buildPatientReport` turns both into sections, so the layout is testable
 * without a network or a PDF.
 */
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { formatShortDate } from '@/lib/format';
import type { EditableTherapyLine } from '@/api/therapyLines';
import type { SupportiveTherapyCourse } from '@/api/supportiveTherapies';
import { TAB_LAYOUT } from './tabLayout.generated';
import type { TabLayoutSection } from './tabLayoutTypes';

export type DiseaseType = 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'mcl' | 'other';

export interface ReportField { label: string; value: string }
export interface ReportTable { columns: string[]; rows: string[][] }
export interface ReportSection {
  title: string;
  fields?: ReportField[];
  table?: ReportTable;
  /** Shown instead of content, e.g. a list that could not be loaded. */
  note?: string;
}
export interface ReportTab { title: string; sections: ReportSection[] }
export interface PatientReport {
  name: string;
  personId: number;
  summary: ReportField[];
  tabs: ReportTab[];
  generatedAt: Date;
}

/** DiseaseTab renders one of these components per disease; the PDF follows suit. */
export const DISEASE_COMPONENTS: Record<Exclude<DiseaseType, 'other'>, string[]> = {
  breast: ['BreastCancerSection', 'StagingBiomarkersSection'],
  lymphoma: ['LymphomaSection'],
  myeloma: ['MyelomaSection'],
  cll: ['CLLSection'],
  mcl: ['MCLSection'],
};

/**
 * Fields the tabs render that are not a plain label and value, so the layout
 * extraction does not list them; each is printed by this module instead.
 */
export const ASSEMBLED_FIELDS = [
  'genetic_mutations', // Genomics tab table
  'cytogenetic_markers', // Myeloma "Cytogenetics" legacy summary
] as const;

type Row = Record<string, unknown>;

export interface GenomicFinding {
  id?: number;
  genomic_feature?: string | null;
  gene?: string | null;
  variant_name?: string | null;
  variant?: string | null;
  genomic_dna_change?: string | null;
  amino_acid_change?: string | null;
  status?: string | null;
  assessment?: string | null;
  origin?: string | null;
  genomic_source_class?: string | null;
  interpretation?: string | null;
  allelic_frequency?: number | string | null;
  allelic_frequency_unit?: string | null;
  test_date?: string | null;
}
export interface Allergy { allergen_name: string; criticality?: string; clinical_status?: string; recorded_date?: string | null }
export interface Immunization { vaccine_name: string; date?: string | null; lot_number?: string | null }

/** A list that failed to load is reported, never silently left out. */
export type Loaded<T> = { ok: true; rows: T[] } | { ok: false };

export interface ReportExtras {
  genomics: Loaded<GenomicFinding>;
  allergies?: Loaded<Allergy>;
  immunizations?: Loaded<Immunization>;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:?\d{2})?)?$/;

/** How a PatientRecord value reads on paper; `null` when there is nothing to print. */
export function formatValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : null;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return null;
    return ISO_DATE.test(text) ? formatShortDate(text) : text;
  }
  if (Array.isArray(value)) {
    const parts = value.map(formatValue).filter((v): v is string => v !== null);
    return parts.length ? parts.join(', ') : null;
  }
  return null; // an object has no single reading; the tabs that hold one are assembled
}

const date = (v: string | null | undefined) => (v ? formatShortDate(v) : '');

export function ageFrom(dateOfBirth: unknown, today = new Date()): number | null {
  if (typeof dateOfBirth !== 'string' || !dateOfBirth) return null;
  const birth = new Date(dateOfBirth);
  if (Number.isNaN(birth.getTime())) return null;
  let age = today.getUTCFullYear() - birth.getUTCFullYear();
  const m = today.getUTCMonth() - birth.getUTCMonth();
  if (m < 0 || (m === 0 && today.getUTCDate() < birth.getUTCDate())) age--;
  return age;
}

/** Fields a tab renders outside a ClinicalField, placed where the tab puts them. */
interface ExtraFields {
  /** Before the section's first field. */
  first?: ReportField[];
  /** Immediately after the named field. */
  after?: Record<string, ReportField[]>;
}

function layoutSections(
  record: Row,
  sections: TabLayoutSection[],
  opts: { patientMode: boolean; extra?: Record<string, ExtraFields> },
): ReportSection[] {
  return sections
    .filter((s) => !(s.staffOnly && opts.patientMode))
    .map((s) => {
      const extra = opts.extra?.[s.title];
      const fields = [...(extra?.first ?? [])];
      for (const [label, name] of s.fields) {
        const value = formatValue(record[name]);
        if (value !== null) fields.push({ label: relabel(record, name, label), value });
        fields.push(...(extra?.after?.[name] ?? []));
      }
      return { title: s.title, fields };
    })
    .filter((s) => s.fields.length > 0);
}

/** GeneralTab offers `region` as a US State picker or as free text. */
const relabel = (record: Row, name: string, label: string) =>
  name === 'region' && record.country !== 'United States' ? 'Region/State' : label;

const loadedTable = <T,>(
  title: string,
  loaded: Loaded<T> | undefined,
  columns: string[],
  toRow: (row: T) => string[],
): ReportSection | null => {
  if (!loaded) return null;
  if (!loaded.ok) return { title, note: `${title} could not be loaded when this report was generated.` };
  if (!loaded.rows.length) return null;
  return { title, table: { columns, rows: loaded.rows.map(toRow) } };
};

function findingStatus(f: GenomicFinding): string {
  const state = f.status
    || (['no_call', 'not_tested'].includes(String(f.assessment)) ? 'indeterminate' : f.assessment)
    || 'present';
  return ({ present: 'Present', absent: 'Absent', indeterminate: 'Indeterminate' } as Record<string, string>)[String(state)]
    ?? 'Indeterminate';
}

function genomicsSection(loaded: Loaded<GenomicFinding>): ReportSection | null {
  return loadedTable('Genomic Findings', loaded,
    ['Genomic feature', 'Finding / variant', 'Status', 'Origin', 'Interpretation', 'VAF', 'Test date'],
    (f) => [
      String(f.genomic_feature || f.gene?.toUpperCase() || ''),
      [f.variant_name || f.variant || f.genomic_dna_change, f.amino_acid_change].filter(Boolean).join(' '),
      findingStatus(f),
      String(f.origin || f.genomic_source_class || ''),
      String(f.interpretation || ''),
      f.allelic_frequency != null && f.allelic_frequency !== ''
        ? `${f.allelic_frequency}${f.allelic_frequency_unit === '1' ? ' (fraction)' : '%'}` : '',
      date(f.test_date),
    ]);
}

export function buildPatientReport(input: {
  record: Row;
  name: string;
  personId: number;
  diseaseType: DiseaseType;
  diseaseLabel?: string;
  patientMode: boolean;
  extras: ReportExtras;
  generatedAt?: Date;
}): PatientReport {
  const { record, patientMode, extras } = input;
  const generatedAt = input.generatedAt ?? new Date();
  const age = ageFrom(record.date_of_birth, generatedAt);
  const tabs: ReportTab[] = [];
  const nonEmpty = (xs: (ReportSection | null)[]) => xs.filter((x): x is ReportSection => x !== null);

  const postal = formatValue(record.postal_code);
  tabs.push({
    title: 'General',
    sections: layoutSections(record, TAB_LAYOUT.GeneralTab, {
      patientMode,
      extra: {
        'Patient Details': {
          first: [
            ...(input.name ? [{ label: 'Patient Name', value: input.name }] : []),
          ],
          after: { date_of_birth: age !== null ? [{ label: 'Age', value: String(age) }] : [] },
        },
        Location: { after: { country: postal ? [{ label: 'Postal Code / Zip Code', value: postal }] : [] } },
      },
    }),
  });

  if (input.diseaseType !== 'other') {
    const components = DISEASE_COMPONENTS[input.diseaseType];
    const sections = layoutSections(
      record,
      TAB_LAYOUT.DiseaseTab.filter((s) => components.includes(s.component)),
      { patientMode },
    );
    const cytogenetics = input.diseaseType === 'myeloma' ? formatValue(record.cytogenetic_markers) : null;
    if (cytogenetics) {
      sections.push({ title: 'Cytogenetics', fields: [{ label: 'Legacy cytogenetic summary', value: cytogenetics }] });
    }
    tabs.push({ title: input.diseaseLabel || 'Disease', sections });
  }

  const lines = (Array.isArray(record.lines_of_therapy) ? record.lines_of_therapy : []) as EditableTherapyLine[];
  const courses = (Array.isArray(record.supportive_therapy_courses) ? record.supportive_therapy_courses : []) as SupportiveTherapyCourse[];
  const supportiveText = formatValue(record.supportive_therapies);
  const planned = formatValue(record.planned_therapies);
  tabs.push({
    title: 'Treatment',
    sections: nonEmpty([
      lines.length ? {
        title: 'Lines of Therapy',
        table: {
          columns: ['Line', 'Regimen', 'Intent', 'Start', 'End', 'Outcome', 'Reason stopped'],
          rows: [...lines].sort((a, b) => a.line - b.line).map((l) => [
            String(l.line), l.regimen || 'Unnamed regimen', l.intent || '', date(l.start_date),
            l.end_date ? date(l.end_date) : 'Present', l.outcome || '', l.discontinuation_reason || '',
          ]),
        },
      } : null,
      ...layoutSections(record, TAB_LAYOUT.TreatmentTab, { patientMode }),
      courses.length ? {
        title: 'Supportive Therapy',
        table: {
          columns: ['Therapy', 'Intent', 'Start', 'End', 'Reason stopped'],
          rows: courses.map((c) => [
            c.regimen_title, c.intent || '', date(c.start_date), c.end_date ? date(c.end_date) : 'Present',
            c.discontinuation_reason || '',
          ]),
        },
      } : supportiveText ? { title: 'Supportive Therapy', fields: [{ label: 'Supportive Therapies', value: supportiveText }] } : null,
      planned ? { title: 'Planned Therapies', fields: [{ label: 'Planned Therapies', value: planned }] } : null,
      loadedTable('Immunizations', extras.immunizations, ['Vaccine', 'Date', 'Lot number'],
        (i) => [i.vaccine_name, date(i.date), i.lot_number || '']),
    ]),
  });

  tabs.push({ title: 'Labs', sections: layoutSections(record, TAB_LAYOUT.LabsTab, { patientMode }) });
  tabs.push({ title: 'Genomics', sections: nonEmpty([genomicsSection(extras.genomics)]) });
  if (extras.allergies) {
    tabs.push({
      title: 'Allergies',
      sections: nonEmpty([loadedTable('Allergies and Intolerances', extras.allergies,
        ['Allergen', 'Criticality', 'Status', 'Recorded'],
        (a) => [a.allergen_name, a.criticality || '', a.clinical_status || '', date(a.recorded_date)])]),
    });
  }
  tabs.push({ title: 'Behavior', sections: layoutSections(record, TAB_LAYOUT.BehaviorTab, { patientMode }) });
  tabs.push({ title: 'Wearables', sections: layoutSections(record, TAB_LAYOUT.WearableTab, { patientMode }) });

  const disease = formatValue(record.disease);
  const dob = formatValue(record.date_of_birth);
  const summary: ReportField[] = [
    { label: 'Patient number', value: `#${input.personId}` },
    ...(dob ? [{ label: 'Date of birth', value: age !== null ? `${dob} (age ${age})` : dob }] : []),
    ...(formatValue(record.gender) ? [{ label: 'Gender', value: formatValue(record.gender)! }] : []),
    ...(disease ? [{ label: 'Disease', value: disease }] : []),
  ];

  return { name: input.name, personId: input.personId, summary, tabs, generatedAt };
}

async function load<T>(path: string, params?: Record<string, unknown>): Promise<Loaded<T>> {
  try {
    const { data } = await clinicalClient().get(clinicalUrl(path), { params });
    const rows = Array.isArray(data) ? data : data?.results;
    return Array.isArray(rows) ? { ok: true, rows } : { ok: false };
  } catch {
    return { ok: false };
  }
}

/** What the tabs fetch for themselves rather than read off the record. */
export async function loadReportExtras(personId: number, patientMode: boolean): Promise<ReportExtras> {
  const [genomics, allergies, immunizations] = await Promise.all([
    load<GenomicFinding>(`/v1/patient-records/${personId}/genomics/`),
    // The Allergies tab and the Treatment tab's immunizations are patient-mode only.
    patientMode ? load<Allergy>('/v1/allergies/', { person_id: personId }) : undefined,
    patientMode ? load<Immunization>('/v1/immunizations/', { person_id: personId }) : undefined,
  ]);
  return { genomics, allergies, immunizations };
}
