import { useState } from 'react';
import { useVocabulary } from '@/hooks/useVocabulary';
import { useWritableFields } from '@/hooks/useWritableFields';
import ClinicalField from '../ClinicalField';
import Section from '../Section';
import { Input } from '@/components/shadcn/input';
import { today } from '@/api/clinicalFacts';
import {
  COUNTRY_OPTIONS, US_STATES,
  DISEASE_OPTIONS, STAGE_OPTIONS, HISTOLOGIC_TYPE_OPTIONS,
  ECOG_OPTIONS, KARNOFSKY_OPTIONS,
} from '../patientConstants';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  editedName: string;
  onNameChange: (name: string) => void;
  onZipcodeChange: (zip: string) => void;
  diseaseType?: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'other';
}

/**
 * Demographics, diagnosis summary and vitals, rendered against the descriptor.
 *
 * This tab is the one that spans both write targets. Gender, race, ethnicity and
 * the address live on `Person` and are written by patching it; the vitals and
 * the performance scores are OMOP measurements, written as facts that derivation
 * reads back. `writeFieldValue` routes on the target, so the tab does not need
 * to know which is which — only to stop offering boxes the server refuses.
 *
 * Sixteen of its thirty fields are writable. The rest divide into three honest
 * refusals: twelve are unmapped and have no write path at all yet, `bmi` is
 * computed from height and weight, and `date_of_birth` is fillable only while
 * empty — the persons endpoint never overwrites one, so a box that appeared to
 * accept a correction would lie about the outcome.
 *
 * Where the descriptor carries its own `options` — gender, race, ethnicity —
 * those win over the local constant. They are the curated set the server
 * resolves a concept from, and a longer local list would offer values the write
 * could not code.
 */
