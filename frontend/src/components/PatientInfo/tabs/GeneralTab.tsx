import React from 'react';
import { useVocabulary } from '../../../hooks/useVocabulary';
import Field from '../Field';
import Section from '../Section';
import { Input } from '../../shadcn/input';
import {
  GENDER_OPTIONS, COUNTRY_OPTIONS, US_STATES, ETHNICITY_OPTIONS,
  DISEASE_OPTIONS, STAGE_OPTIONS, HISTOLOGIC_TYPE_OPTIONS,
  ECOG_OPTIONS, KARNOFSKY_OPTIONS,
} from '../patientConstants';

interface Props {
  formData: any;
  onChange: (field: string, value: any) => void;
  editedName: string;
  onNameChange: (name: string) => void;
  onZipcodeChange: (zip: string) => void;
}

function calculateAge(dateOfBirth: string): number | null {
  if (!dateOfBirth) return null;
  const today = new Date();
  const birthDate = new Date(dateOfBirth);
  let age = today.getFullYear() - birthDate.getFullYear();
  const m = today.getMonth() - birthDate.getMonth();
  if (m < 0 || (m === 0 && today.getDate() < birthDate.getDate())) age--;
  return age;
}

export default function GeneralTab({ formData, onChange, editedName, onNameChange, onZipcodeChange }: Props) {
  const { source: ecogSource }        = useVocabulary('ecog-status', 'code');
  const { source: karnofskySource }   = useVocabulary('karnofsky-score', 'code');
  const { source: diseaseSource }     = useVocabulary('disease', 'title');
  const { source: cancerStageSource } = useVocabulary('cancer-stage', 'title');
  const { source: ethnicitySource }   = useVocabulary('ethnicity', 'title');
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');

  const age = formData?.date_of_birth ? calculateAge(formData.date_of_birth) : null;
  const histOptions = histologicOptions.length ? histologicOptions.map((o: any) => o.value) : HISTOLOGIC_TYPE_OPTIONS;

  return (
    <div>
      <Section title="Patient Details" description="Basic patient information and demographics.">
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-portal-text-primary">Patient Name</label>
          <Input
            value={editedName}
            onChange={(e) => onNameChange(e.target.value)}
          />
        </div>

        <Field label="Date of Birth" name="date_of_birth" type="date"
          value={formData?.date_of_birth} onChange={onChange} />

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-portal-text-primary">Age</label>
          <Input value={age ?? ''} disabled readOnly />
        </div>

        <Field label="Gender" name="gender" type="select"
          value={formData?.gender} options={GENDER_OPTIONS} onChange={onChange} />

        <div className="sm:col-span-2">
          <Field label="Email" name="email" type="email"
            value={formData?.email} onChange={onChange} />
        </div>
      </div>
      </Section>

      <Section title="Location" description="Patient address and region.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Country" name="country" type="select"
            value={formData?.country} options={COUNTRY_OPTIONS} onChange={onChange} />

          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Postal Code / Zip Code</label>
            <Input
              value={formData?.postal_code || ''}
              onChange={(e) => onZipcodeChange(e.target.value)}
              placeholder="Enter 5-digit US zip code to auto-fill city and state"
            />
          </div>

          <Field label="City" name="city" type="text"
            value={formData?.city} onChange={onChange} />

          {formData?.country === 'United States'
            ? <Field label="State" name="region" type="select"
                value={formData?.region} options={US_STATES} onChange={onChange} />
            : <Field label="Region/State" name="region" type="text"
                value={formData?.region} onChange={onChange} />
          }
        </div>
      </Section>

      <Section title="Ethnicity" description="Self-reported ethnicity and background.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Ethnicity" name="ethnicity" type="select"
              value={formData?.ethnicity} options={ETHNICITY_OPTIONS}
              onChange={onChange} vocabSource={ethnicitySource} />
          </div>
        </div>
      </Section>

      <Section title="Clinical Summary" description="Diagnosis and eligibility-related information.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Disease" name="disease" type="select"
            value={formData?.disease} options={DISEASE_OPTIONS}
            onChange={onChange} vocabSource={diseaseSource} />

          <Field label="Stage" name="stage" type="select"
            value={formData?.stage} options={STAGE_OPTIONS}
            onChange={onChange} vocabSource={cancerStageSource} />

          <div className="sm:col-span-2">
            <Field label="Histologic Type" name="histologic_type" type="select"
              value={formData?.histologic_type} options={histOptions}
              onChange={onChange} vocabSource={histologicSource} />
          </div>

          <Field label="ECOG Performance Status" name="ecog_performance_status" type="select"
            value={formData?.ecog_performance_status} options={ECOG_OPTIONS}
            onChange={onChange} vocabSource={ecogSource} />

          <Field label="ECOG Assessment Date" name="ecog_assessment_date" type="date"
            value={formData?.ecog_assessment_date} onChange={onChange} />

          <Field label="Karnofsky Performance Score" name="karnofsky_performance_score" type="select"
            value={formData?.karnofsky_performance_score} options={KARNOFSKY_OPTIONS}
            onChange={onChange} vocabSource={karnofskySource} />
        </div>
      </Section>

      <Section title="Physical Measurements" description="Body measurements and vital signs.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Weight (kg)" name="weight" type="number"
            value={formData?.weight} onChange={onChange} />
          <Field label="Height (cm)" name="height" type="number"
            value={formData?.height} onChange={onChange} />
          <Field label="BMI" name="bmi" type="number"
            value={formData?.bmi} onChange={onChange} />
          <Field label="Systolic Blood Pressure (mmHg)" name="systolic_blood_pressure" type="number"
            value={formData?.systolic_blood_pressure} onChange={onChange} />
          <Field label="Diastolic Blood Pressure (mmHg)" name="diastolic_blood_pressure" type="number"
            value={formData?.diastolic_blood_pressure} onChange={onChange} />
          <Field label="Heart Rate (bpm)" name="heartrate" type="number"
            value={formData?.heartrate} onChange={onChange} />
        </div>
      </Section>
    </div>
  );
}
