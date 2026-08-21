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
 * Blood counts and chemistry, rendered against the server's writable-field
 * descriptor.
 *
 * Every field here was already mapped server-side — the server has known the OMOP
 * fact behind `anc_thousand_per_ul` for some time. What it could not do was save:
 * the tab PATCHed `PatientRecord`, which owns no writable clinical column and
 * refuses. A mapping and a write path are independent halves, and this supplies
 * the second.
 *
 * Fourteen of these analytes also appear on the Labs tab. That duplication
 * predates the descriptor — Labs used alias field names (`serum_sodium`) where
 * this tab used canonicals (`sodium_meq_l`), so the same analyte looked like two
 * different fields. Both now write the same LOINC code, which is correct but
 * means one value is editable in two places. Which tab should own them is a
 * product decision, not one to settle here.
 */

const COUNTS: Array<[string, string]> = [
  ['Hemoglobin (g/dL)', 'hemoglobin_g_dl'],
  ['Hematocrit (%)', 'hematocrit_percent'],
  ['WBC Count (10³/µL)', 'wbc_count_thousand_per_ul'],
  ['RBC Count (10⁶/µL)', 'rbc_million_per_ul'],
  ['Platelet Count (10³/µL)', 'platelet_count_thousand_per_ul'],
  ['ANC (10³/µL)', 'anc_thousand_per_ul'],
  ['ALC (10³/µL)', 'alc_thousand_per_ul'],
  ['AMC (10³/µL)', 'amc_thousand_per_ul'],
];

const ELECTROLYTES: Array<[string, string]> = [
  ['Sodium (mEq/L)', 'sodium_meq_l'],
  ['Potassium (mEq/L)', 'potassium_meq_l'],
  // The canonical column. calcium_mg_dl is its alias — populated during
  // derivation, owning no LOINC code of its own, and so not editable. Showing
  // the alias here rendered a read-only box pointing at a field the user could
  // not reach.
  ['Calcium (mg/dL)', 'serum_calcium_mg_dl'],
  ['Magnesium (mg/dL)', 'magnesium_mg_dl'],
];

const CARDIAC_OTHER: Array<[string, string]> = [
  ['Troponin (ng/mL)', 'troponin_ng_ml'],
  ['BNP (pg/mL)', 'bnp_pg_ml'],
  ['Glucose (mg/dL)', 'glucose_mg_dl'],
  ['HbA1c (%)', 'hba1c_percent'],
  ['LDH (U/L)', 'ldh_u_l'],
];

const COAGULATION: Array<[string, string]> = [
  ['INR', 'inr'],
  ['PT (seconds)', 'pt_seconds'],
  ['PTT (seconds)', 'ptt_seconds'],
];

const TUMOR_MARKERS: Array<[string, string]> = [
  ['CEA (ng/mL)', 'cea_ng_ml'],
  ['CA 19-9 (U/mL)', 'ca19_9_u_ml'],
  ['PSA (ng/mL)', 'psa_ng_ml'],
];

export default function BloodTab({ formData, onChange }: Props) {
  const { descriptors, loading } = useWritableFields();
  const [date, setDate] = useState(today());

  const section = (title: string, fields: Array<[string, string]>) => (
    <Section title={title}>
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
        {fields.map(([label, name]) => (
          <ClinicalField
            key={name}
            label={label}
            name={name}
            type="number"
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
          These values are stored as OMOP measurements. Editing one records a new
          result dated below and re-derives the record; a field without an editable
          box explains why underneath it.
        </p>
      )}
      {section('Blood Counts', COUNTS)}
      {section('Electrolytes', ELECTROLYTES)}
      {section('Cardiac & Other', CARDIAC_OTHER)}
      {section('Coagulation', COAGULATION)}
      {section('Tumor Markers', TUMOR_MARKERS)}
    </div>
  );
}
