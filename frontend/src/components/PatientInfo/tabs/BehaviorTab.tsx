import Field from '../Field';
import Section from '../Section';
import {
  SMOKING_STATUS_OPTIONS, ALCOHOL_USE_OPTIONS, EXERCISE_FREQUENCY_OPTIONS,
  DIET_TYPE_OPTIONS, SLEEP_QUALITY_OPTIONS, STRESS_LEVEL_OPTIONS, SOCIAL_SUPPORT_OPTIONS,
  EMPLOYMENT_STATUS_OPTIONS, EDUCATION_LEVEL_OPTIONS, MARITAL_STATUS_OPTIONS, INSURANCE_TYPE_OPTIONS,
} from '../patientConstants';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
}

export default function BehaviorTab({ formData, onChange }: Props) {
  return (
    <div>
      <Section title="Lifestyle Factors">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Smoking Status" name="smoking_status" type="select" value={formData?.smoking_status} options={SMOKING_STATUS_OPTIONS} onChange={onChange} />
          <Field label="Pack Years (if applicable)" name="pack_years" type="number" value={formData?.pack_years} onChange={onChange} />
          <Field label="Alcohol Use" name="alcohol_use" type="select" value={formData?.alcohol_use} options={ALCOHOL_USE_OPTIONS} onChange={onChange} />
          <Field label="Drinks per Week (if applicable)" name="drinks_per_week" type="number" value={formData?.drinks_per_week} onChange={onChange} />
          <Field label="Exercise Frequency" name="exercise_frequency" type="select" value={formData?.exercise_frequency} options={EXERCISE_FREQUENCY_OPTIONS} onChange={onChange} />
          <Field label="Exercise Minutes per Week" name="exercise_minutes_per_week" type="number" value={formData?.exercise_minutes_per_week} onChange={onChange} />
          <Field label="Diet Type" name="diet_type" type="select" value={formData?.diet_type} options={DIET_TYPE_OPTIONS} onChange={onChange} />
        </div>
      </Section>

      <Section title="Sleep &amp; Wellbeing">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Average Sleep Hours per Night" name="sleep_hours_per_night" type="number" value={formData?.sleep_hours_per_night} onChange={onChange} />
          <Field label="Sleep Quality" name="sleep_quality" type="select" value={formData?.sleep_quality} options={SLEEP_QUALITY_OPTIONS} onChange={onChange} />
          <Field label="Stress Level" name="stress_level" type="select" value={formData?.stress_level} options={STRESS_LEVEL_OPTIONS} onChange={onChange} />
          <Field label="Social Support" name="social_support" type="select" value={formData?.social_support} options={SOCIAL_SUPPORT_OPTIONS} onChange={onChange} />
        </div>
      </Section>

      <Section title="Socioeconomic Factors">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Employment Status" name="employment_status" type="select" value={formData?.employment_status} options={EMPLOYMENT_STATUS_OPTIONS} onChange={onChange} />
          <Field label="Education Level" name="education_level" type="select" value={formData?.education_level} options={EDUCATION_LEVEL_OPTIONS} onChange={onChange} />
          <Field label="Marital Status" name="marital_status" type="select" value={formData?.marital_status} options={MARITAL_STATUS_OPTIONS} onChange={onChange} />
          <Field label="Insurance Type" name="insurance_type" type="select" value={formData?.insurance_type} options={INSURANCE_TYPE_OPTIONS} onChange={onChange} />
          <Field label="Number of Dependents" name="number_of_dependents" type="number" value={formData?.number_of_dependents} onChange={onChange} />
          <Field label="Annual Household Income (USD)" name="annual_household_income" type="number" value={formData?.annual_household_income} onChange={onChange} />
        </div>
      </Section>

      <Section title="Reproductive Health">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Pregnancy Test Date" name="pregnancy_test_date" type="date" value={formData?.pregnancy_test_date} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Pregnancy Test Result" name="pregnancy_test_result_value" type="text" value={formData?.pregnancy_test_result_value} onChange={onChange} />
          </div>
          <Field label="Using Contraceptives" name="contraceptive_use" type="boolean" value={formData?.contraceptive_use} onChange={onChange} />
        </div>
      </Section>

      <Section title="Consent and Care Support">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Ability to Consent" name="consent_capability" type="boolean" value={formData?.consent_capability} onChange={onChange} />
          <Field label="Availability of Caregiver" name="caregiver_availability_status" type="boolean" value={formData?.caregiver_availability_status} onChange={onChange} />
        </div>
      </Section>

      <Section title="Mental Health and Substance Use">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Mental Health Disorders" name="no_mental_health_disorder_status" type="boolean" value={formData?.no_mental_health_disorder_status} onChange={onChange} />
          <Field label="Non-prescription Recreational Drug Use" name="no_substance_use_status" type="boolean" value={formData?.no_substance_use_status} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Substance Use Details" name="substance_use_details" type="text" value={formData?.substance_use_details} onChange={onChange} />
          </div>
        </div>
      </Section>

      <Section title="Environmental and Occupational Risk">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Geographic/Occupational/Environmental/Infectious Disease Exposure Risk" name="no_geographic_exposure_risk" type="boolean" value={formData?.no_geographic_exposure_risk} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Exposure Risk Details" name="geographic_exposure_risk_details" type="text" value={formData?.geographic_exposure_risk_details} onChange={onChange} />
          </div>
        </div>
      </Section>
    </div>
  );
}
