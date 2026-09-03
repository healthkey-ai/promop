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

/**
 * Haematology. The counts are the only thing this tab holds that Labs does not.
 *
 * It used to carry Electrolytes, Cardiac & Other, Coagulation and Tumor Markers
 * as well — all four rendered the same field keys as sections already on the
 * Labs tab, so the same value had two editable boxes on two tabs. Labs is where
 * chemistry belongs and where those sections already live; this tab is the
 * blood count.
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

export default function BloodTab({ formData, onChange }: Props) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors, loading } = useWritableFields(personId);
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
    </div>
  );
}
