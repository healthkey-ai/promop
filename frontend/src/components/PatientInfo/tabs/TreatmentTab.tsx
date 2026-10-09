import { useEffect, useState } from 'react';
import { Pencil, Plus } from 'lucide-react';
import { useWritableFields, invalidateWritableFieldsCache } from '@/hooks/useWritableFields';
import { listTherapyRegimens, type EditableTherapyLine } from '@/api/therapyLines';
import type { TherapyRegimen } from '@/types/therapy';
import ClinicalField from '../ClinicalField';
import Section from '../Section';
import TherapyLineDialog from '../TherapyLineDialog';
import SupportiveTherapyDialog from '../SupportiveTherapyDialog';
import type { SupportiveTherapyCourse } from '@/api/supportiveTherapies';

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
  diseaseType: 'breast' | 'lymphoma' | 'myeloma' | 'cll' | 'mcl' | 'other';
  /** Receives the re-derived record after a line is authored. Without it the tab
   *  still writes correctly but shows stale values until the next refetch. */
  onRecordRefreshed?: (patientInfo: Record<string, unknown>) => void;
}

type TherapyDialogState =
  | { mode: 'add' }
  | { mode: 'edit'; line: EditableTherapyLine };

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
    if (['c3242', 'c3209', 'c9335', 'c2987', 'mcl', 'dlbcl'].includes(d)) return disease.toUpperCase();
    if (d.includes('mantle')) return 'MCL';
    if (d.includes('follicular')) return 'C3209';
    if (d.includes('myeloma') || d === 'mm') return 'C3242';
    if (d.includes('cll') || d.includes('chronic lymphocytic') || d.includes('chronic lymphoid')) return 'C2987';
    if (d.includes('breast')) return 'C9335';
    if (d.includes('diffuse large b-cell') || d.includes('dlbcl')) return 'DLBCL';
  }
  // Fall back to the type-safe diseaseType prop.
  // 'lymphoma' is intentionally omitted — it groups MCL and FL, and picking
  // one code would show the wrong regimens for the other. Without a raw
  // disease string the picker falls back to "search all regimens".
  const TYPE_TO_CODE: Record<string, string> = {
    myeloma: 'C3242',
    cll: 'C2987',
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

  const [supportiveDialog, setSupportiveDialog] = useState<{ course?: SupportiveTherapyCourse } | null>(null);
  const supportiveCourses = (formData.supportive_therapy_courses ?? []) as SupportiveTherapyCourse[];

  const field = (label: string, name: string, type: 'text' | 'number' | 'date') => (
    <ClinicalField
      label={label}
      name={name}
      type={type}
      value={formData?.[name]}
      descriptor={descriptors[name]}
      onChange={onChange}
    />
  );

  const linesCount = (() => {
    const v = String(formData?.therapy_lines_count ?? '');
    if (v === '3+') return 3;
    return parseInt(v) || 0;
  })();

  const therapyLines = Array.isArray(formData?.lines_of_therapy)
    ? (formData.lines_of_therapy as EditableTherapyLine[])
    : [];

  // Planned therapy regimen picker: load regimens for the patient's disease + next line.
  const diseaseCode = diseaseToDiseaseCode(formData?.disease, diseaseType);
  const nextLine = Math.max(linesCount, ...therapyLines.map((line) => line.line)) + 1;
  const nextRound = nextLine === 1 ? 'first_line_therapy'
    : nextLine === 2 ? 'second_line_therapy'
    : 'later_line_therapy';
  const [plannedRegimens, setPlannedRegimens] = useState<TherapyRegimen[]>([]);
  const [loadingPlanned, setLoadingPlanned] = useState(false);

  const [plannedError, setPlannedError] = useState('');
  useEffect(() => {
    let active = true;
    const load = async () => {
      setPlannedRegimens([]);
      setPlannedError('');
      setLoadingPlanned(false);
      if (!diseaseCode) return;
      setLoadingPlanned(true);
      try {
        const items = await listTherapyRegimens(diseaseCode, nextRound);
        if (active) setPlannedRegimens(items);
      } catch {
        if (active) setPlannedError('Could not load planned therapies. Reopen this tab to retry.');
      } finally { if (active) setLoadingPlanned(false); }
    };
    void load();
    return () => { active = false; };
  }, [diseaseCode, nextRound]);
  const plannedValue = String(formData.planned_therapies ?? '');


  return (
    <div>
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
                      {line.intent && (
                        <span className="ml-2 font-normal text-portal-text-secondary">
                          {line.intent}
                        </span>
                      )}
                      {line.outcome && (
                        <span className="ml-2 inline-block rounded bg-muted px-1.5 py-0.5 text-xs font-normal">
                          {line.outcome}
                        </span>
                      )}
                    </p>
                    <p className="truncate text-xs text-portal-text-secondary">
                      {line.start_date || 'No start date'}
                      {line.end_date ? ` to ${line.end_date}` : ' to present'}
                      {line.discontinuation_reason && (
                        <span className="ml-2">Reason: {line.discontinuation_reason}</span>
                      )}
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
          defaultLineNumber={nextLine}
          line={dialogState.mode === 'edit' ? dialogState.line : undefined}
          diseaseCode={diseaseToDiseaseCode(formData?.disease, diseaseType)}
          onClose={() => setDialogState(null)}
          onAuthored={(info) => {
            invalidateWritableFieldsCache(personId);
            onRecordRefreshed?.(info);
          }}
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

      <Section title="Supportive Therapy">
        {personId !== null && <button type="button" onClick={() => setSupportiveDialog({})}
          className="mb-3 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted">
          <Plus size={14} /> Add supportive therapy
        </button>}
        {supportiveCourses.length > 0 ? <ul className="divide-y divide-border rounded-md border border-border">
          {supportiveCourses.map((course) => <li key={course.id} className="flex items-center gap-3 px-3 py-2 text-sm">
            <div className="flex-1">
              <p className="font-medium">{course.regimen_title}{course.intent && <span className="ml-2 font-normal">{course.intent}</span>}</p>
              <p className="text-xs text-muted-foreground">{course.start_date || 'No start date'} to {course.end_date || 'present'}{course.discontinuation_reason && ` · Reason: ${course.discontinuation_reason}`}</p>
            </div>
            <button type="button" aria-label={`Edit ${course.regimen_title}`} onClick={() => setSupportiveDialog({ course })} className="rounded-md border px-2.5 py-1.5">Edit</button>
          </li>)}
        </ul> : <p className="text-sm text-muted-foreground">{String(formData.supportive_therapies || 'No supportive therapies recorded.')}</p>}
      </Section>
      {supportiveDialog && personId !== null && <SupportiveTherapyDialog
        personId={personId} diseaseCode={diseaseCode} course={supportiveDialog.course}
        onClose={() => setSupportiveDialog(null)} onSaved={(info) => onRecordRefreshed?.(info)}
      />}

      <Section title="Planned Therapies">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <label htmlFor="planned_therapies" className="mb-1 block text-sm font-medium">Planned Therapies</label>
            <select id="planned_therapies" value={plannedValue}
              disabled={loadingPlanned || !diseaseCode || !!plannedError || !descriptors.planned_therapies?.writable}
              onChange={(e) => onChange('planned_therapies', e.target.value || null)}
              className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm disabled:opacity-50">
              <option value="">{loadingPlanned ? 'Loading regimens…' : 'Select a regimen…'}</option>
              {plannedValue && !plannedRegimens.some((r) => r.title === plannedValue) && <option value={plannedValue}>{plannedValue}</option>}
              {plannedRegimens.map((r) => <option key={r.code} value={r.title}>{r.title}</option>)}
            </select>
            {plannedError && <p role="alert" className="text-sm text-red-700">{plannedError}</p>}
            {!diseaseCode && <p className="text-xs text-muted-foreground">Select a disease to see available therapies.</p>}
            {diseaseCode && !loadingPlanned && !plannedError && plannedRegimens.length === 0 && <p className="text-xs text-muted-foreground">No regimens are available for the next line.</p>}

          </div>
        </div>
      </Section>
    </div>
  );
}
