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

interface LaterTherapy {
  therapy: string;
  startDate?: string | null;
  endDate?: string | null;
  lineNumber?: number | null;
}

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  diseaseType: 'breast' | 'lymphoma' | 'mcl' | 'myeloma' | 'cll' | 'other';
}

function getTherapyOptions(diseaseType: string, line: 'first' | 'second' | 'later', bcFirst: { value: string }[], bcSecond: { value: string }[], bcLater: { value: string }[]) {
  switch (diseaseType) {
    case 'breast':
      if (line === 'first') return bcFirst.length ? bcFirst.map((o) => o.value) : BREAST_CANCER_FIRST_LINE;
      if (line === 'second') return bcSecond.length ? bcSecond.map((o) => o.value) : BREAST_CANCER_SECOND_LINE;
      return bcLater.length ? bcLater.map((o) => o.value) : BREAST_CANCER_LATER_LINE;
    case 'lymphoma':
    // MCL reuses the B-cell NHL regimen lists (R-CHOP / BR / rituximab overlap) until a curated
    // mantle-cell list exists — correct-adjacent, and far better than the generic 'Other'.
    case 'mcl':
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

/** Read-only display of a line's HemOnc/RxNorm component drug concept_ids (derived server-side). */
function ComponentIds({ ids }: { ids?: number[] | null }) {
  if (!Array.isArray(ids) || ids.length === 0) return null;
  return (
    <p className="sm:col-span-2 text-xs text-portal-text-secondary -mt-2">
      Component concept IDs: {ids.join(', ')}
    </p>
  );
}

/** Read-only display of a line's therapy-class ("type") concept_ids (derived server-side, ADR 0002). */
function TypeClassIds({ ids }: { ids?: number[] | null }) {
  if (!Array.isArray(ids) || ids.length === 0) return null;
  return (
    <p className="sm:col-span-2 text-xs text-portal-text-secondary -mt-2">
      Therapy type concept IDs: {ids.join(', ')}
    </p>
  );
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

  // '3+' is a UI token for ≥3; server stores an integer. Convert in both directions.
  const rawLines = formData?.therapy_lines_count;
  const displayLines = rawLines == null || rawLines === ''
    ? ''
    : Number(rawLines) >= 3 ? '3+' : String(rawLines);

  function handleLinesChange(_field: string, val: unknown) {
    const num = val === '' || val == null ? null : val === '3+' ? 3 : parseInt(val as string, 10);
    onChange('therapy_lines_count', num);
  }

  return (
    <div>
      <Section title="Treatment History">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field label="Number of Prior Lines" name="therapy_lines_count" type="select"
            value={displayLines}
            options={['0', '1', '2', '3+']}
            onChange={handleLinesChange} />
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
          <ComponentIds ids={formData?.first_line_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.first_line_therapy_type_ids as number[] | null | undefined} />
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
          <ComponentIds ids={formData?.second_line_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.second_line_therapy_type_ids as number[] | null | undefined} />
          <Field label="Second Line Start Date" name="second_line_start_date" type="date" value={formData?.second_line_start_date} onChange={onChange} />
          <Field label="Second Line End Date" name="second_line_end_date" type="date" value={formData?.second_line_end_date} onChange={onChange} />
          <Field label="Therapy Intent" name="second_line_intent" type="select" value={formData?.second_line_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
          <Field label="Reason for Discontinuation" name="second_line_discontinuation_reason" type="select" value={formData?.second_line_discontinuation_reason} options={DISCONTINUATION_REASON_OPTIONS} onChange={onChange} />
          <Field label="Second Line Outcome" name="second_line_outcome" type="select" value={formData?.second_line_outcome} options={THERAPY_OUTCOME_OPTIONS} onChange={onChange} />
        </div>
      </Section>}

      {(linesCount >= 3 || (Array.isArray(formData?.later_therapies) && (formData.later_therapies as LaterTherapy[]).length > 0)) && <Section title="Later Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Field label="Later Line Therapy" name="later_therapy" type="select"
              value={typeof formData?.later_therapy === 'string' ? formData.later_therapy.split(';')[0].trim() : formData?.later_therapy}
              options={getTherapyOptions(diseaseType, 'later', bcFirstLineOptions, bcSecondLineOptions, bcLaterLineOptions)}
              onChange={onChange}
              vocabSource={breastSource ? bcLaterLineSource : null} />
          </div>
          <ComponentIds ids={formData?.later_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.later_therapy_type_ids as number[] | null | undefined} />
          <Field label="Later Line Start Date" name="later_start_date" type="date" value={formData?.later_start_date} onChange={onChange} />
          <Field label="Later Line End Date" name="later_end_date" type="date" value={formData?.later_end_date} onChange={onChange} />
          <Field label="Therapy Intent" name="later_intent" type="select" value={formData?.later_intent} options={THERAPY_INTENT_OPTIONS} onChange={onChange} />
          <Field label="Reason for Discontinuation" name="later_discontinuation_reason" type="select" value={formData?.later_discontinuation_reason} options={DISCONTINUATION_REASON_OPTIONS} onChange={onChange} />
          <Field label="Later Line Outcome" name="later_outcome" type="select" value={formData?.later_outcome} options={THERAPY_OUTCOME_OPTIONS} onChange={onChange} />
          {(() => {
            const laterTherapies = Array.isArray(formData?.later_therapies)
              ? (formData.later_therapies as LaterTherapy[])
              : [];
            if (!laterTherapies.length) return null;
            return (
              <div className="sm:col-span-2">
                <label className="block text-sm font-medium text-portal-text-primary mb-1">All Later Therapy Lines</label>
                <ul className="text-sm text-portal-text-primary space-y-1 pl-1">
                  {laterTherapies.map((t) => (
                    <li key={`${t.startDate ?? ''}-${t.therapy}`} className="flex gap-2">
                      <span className="font-medium">Line {t.lineNumber ?? 3}:</span>
                      <span>{t.therapy}</span>
                      <span className="text-portal-text-secondary">{t.startDate}{t.endDate ? ` – ${t.endDate}` : ''}</span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })()}
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
