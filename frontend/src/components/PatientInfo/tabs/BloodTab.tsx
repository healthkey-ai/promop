import React from 'react';
import Field from '../Field';
import Section from '../Section';

interface Props {
  formData: any;
  onChange: (field: string, value: any) => void;
}

export default function BloodTab({ formData, onChange }: Props) {
  return (
    <div>
      <Section title="Blood Counts">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Hemoglobin (g/dL)" name="hemoglobin_g_dl" type="number" value={formData?.hemoglobin_g_dl} onChange={onChange} />
          <Field label="Hematocrit (%)" name="hematocrit_percent" type="number" value={formData?.hematocrit_percent} onChange={onChange} />
          <Field label="WBC Count (10³/µL)" name="wbc_count_thousand_per_ul" type="number" value={formData?.wbc_count_thousand_per_ul} onChange={onChange} />
          <Field label="RBC Count (10⁶/µL)" name="rbc_million_per_ul" type="number" value={formData?.rbc_million_per_ul} onChange={onChange} />
          <Field label="Platelet Count (10³/µL)" name="platelet_count_thousand_per_ul" type="number" value={formData?.platelet_count_thousand_per_ul} onChange={onChange} />
          <Field label="ANC (10³/µL)" name="anc_thousand_per_ul" type="number" value={formData?.anc_thousand_per_ul} onChange={onChange} />
          <Field label="ALC (10³/µL)" name="alc_thousand_per_ul" type="number" value={formData?.alc_thousand_per_ul} onChange={onChange} />
          <Field label="AMC (10³/µL)" name="amc_thousand_per_ul" type="number" value={formData?.amc_thousand_per_ul} onChange={onChange} />
        </div>
      </Section>

      <Section title="Electrolytes">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Sodium (mEq/L)" name="sodium_meq_l" type="number" value={formData?.sodium_meq_l} onChange={onChange} />
          <Field label="Potassium (mEq/L)" name="potassium_meq_l" type="number" value={formData?.potassium_meq_l} onChange={onChange} />
          <Field label="Calcium (mg/dL)" name="calcium_mg_dl" type="number" value={formData?.calcium_mg_dl} onChange={onChange} />
          <Field label="Magnesium (mg/dL)" name="magnesium_mg_dl" type="number" value={formData?.magnesium_mg_dl} onChange={onChange} />
        </div>
      </Section>

      <Section title="Cardiac &amp; Other">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Troponin (ng/mL)" name="troponin_ng_ml" type="number" value={formData?.troponin_ng_ml} onChange={onChange} />
          <Field label="BNP (pg/mL)" name="bnp_pg_ml" type="number" value={formData?.bnp_pg_ml} onChange={onChange} />
          <Field label="Glucose (mg/dL)" name="glucose_mg_dl" type="number" value={formData?.glucose_mg_dl} onChange={onChange} />
          <Field label="HbA1c (%)" name="hba1c_percent" type="number" value={formData?.hba1c_percent} onChange={onChange} />
          <Field label="LDH (U/L)" name="ldh_u_l" type="number" value={formData?.ldh_u_l} onChange={onChange} />
        </div>
      </Section>

      <Section title="Coagulation">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="INR" name="inr" type="number" value={formData?.inr} onChange={onChange} />
          <Field label="PT (seconds)" name="pt_seconds" type="number" value={formData?.pt_seconds} onChange={onChange} />
          <Field label="PTT (seconds)" name="ptt_seconds" type="number" value={formData?.ptt_seconds} onChange={onChange} />
        </div>
      </Section>

      <Section title="Tumor Markers">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="CEA (ng/mL)" name="cea_ng_ml" type="number" value={formData?.cea_ng_ml} onChange={onChange} />
          <Field label="CA 19-9 (U/mL)" name="ca19_9_u_ml" type="number" value={formData?.ca19_9_u_ml} onChange={onChange} />
          <Field label="PSA (ng/mL)" name="psa_ng_ml" type="number" value={formData?.psa_ng_ml} onChange={onChange} />
        </div>
      </Section>
    </div>
  );
}
