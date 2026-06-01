import { useVocabulary } from '@/hooks/useVocabulary';
import Field from '../Field';
import Section from '../Section';
import {
  REFRACTORY_STATUS_OPTIONS, THERAPY_INTENT_OPTIONS,
  DISCONTINUATION_REASON_OPTIONS, THERAPY_OUTCOME_OPTIONS, SUPPORTIVE_THERAPIES_OPTIONS,
  PLANNED_THERAPIES,
  BREAST_CANCER_FIRST_LINE, BREAST_CANCER_SECOND_LINE, BREAST_CANCER_LATER_LINE,
  LYMPHOMA_FIRST_LINE, LYMPHOMA_SECOND_LINE, LYMPHOMA_LATER_LINE,
  MYELOMA_FIRST_LINE, MYELOMA_SECOND_LINE, MYELOMA_LATER_LINE,
  CLL_FIRST_LINE, CLL_SECOND_LINE, CLL_LATER_LINE,
} from '../patientConstants';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'other';
}

function getTherapyOptions(diseaseType: string, line: 'first' | 'second' | 'later', bcFirst: { value: string }[], bcSecond: { value: string }[], bcLater: { value: string }[]) {
  switch (diseaseType) {
    case 'breast':
      if (line === 'first') return bcFirst.length ? bcFirst.map((o) => o.value) : BREAST_CANCER_FIRST_LINE;
      if (line === 'second') return bcSecond.length ? bcSecond.map((o) => o.value) : BREAST_CANCER_SECOND_LINE;
      return bcLater.length ? bcLater.map((o) => o.value) : BREAST_CANCER_LATER_LINE;
    case 'lymphoma':
      if (line === 'first') return LYMPHOMA_FIRST_LINE;
      if (line === 'second') return LYMPHOMA_SECOND_LINE;
      return LYMPHOMA_LATER_LINE;
    case 'myeloma':
      if (line === 'first') return MYELOMA_FIRST_LINE;
      if (line === 'second') return MYELOMA_SECOND_LINE;
      return MYELOMA_LATER_LINE;
    case 'cll':
      if (line === 'first') return CLL_FIRST_LINE;
      if (line === 'second') return CLL_SECOND_LINE;
      return CLL_LATER_LINE;
    default:
      return ['Other'];
  }
}

