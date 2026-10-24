import type { AxiosInstance } from 'axios';
import type { FieldDescriptor } from '@/hooks/useWritableFields';
import { diseaseToDiseaseCode } from '@/api/therapyContext';

export interface FormFieldDescriptor extends FieldDescriptor {
  label?: string;
  control?: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'multiselect' | 'email';
  bindings?: string[];
  compatibility_reason?: string;
  options_source?: 'therapy_regimens';
}
export interface PatientFormSnapshot {
  values: Record<string, unknown>;
  fields: Record<string, FormFieldDescriptor>;
  pending: Record<string, unknown>;
  errors: Record<string, string>;
  personId?: number;
  loading: boolean;
  saving: boolean;
  writable: boolean;
  error?: string;
}
export interface PatientFormResult {
  patient_info: Record<string, unknown>;
  patient_name?: string;
  write: { applied: string[]; rejected?: Record<string, string>; deferred_profile?: string[] };
}

/** One queue per immutable authorization context, not per mounted row/dialog.
 * A SPA unmount only removes listeners: a pending/in-flight batch still drains.
 * This deliberately does not promise delivery after closing the browser tab. */
export class PatientFormStore {
  private snapshot: PatientFormSnapshot = {
    values: {}, fields: {}, pending: {}, errors: {}, loading: true, saving: false, writable: false,
  };
  private listeners = new Set<() => void>();
  private updated = new Set<(result: PatientFormResult) => unknown>();
  private queue: Record<string, unknown> = {};
  private revisions: Record<string, number> = {};
  private revision = 0;
  private explicitRevision = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private flight = false;
  private loading?: Promise<void>;
  private readRevision = 0;

