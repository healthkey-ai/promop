import { useState } from 'react';
import { Pencil, Plus } from 'lucide-react';
import { useWritableFields, type FieldDescriptor } from '@/hooks/useWritableFields';
import type { EditableTherapyLine } from '@/api/therapyLines';
import ClinicalField from '../ClinicalField';
import Section from '../Section';
import TherapyLineDialog from '../TherapyLineDialog';

interface LaterTherapy {
  therapy: string;
  startDate?: string | null;
  endDate?: string | null;
  lineNumber?: number | null;
}

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'other';
  /** Receives the re-derived record after a line is authored. Without it the tab
   *  still writes correctly but shows stale values until the next refetch. */
  onRecordRefreshed?: (patientInfo: Record<string, unknown>) => void;
}

type TherapyDialogState =
  | { mode: 'add' }
  | { mode: 'edit'; line: EditableTherapyLine };

/**
 * Therapy history, rendered from the writable-field descriptor.
 *
 * Every field on this tab is read-only, which makes it different from Blood and
 * Labs. Those hold analytes with a LOINC code each, so an edit is one
 * Measurement. Nothing here is a single fact: a line of therapy is an Episode
 * grouping the drug exposures given in it, and `first_line_therapy`,
 * `therapy_lines_count`, `refractory_status` and the rest are all read back out
 * of that grouping by regimen inference.
 *
 * So the tab used to offer twenty-six inputs — selects over regimen lists, date
 * pickers, an outcome dropdown — that could not save. Every one returned
 *
 *   405 Only projection-owned PatientRecord fields are writable
 *
 * or its OMOP-mapped sibling. The vocabulary-backed regimen option lists are
 * gone with them: a picker whose selection is discarded is worse than no picker,
 * because it looks like it worked.
 *
 * Twenty-five of the fields carry the same reason, so it is stated once at the
 * top with the authoring steps the server supplies, rather than printed beside
 * each box.
 */

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

/**
 * What to do instead of typing here, taken from the descriptor rather than
 * written out locally — the steps name concept ids and endpoints, and a copy in
 * the UI would go stale against the server that actually enforces them.
 */
function HowToAuthor({ descriptor }: { descriptor?: FieldDescriptor }) {
  const via = descriptor?.authored_via;
  return (
    <div className="mb-5 rounded-md border border-border bg-muted/40 px-4 py-3">
      <p className="text-xs text-muted-foreground">
        {descriptor?.reason
          ?? 'Therapy fields are derived from the treatment episodes on record.'}
      </p>
      {Array.isArray(via?.steps) && via.steps.length > 0 && (
        <>
          <p className="mt-2 text-xs font-medium text-foreground/80">
            To record a line of therapy:
          </p>
          <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
            {via.steps.map((step: string) => <li key={step}>{step}</li>)}
          </ol>
        </>
      )}
    </div>
  );
}

/**
 * Map the patient's disease string to the Disease vocabulary code for API filtering.
 *
 * Uses the raw disease string (not diseaseType) so MCL and FL are distinguishable —
 * both map to diseaseType='lymphoma' but have different Disease codes.
 * Falls back to the broader diseaseType when the raw string is absent.
 */
function diseaseToDiseaseCode(
  disease: unknown,
  diseaseType: Props['diseaseType'],
): string | undefined {
  // Try the raw disease string first for finer discrimination (MCL vs FL).
  if (typeof disease === 'string') {
    const d = disease.toLowerCase();
    if (d.includes('mantle')) return 'MCL';
    if (d.includes('follicular')) return 'C3209';
    if (d.includes('myeloma') || d === 'mm') return 'C3242';
    if (d.includes('cll') || d.includes('chronic lymphocytic')) return 'C2987';
    if (d.includes('breast')) return 'C9335';
  }
  // Fall back to the type-safe diseaseType prop.
  const TYPE_TO_CODE: Record<string, string> = {
    myeloma: 'C3242',
    cll: 'C2987',
    lymphoma: 'C3209',
    breast: 'C9335',
  };
  return TYPE_TO_CODE[diseaseType];
}

