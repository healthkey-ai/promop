import React from 'react';
import Field from '../Field';
import Section from '../Section';

interface Props {
  formData: any;
  onChange: (field: string, value: any) => void;
}

export default function LabsTab({ formData, onChange }: Props) {
  return (
    <div>
      <Section title="Chemistry Panel">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Serum Creatinine (mg/dL)" name="serum_creatinine_level" type="number" value={formData?.serum_creatinine_level} onChange={onChange} />
          <Field label="Creatinine Clearance Rate" name="creatinine_clearance_rate" type="number" value={formData?.creatinine_clearance_rate} onChange={onChange} />
          <Field label="Blood Urea Nitrogen (mg/dL)" name="blood_urea_nitrogen" type="number" value={formData?.blood_urea_nitrogen} onChange={onChange} />
          <Field label="eGFR (mL/min/1.73m²)" name="egfr" type="number" value={formData?.egfr} onChange={onChange} />
          <Field label="Serum Sodium (mEq/L)" name="serum_sodium" type="number" value={formData?.serum_sodium} onChange={onChange} />
          <Field label="Serum Potassium (mEq/L)" name="serum_potassium" type="number" value={formData?.serum_potassium} onChange={onChange} />
          <Field label="Serum Calcium (mg/dL)" name="serum_calcium_level" type="number" value={formData?.serum_calcium_level} onChange={onChange} />
          <Field label="Magnesium (mg/dL)" name="magnesium" type="number" value={formData?.magnesium} onChange={onChange} />
          <Field label="Phosphorus (mg/dL)" name="phosphorus" type="number" value={formData?.phosphorus} onChange={onChange} />
          <Field label="Serum Albumin (g/dL)" name="albumin_level" type="number" value={formData?.albumin_level} onChange={onChange} />
          <Field label="Total Protein (g/dL)" name="total_protein" type="number" value={formData?.total_protein} onChange={onChange} />
        </div>
      </Section>

      <Section title="Liver Function Tests">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="AST (U/L)" name="liver_enzyme_levels_ast" type="number" value={formData?.liver_enzyme_levels_ast} onChange={onChange} />
          <Field label="ALT (U/L)" name="liver_enzyme_levels_alt" type="number" value={formData?.liver_enzyme_levels_alt} onChange={onChange} />
          <Field label="ALP (U/L)" name="liver_enzyme_levels_alp" type="number" value={formData?.liver_enzyme_levels_alp} onChange={onChange} />
          <Field label="Total Bilirubin (mg/dL)" name="serum_bilirubin_level_total" type="number" value={formData?.serum_bilirubin_level_total} onChange={onChange} />
          <Field label="Direct Bilirubin (mg/dL)" name="serum_bilirubin_level_direct" type="number" value={formData?.serum_bilirubin_level_direct} onChange={onChange} />
          <Field label="Albumin (g/dL)" name="albumin_g_dl" type="number" value={formData?.albumin_g_dl} onChange={onChange} />
        </div>
      </Section>

      <Section title="Other Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="LDH (U/L)" name="ldh" type="number" value={formData?.ldh} onChange={onChange} />
          <Field label="Alkaline Phosphatase (U/L)" name="alkaline_phosphatase" type="number" value={formData?.alkaline_phosphatase} onChange={onChange} />
          <Field label="Beta-2 Microglobulin (mg/L)" name="beta2_microglobulin" type="number" value={formData?.beta2_microglobulin} onChange={onChange} />
          <Field label="C-Reactive Protein (mg/L)" name="c_reactive_protein" type="number" value={formData?.c_reactive_protein} onChange={onChange} />
          <Field label="ESR (mm/hr)" name="esr" type="number" value={formData?.esr} onChange={onChange} />
        </div>
      </Section>
    </div>
  );
}