export default function GeneralTab({
  formData, onChange, editedName, onNameChange, onZipcodeChange, diseaseType,
}: Props) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors, loading } = useWritableFields(personId);
  const [date, setDate] = useState(today());

  const { source: ecogSource }        = useVocabulary('ecog-status', 'code');
  const { source: karnofskySource }   = useVocabulary('karnofsky-score', 'code');
  const { source: diseaseSource }     = useVocabulary('disease', 'title');
  const { source: cancerStageSource } = useVocabulary('cancer-stage', 'title');
  const { source: ethnicitySource }   = useVocabulary('ethnicity', 'title');
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');

  const histOptions = histologicOptions.length
    ? histologicOptions.map((o: { value: string }) => o.value)
    : HISTOLOGIC_TYPE_OPTIONS;

  const age = formData?.date_of_birth
    ? calculateAge(formData.date_of_birth as string)
    : null;

  /**
   * A measurement is an event, so it needs a date; a Person attribute is not, so
   * it does not. Offering a result date beside a gender picker would suggest the
   * record keeps a history of it, which it does not.
   */
  const field = (
    label: string,
    name: string,
    type: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'email',
    extra: { options?: string[]; vocabSource?: ReturnType<typeof useVocabulary>['source'] } = {},
  ) => {
    const descriptor = descriptors[name];
    const dated = descriptor?.writable && descriptor.target === 'measurement';
    return (
      <ClinicalField
        label={label}
        name={name}
        type={type}
        value={formData?.[name]}
        descriptor={descriptor}
        options={extra.options}
        vocabSource={extra.vocabSource}
        onChange={onChange}
        date={dated ? date : undefined}
        onDateChange={dated ? setDate : undefined}
      />
    );
  };

  return (
    <div>
      {!loading && (
        <p className="mb-4 text-xs text-muted-foreground">
          Demographics are stored on the patient record; vitals and performance
          scores are stored as OMOP measurements, so editing one records a result
          dated below and re-derives the record. A field without an editable box
          explains why underneath it.
        </p>
      )}

      <Section title="Patient Details" description="Basic patient information and demographics.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Patient Name</label>
            <Input value={editedName} onChange={(e) => onNameChange(e.target.value)} />
          </div>

          {field('Date of Birth', 'date_of_birth', 'date')}

          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Age</label>
            <Input value={age ?? ''} disabled readOnly />
            <p className="text-xs text-muted-foreground">
              Calculated from the date of birth.
            </p>
          </div>

          {field('Gender', 'gender', 'select')}

          {field('Email', 'email', 'email')}
          {field('Phone Number', 'phone_number', 'text')}

          <div className="sm:col-span-2">
            {field('Treating Institution', 'facility_name', 'text')}
          </div>
        </div>
      </Section>

      <Section title="Location" description="Patient address and region.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Country', 'country', 'select', { options: COUNTRY_OPTIONS })}

          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Postal Code / Zip Code</label>
            <Input
              value={(formData?.postal_code as string) || ''}
              onChange={(e) => onZipcodeChange(e.target.value)}
              placeholder="Enter 5-digit US zip code to auto-fill city and state"
            />
          </div>

          {field('City', 'city', 'text')}

          {formData?.country === 'United States'
            ? field('State', 'region', 'select', { options: US_STATES })
            : field('Region/State', 'region', 'text')}

          {/* Normally filled in by the zip lookup above, but writable: trial
              matching measures distance to sites from these, so a wrong pair is
              worth being able to correct by hand. */}
          {field('Latitude', 'latitude', 'number')}
          {field('Longitude', 'longitude', 'number')}
        </div>
      </Section>

      <Section title="Clinician Validation" description="Whether a clinician has checked this record.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Validated', 'validated', 'boolean')}
          {field('Validated By', 'validated_by', 'text')}
          {field('Validation Date', 'validation_date', 'date')}
        </div>
      </Section>

      <Section title="Race &amp; Ethnicity" description="Self-reported race and ethnicity (OMB standard categories).">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Race', 'race', 'select')}
          {field('Ethnicity', 'ethnicity', 'select', { vocabSource: ethnicitySource })}
        </div>
      </Section>

      <Section title="Clinical Summary" description="Diagnosis and eligibility-related information.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Disease', 'disease', 'select', { options: DISEASE_OPTIONS, vocabSource: diseaseSource })}
          {field('Stage', 'stage', 'select', { options: STAGE_OPTIONS, vocabSource: cancerStageSource })}

          {(!diseaseType || diseaseType === 'breast' || diseaseType === 'other') && (
            <div className="sm:col-span-2">
              {field('Histologic Type', 'histologic_type', 'select', { options: histOptions, vocabSource: histologicSource })}
            </div>
          )}

          {field('ECOG Performance Status', 'ecog_performance_status', 'select', { options: ECOG_OPTIONS, vocabSource: ecogSource })}
          {field('ECOG Assessment Date', 'ecog_assessment_date', 'date')}
          {field('Karnofsky Performance Score', 'karnofsky_performance_score', 'select', { options: KARNOFSKY_OPTIONS, vocabSource: karnofskySource })}
        </div>
      </Section>

      <Section title="Medical History" description="Pre-existing conditions and clinical status.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            {field('Pre-existing Conditions', 'preexisting_conditions', 'text')}
          </div>
          {field('Peripheral Neuropathy Grade', 'peripheral_neuropathy_grade', 'number')}
          {field('No Other Active Malignancies', 'no_other_active_malignancies', 'boolean')}
          {field('No Active Infection', 'no_active_infection_status', 'boolean')}
        </div>
      </Section>

      <Section title="Infection Status" description="Viral infection history.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('HIV Positive', 'hiv_status', 'boolean')}
          {field('No HIV', 'no_hiv_status', 'boolean')}
          {field('Hepatitis B Positive', 'hepatitis_b_status', 'boolean')}
          {field('No Hepatitis B', 'no_hepatitis_b_status', 'boolean')}
          {field('Hepatitis C Positive', 'hepatitis_c_status', 'boolean')}
          {field('No Hepatitis C', 'no_hepatitis_c_status', 'boolean')}
        </div>
      </Section>

      <Section title="Physical Measurements" description="Body measurements and vital signs.">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Weight (kg)', 'weight', 'number')}
          {field('Height (cm)', 'height', 'number')}
          {field('BMI', 'bmi', 'number')}
          {field('Systolic Blood Pressure (mmHg)', 'systolic_blood_pressure', 'number')}
          {field('Diastolic Blood Pressure (mmHg)', 'diastolic_blood_pressure', 'number')}
          {field('Heart Rate (bpm)', 'heartrate', 'number')}
        </div>
      </Section>
    </div>
  );
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