  constructor(private client: AxiosInstance, private base: string) {}

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  onUpdated(listener: (result: PatientFormResult) => unknown) {
    this.updated.add(listener);
    return () => {
      // The host's invalidation must still run when navigation removed its
      // root while a save/follow-up is pending. Retire it only after draining.
      const removeWhenIdle = () => {
        if (this.flight || Object.keys(this.snapshot.pending).length) return false;
        this.updated.delete(listener);
        return true;
      };
      if (!removeWhenIdle()) {
        const stop = this.subscribe(() => { if (removeWhenIdle()) stop(); });
      }
    };
  }
  private publish(next: Partial<PatientFormSnapshot>) {
    this.snapshot = { ...this.snapshot, ...next };
    this.listeners.forEach((listener) => listener());
  }
  resolve(name: string): string | undefined {
    if (name in this.snapshot.fields) return name;
    return Object.keys(this.snapshot.fields).find((key) => this.snapshot.fields[key].bindings?.includes(name));
  }
  /** Merge an explicit server acknowledgement that did not go through the queue. */
  applyServerValues(values: Record<string, unknown>) {
    ++this.explicitRevision;
    this.publish({ values: { ...this.snapshot.values, ...values } });
  }
  load = (): Promise<void> => {
    if (this.loading) return this.loading;
    const revision = ++this.readRevision;
    this.loading = (async () => {
      try {
        const { data } = await this.client.get(`${this.base}/patient-info/me/`);
        const values = { ...data.patient_info, patient_name: data.patient_name };
        const personId = Number(values.person_id ?? values.person);
        if (!personId) throw new Error('No linked patient record.');
        if (this.snapshot.personId && this.snapshot.personId !== personId) {
          throw new Error('The linked patient changed. Start a new patient context.');
        }
        const response = await this.client.get<Record<string, FormFieldDescriptor>>(`${this.base}/v1/patient-records/writable-fields/`, {
          params: { person_id: personId },
          headers: { 'X-Patient-Form': '1' },
        });
        const fields = { ...response.data };
        if (fields.planned_therapies?.options_source === 'therapy_regimens') {
          const disease = diseaseToDiseaseCode(values.disease);
          try {
            if (!disease) throw new Error('Select a disease to see available therapies.');
            const lines = Array.isArray(values.lines_of_therapy) ? values.lines_of_therapy : [];
            const count = values.therapy_lines_count === '3+' ? 3 : parseInt(String(values.therapy_lines_count)) || 0;
            const next = Math.max(count, ...lines.map((line: { line: number }) => line.line)) + 1;
            const round = next === 1 ? 'first_line_therapy' : next === 2 ? 'second_line_therapy' : 'later_line_therapy';
            const regimens = await this.client.get<Array<{ title: string }>>(`${this.base}/v1/therapy-regimens/`, { params: { disease, round } });
            fields.planned_therapies = { ...fields.planned_therapies,
              options: regimens.data.map((regimen) => ({ value: regimen.title })) };
          } catch {
            fields.planned_therapies = { ...fields.planned_therapies, writable: false,
              reason: disease ? 'Could not load planned therapies. Retry loading the profile.' : 'Select a disease to see available therapies.' };
          }
        }
        if (revision !== this.readRevision) return;
        this.publish({ values, fields, personId, loading: false,
          writable: data.write_enabled !== false, error: undefined });
      } catch {
        if (revision === this.readRevision) this.publish({ loading: false, writable: false,
          error: 'Could not load the patient record and field permissions. Please retry.' });
      } finally { this.loading = undefined; }
    })();
    return this.loading;
  };
  enqueue = (name: string, value: unknown) => {
    const field = this.snapshot.fields[name];
    if (!this.snapshot.writable || !field?.writable || field.target !== 'patient_record') return;
    this.queue[name] = value;
    this.revisions[name] = ++this.revision;
    const errors = { ...this.snapshot.errors };
    delete errors[name];
    this.publish({ pending: { ...this.snapshot.pending, [name]: value }, errors });
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush(); }, 500);
  };
  flush = async (): Promise<void> => {
    clearTimeout(this.timer);
    if (this.flight || !Object.keys(this.queue).length || !this.snapshot.personId) return;
    const batch = this.queue;
    const revisions = { ...this.revisions };
    const explicitAtStart = this.explicitRevision;
    this.queue = {};
    this.flight = true;
    this.publish({ saving: true });
    const batchErrors: Record<string, string> = {};
    try {
      const { data } = await this.client.patch<PatientFormResult>(
        `${this.base}/v1/patient-records/${this.snapshot.personId}/`, batch,
        { headers: { 'X-Patient-Form': '1' } },
      );
      if (!data.patient_info || !Array.isArray(data.write?.applied)) {
        throw new Error('The server did not acknowledge the saved fields.');
      }
      // Ignore GETs started before this confirmed write. Never merge submitted
      // values into the baseline: the server's re-derived read model owns it.
      ++this.readRevision;
      const serverValues = { ...data.patient_info };
      // An explicit pre-existing-conditions answer can land while this PATCH is
      // in flight. Its response was built from the older answer; do not let it
      // resurrect what the patient just withdrew.
      if (this.explicitRevision !== explicitAtStart) {
        serverValues.no_pre_existing_conditions = this.snapshot.values.no_pre_existing_conditions;
        serverValues.preexisting_conditions = this.snapshot.values.preexisting_conditions;
      }
      this.publish({ values: { ...serverValues,
        patient_name: data.patient_name ?? this.snapshot.values.patient_name } });
      for (const name of Object.keys(batch)) {
        if (!data.write.applied.includes(name)) batchErrors[name] = data.write.rejected?.[name]
          ?? 'This field was not saved. It may require a different editing workflow.';
      }
      await Promise.allSettled([...this.updated].map((listener) => Promise.resolve().then(() => listener(data))));
    } catch (error) {
      const rejected = (error as { response?: { data?: { rejected?: Record<string, string> } } })
        ?.response?.data?.rejected;
      for (const name of Object.keys(batch)) batchErrors[name] = rejected?.[name]
        ?? 'Could not save this value. Your last saved value has been restored.';
    } finally {
      const pending = { ...this.snapshot.pending };
      const errors = { ...this.snapshot.errors };
      // A stale failure/success must not retire a newer edit to the same field.
      for (const name of Object.keys(batch)) {
        if (this.revisions[name] === revisions[name]) {
          delete pending[name];
          if (batchErrors[name]) errors[name] = batchErrors[name];
          else delete errors[name];
        }
      }
      this.flight = false;
      this.publish({ pending, errors, saving: false });
      if (Object.keys(this.queue).length) await this.flush();
    }
  };
}