export default function TreatmentTab({ formData, onChange, diseaseType }: Props) {
  const { options: bcFirstLineOptions, source: bcFirstLineSource }   = useVocabulary('breast-cancer-first-line-therapy', 'title');
  const { options: bcSecondLineOptions, source: bcSecondLineSource } = useVocabulary('breast-cancer-second-line-therapy', 'title');
  const { options: bcLaterLineOptions, source: bcLaterLineSource }   = useVocabulary('breast-cancer-later-line-therapy', 'title');

  const breastSource = diseaseType === 'breast';

  const linesCount = (() => {
    const v = String(formData?.therapy_lines_count ?? '');
    if (v === '3+') return 3;
    return parseInt(v) || 0;
  })();

  return (
    <div>
      <Section title="Treatment History">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Number of Prior Lines" name="therapy_lines_count" type="select"
            value={formData?.therapy_lines_count}
            options={['0', '1', '2', '3+']}
            onChange={onChange} />
          <Field label="Relapse Count" name="relapse_count" type="number"
            value={formData?.relapse_count} onChange={onChange} />
          <div className="sm:col-span-2">
            <Field label="Refractory Status" name="refractory_status" type="select"
              value={formData?.refractory_status} options={REFRACTORY_STATUS_OPTIONS} onChange={onChange} />
          </div>
        </div>
      </Section>

      {linesCount >= 1 && <Section title="First Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="First Line Therapy" name="first_line_therapy" type="select"
              value={formData?.first_line_therapy}
              options={getTherapyOptions(diseaseType, 'first', bcFirstLineOptions, bcSecondLineOptions, bcLaterLineOptions)}
              onChange={onChange}
              vocabSource={breastSource ? bcFirstLineSource : null} />
          </div>
          <Field label="First Line Start Date" name="first_line_start_date" type="date" value={formData?.first_line_start_date} onChange={onChange} />
          <Field label="First Line End Date" name="first_line_end_date" type="date" value={formData?.first_line_end_date} onChange={onChange} />
          <Field label="Therapy Intent" name="first_line_intent" type="select" value={formData?.first_line_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
          <Field label="Reason for Discontinuation" name="first_line_discontinuation_reason" type="select" value={formData?.first_line_discontinuation_reason} options={DISCONTINUATION_REASON_OPTIONS} onChange={onChange} />
          <Field label="First Line Outcome" name="first_line_outcome" type="select" value={formData?.first_line_outcome} options={THERAPY_OUTCOME_OPTIONS} onChange={onChange} />
        </div>
      </Section>}

      {linesCount >= 2 && <Section title="Second Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Second Line Therapy" name="second_line_therapy" type="select"
              value={formData?.second_line_therapy}
              options={getTherapyOptions(diseaseType, 'second', bcFirstLineOptions, bcSecondLineOptions, bcLaterLineOptions)}
              onChange={onChange}
              vocabSource={breastSource ? bcSecondLineSource : null} />
          </div>
          <Field label="Second Line Start Date" name="second_line_start_date" type="date" value={formData?.second_line_start_date} onChange={onChange} />
          <Field label="Second Line End Date" name="second_line_end_date" type="date" value={formData?.second_line_end_date} onChange={onChange} />
          <Field label="Therapy Intent" name="second_line_intent" type="select" value={formData?.second_line_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
          <Field label="Reason for Discontinuation" name="second_line_discontinuation_reason" type="select" value={formData?.second_line_discontinuation_reason} options={DISCONTINUATION_REASON_OPTIONS} onChange={onChange} />
          <Field label="Second Line Outcome" name="second_line_outcome" type="select" value={formData?.second_line_outcome} options={THERAPY_OUTCOME_OPTIONS} onChange={onChange} />
        </div>
      </Section>}

      {linesCount >= 3 && <Section title="Later Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Later Line Therapy" name="later_therapy" type="select"
              value={formData?.later_therapy}
              options={getTherapyOptions(diseaseType, 'later', bcFirstLineOptions, bcSecondLineOptions, bcLaterLineOptions)}
              onChange={onChange}
              vocabSource={breastSource ? bcLaterLineSource : null} />
          </div>
          <Field label="Later Line Start Date" name="later_start_date" type="date" value={formData?.later_start_date} onChange={onChange} />
          <Field label="Later Line End Date" name="later_end_date" type="date" value={formData?.later_end_date} onChange={onChange} />
          <Field label="Therapy Intent" name="later_intent" type="select" value={formData?.later_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
          <Field label="Reason for Discontinuation" name="later_discontinuation_reason" type="select" value={formData?.later_discontinuation_reason} options={DISCONTINUATION_REASON_OPTIONS} onChange={onChange} />
          <Field label="Later Line Outcome" name="later_outcome" type="select" value={formData?.later_outcome} options={THERAPY_OUTCOME_OPTIONS} onChange={onChange} />
        </div>
      </Section>}

      <Section title="Supportive Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Supportive Therapy Start Date" name="supportive_therapy_start_date" type="date" value={formData?.supportive_therapy_start_date} onChange={onChange} />
          <Field label="Supportive Therapy End Date" name="supportive_therapy_end_date" type="date" value={formData?.supportive_therapy_end_date} onChange={onChange} />
          <Field label="Supportive Therapies" name="supportive_therapies" type="multiselect" value={formData?.supportive_therapies} options={SUPPORTIVE_THERAPIES_OPTIONS} onChange={onChange} />
          <Field label="Supportive Therapy Intent" name="supportive_therapy_intent" type="select" value={formData?.supportive_therapy_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
        </div>
      </Section>

      <Section title="Planned Therapies">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Planned Therapies" name="planned_therapies" type="select" value={formData?.planned_therapies} options={PLANNED_THERAPIES} onChange={onChange} />
          </div>
        </div>
      </Section>
    </div>
  );
}
