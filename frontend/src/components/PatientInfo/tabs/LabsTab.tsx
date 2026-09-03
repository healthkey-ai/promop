import { useState } from 'react';
import ClinicalField from '../ClinicalField';
import Section from '../Section';
import { useWritableFields } from '@/hooks/useWritableFields';
import { today } from '@/api/clinicalFacts';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
}

/**
 * Lab values, rendered against the server's writable-field descriptor.
 *
 * The field names here are the *canonical* ones. This tab previously showed
 * legacy aliases — `egfr`, `serum_sodium`, `magnesium`, `ldh`,
 * `alkaline_phosphatase` — which are populated from their canonical column during
 * derivation and own no LOINC code of their own. Editing an alias could never
 * work: two fields writing one code is the collision #471 removed. The canonical
 * column is the one with a fact behind it, so it is the one shown.
 */

const CHEMISTRY: Array<[string, string]> = [
  ['Serum Creatinine (mg/dL)', 'serum_creatinine_mg_dl'],
  ['Creatinine Clearance (mL/min)', 'creatinine_clearance_ml_min'],
  ['Blood Urea Nitrogen (mg/dL)', 'bun_mg_dl'],
  ['eGFR (mL/min/1.73m²)', 'egfr_ml_min_173m2'],
  ['Sodium (mEq/L)', 'sodium_meq_l'],
  ['Potassium (mEq/L)', 'potassium_meq_l'],
  ['Serum Calcium (mg/dL)', 'serum_calcium_mg_dl'],
  ['Magnesium (mg/dL)', 'magnesium_mg_dl'],
  ['Phosphorus (mg/dL)', 'phosphorus'],
  ['Albumin (g/dL)', 'albumin_g_dl'],
  ['Total Protein (g/dL)', 'total_protein'],
  ['Glucose (mg/dL)', 'glucose_mg_dl'],
];

const LIVER: Array<[string, string]> = [
  ['AST (U/L)', 'ast_u_l'],
  ['ALT (U/L)', 'alt_u_l'],
  ['Alkaline Phosphatase (U/L)', 'alkaline_phosphatase_u_l'],
  ['Total Bilirubin (mg/dL)', 'bilirubin_total_mg_dl'],
  ['Direct Bilirubin (mg/dL)', 'serum_bilirubin_level_direct'],
];

// Prognostic and inflammatory. LDH is a tumour-burden marker here — it sits in
// the R-IPI for DLBCL — which is why it is not filed under "cardiac" anything.
//
// Beta-2 microglobulin lives here, not on DiseaseTab. It is an ISS staging
// parameter, but DiseaseTab's disease sections are mutually exclusive, so
// rendering it there left it unreachable for breast, CLL and unknown-disease
// patients; and its always-rendered section already holds
// serum_beta2_microglobulin_level (LOINC 32731-2), a different fact that would
// have sat beside this one (1952-1) as two indistinguishable mg/L boxes. Labs
// renders for every patient and is where a lab value belongs.
const MARKERS: Array<[string, string]> = [
  ['LDH (U/L)', 'ldh_u_l'],
  ['Beta-2 Microglobulin (mg/L)', 'beta2_microglobulin'],
  ['C-Reactive Protein (mg/L)', 'c_reactive_protein'],
  ['ESR (mm/hr)', 'esr'],
  ['HbA1c (%)', 'hba1c_percent'],
];

const COAGULATION: Array<[string, string]> = [
  ['INR', 'inr'],
  ['Prothrombin Time (s)', 'pt_seconds'],
  ['aPTT (s)', 'ptt_seconds'],
];

const TUMOR_MARKERS: Array<[string, string]> = [
  ['CEA (ng/mL)', 'cea_ng_ml'],
  ['CA 19-9 (U/mL)', 'ca19_9_u_ml'],
  ['PSA (ng/mL)', 'psa_ng_ml'],
];

// Split out of "Other Markers": these are cardiac, and grouping them with
// tumour-burden markers said the wrong thing about why an oncology record
// tracks them. They matter here for anthracycline and HER2-therapy
// cardiotoxicity monitoring.
const CARDIAC: Array<[string, string]> = [
  ['Troponin (ng/mL)', 'troponin_ng_ml'],
  ['BNP (pg/mL)', 'bnp_pg_ml'],
];

const DIAGNOSTIC: Array<[string, string]> = [
  ['Pulmonary Function Test Normal', 'pulmonary_function_test_result'],
  ['Bone Imaging Normal', 'bone_imaging_result'],
];

export default function LabsTab({ formData, onChange }: Props) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors, loading } = useWritableFields(personId);
  const [date, setDate] = useState(today());

  const section = (
    title: string,
    fields: Array<[string, string]>,
    type: 'number' | 'boolean' = 'number',
  ) => (
    <Section title={title}>
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
        {fields.map(([label, name]) => (
          <ClinicalField
            key={name}
            label={label}
            name={name}
            type={type}
            value={formData?.[name]}
            descriptor={descriptors[name]}
            onChange={onChange}
            date={date}
            onDateChange={setDate}
          />
        ))}
      </div>
    </Section>
  );

  return (
    <div>
      {!loading && (
        <p className="mb-4 text-xs text-muted-foreground">
          Lab values are stored as OMOP measurements. Editing one records a new
          result dated below and re-derives the record; a field without an editable
          box explains why underneath it.
        </p>
      )}
      {section('Chemistry Panel', CHEMISTRY)}
      {section('Liver Function', LIVER)}
      {section('Coagulation', COAGULATION)}
      {section('Other Markers', MARKERS)}
      {section('Cardiac', CARDIAC)}
      {section('Tumor Markers', TUMOR_MARKERS)}
      {section('Diagnostic Tests', DIAGNOSTIC, 'boolean')}
    </div>
  );
}