export default function TreatmentTab({ formData, onChange, diseaseType, onRecordRefreshed }: Props) {
  // person_id rides in the record the tab already receives, so neither the
  // descriptor nor authoring needs an extra prop threaded through both hosts.
  const personId = Number(formData?.person_id ?? formData?.person ?? 0) || null;

  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const { descriptors } = useWritableFields(personId ?? undefined);
  const [dialogState, setDialogState] = useState<TherapyDialogState | null>(null);

  const field = (label: string, name: string, type: 'text' | 'number' | 'date') => (
    <ClinicalField
      label={label}
      name={name}
      type={type}
      value={formData?.[name]}
      descriptor={descriptors[name]}
      onChange={onChange}
      showReason={false}
    />
  );

  const linesCount = (() => {
    const v = String(formData?.therapy_lines_count ?? '');
    if (v === '3+') return 3;
    return parseInt(v) || 0;
  })();

  const laterTherapies = Array.isArray(formData?.later_therapies)
    ? (formData.later_therapies as LaterTherapy[])
    : [];
  const therapyLines = Array.isArray(formData?.lines_of_therapy)
    ? (formData.lines_of_therapy as EditableTherapyLine[])
    : [];

  return (
    <div>
      <HowToAuthor descriptor={descriptors.first_line_therapy} />

      {personId !== null && (
        <div className="mb-5 space-y-3">
          <button
            onClick={() => setDialogState({ mode: 'add' })}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted"
          >
            <Plus size={14} />
            Add therapy line
          </button>
          {therapyLines.length > 0 && (
            <ul className="divide-y divide-border rounded-md border border-border">
              {therapyLines.map((line) => (
                <li
                  key={`${line.episode_id ?? 'no-episode'}-${line.line}`}
                  className="flex items-center gap-3 px-3 py-2 text-sm"
                >
                  <div className="min-w-0 flex-1">
                    <p className="font-medium text-portal-text-primary">
                      Line {line.line}: {line.regimen || 'Unnamed regimen'}
                    </p>
                    <p className="truncate text-xs text-portal-text-secondary">
                      {line.start_date || 'No start date'}{line.end_date ? ` to ${line.end_date}` : ''}
                      {line.outcome ? ` - ${line.outcome}` : ''}
                    </p>
                  </div>
                  <button
                    type="button"
                    disabled={typeof line.episode_id !== 'number'}
                    onClick={() => setDialogState({ mode: 'edit', line })}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Pencil size={13} />
                    Edit
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {dialogState && personId !== null && (
        <TherapyLineDialog
          personId={personId}
          defaultLineNumber={linesCount + 1}
          line={dialogState.mode === 'edit' ? dialogState.line : undefined}
          diseaseCode={diseaseToDiseaseCode(formData?.disease, diseaseType)}
          onClose={() => setDialogState(null)}
          onAuthored={(info) => onRecordRefreshed?.(info)}
        />
      )}

      <Section title="Treatment History">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Number of Prior Lines', 'therapy_lines_count', 'number')}
          {field('Relapse Count', 'relapse_count', 'number')}
          <div className="sm:col-span-2">
            {field('Refractory Status', 'refractory_status', 'text')}
          </div>
        </div>
      </Section>

      {linesCount >= 1 && <Section title="First Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            {field('First Line Therapy', 'first_line_therapy', 'text')}
          </div>
          <ComponentIds ids={formData?.first_line_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.first_line_therapy_type_ids as number[] | null | undefined} />
          {field('First Line Start Date', 'first_line_start_date', 'date')}
          {field('First Line End Date', 'first_line_end_date', 'date')}
          {field('Therapy Intent', 'first_line_intent', 'text')}
          {field('Reason for Discontinuation', 'first_line_discontinuation_reason', 'text')}
          {field('First Line Outcome', 'first_line_outcome', 'text')}
        </div>
      </Section>}

      {linesCount >= 2 && <Section title="Second Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            {field('Second Line Therapy', 'second_line_therapy', 'text')}
          </div>
          <ComponentIds ids={formData?.second_line_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.second_line_therapy_type_ids as number[] | null | undefined} />
          {field('Second Line Start Date', 'second_line_start_date', 'date')}
          {field('Second Line End Date', 'second_line_end_date', 'date')}
          {field('Therapy Intent', 'second_line_intent', 'text')}
          {field('Reason for Discontinuation', 'second_line_discontinuation_reason', 'text')}
          {field('Second Line Outcome', 'second_line_outcome', 'text')}
        </div>
      </Section>}

      {(linesCount >= 3 || laterTherapies.length > 0) && <Section title="Later Line Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            {field('Later Line Therapy', 'later_therapy', 'text')}
          </div>
          <ComponentIds ids={formData?.later_component_ids as number[] | null | undefined} />
          <TypeClassIds ids={formData?.later_therapy_type_ids as number[] | null | undefined} />
          {field('Later Line Start Date', 'later_start_date', 'date')}
          {field('Later Line End Date', 'later_end_date', 'date')}
          {field('Therapy Intent', 'later_intent', 'text')}
          {field('Reason for Discontinuation', 'later_discontinuation_reason', 'text')}
          {field('Later Line Outcome', 'later_outcome', 'text')}
          {laterTherapies.length > 0 && (
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
          )}
        </div>
      </Section>}

      <Section title="Supportive Therapy">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          {field('Supportive Therapy Start Date', 'supportive_therapy_start_date', 'date')}
          {field('Supportive Therapy End Date', 'supportive_therapy_end_date', 'date')}
          {field('Supportive Therapies', 'supportive_therapies', 'text')}
          {field('Supportive Therapy Intent', 'supportive_therapy_intent', 'text')}
        </div>
      </Section>

      <Section title="Planned Therapies">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            {field('Planned Therapies', 'planned_therapies', 'text')}
          </div>
        </div>
      </Section>
    </div>
  );
}
