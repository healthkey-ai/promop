import type { AxiosInstance } from 'axios';
import type { FieldDescriptor } from '@/hooks/useWritableFields';
import { diseaseToDiseaseCode } from '@/api/therapyContext';

/**
 * A dated edit is `{value, date}`: the pair is one clinical assertion, so a
 * stale date is a wrong clinical day rather than a formatting detail.
 */
function isDatedEdit(value: unknown): value is { value: unknown; date: string } {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && 'date' in (value as Record<string, unknown>);
}

const STALE_DATES_ERROR =
  'The result dates could not be refreshed. Retry before saving so this result lands on the right day.';

export interface FormFieldDescriptor extends FieldDescriptor {
  label?: string;
  control?: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'multiselect' | 'email';
  bindings?: string[];
  compatibility_reason?: string;
  options_source?: 'therapy_regimens';
  /** Why a writable picker has no choices; never leave a blank panel unexplained. */
  options_note?: string;
  /** The choices could not be fetched; the editor offers `store.load()` again. */
  retryable?: boolean;
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
  /** A post-save descriptor refresh failed, so `latest_date` may be stale. */
  datesStale?: boolean;
}
export interface PatientFormResult {
  patient_info: Record<string, unknown>;
  patient_name?: string;
  write: { applied: string[]; rejected?: Record<string, string>; deferred_profile?: string[] };
}

/** One queue per immutable authorization context, not per mounted row/dialog. * A SPA unmount only removes listeners: a pending/in-flight batch still drains.
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

  private reloadSequence = 0;
  resolve(name: string): string | undefined {
    if (name in this.snapshot.fields) return name;
    return Object.keys(this.snapshot.fields).find((key) => this.snapshot.fields[key].bindings?.includes(name));
  }
  /** Merge an explicit server acknowledgement that did not go through the queue. */
  applyServerValues(values: Record<string, unknown>) {
    ++this.explicitRevision;
    this.publish({ values: { ...this.snapshot.values, ...values } });
  }
  /** Fetch the host's planned-therapy choices for the current context. */
  private enrichFields = async (
    fields: Record<string, FormFieldDescriptor>,
    values: Record<string, unknown>,
  ): Promise<Record<string, FormFieldDescriptor>> => {
    if (fields.planned_therapies?.options_source !== 'therapy_regimens') return fields;
    const enriched = { ...fields };
    const disease = diseaseToDiseaseCode(values.disease);
    try {
      if (!disease) throw new Error('Select a disease to see available therapies.');
      const lines = Array.isArray(values.lines_of_therapy) ? values.lines_of_therapy : [];
      const count = values.therapy_lines_count === '3+' ? 3 : parseInt(String(values.therapy_lines_count)) || 0;
      const next = Math.max(count, ...lines.map((line: { line: number }) => line.line)) + 1;
      const round = next === 1 ? 'first_line_therapy' : next === 2 ? 'second_line_therapy' : 'later_line_therapy';
      const regimens = await this.client.get<Array<{ title: string; value?: string }>>(
        `${this.base}/v1/therapy-regimens/`, { params: { disease, round } });
      enriched.planned_therapies = { ...enriched.planned_therapies,
        options: regimens.data.map((regimen) => ({
          value: regimen.value ?? regimen.title, label: regimen.title,
        })),
        options_note: regimens.data.length ? undefined
          : 'No planned therapies are available for this disease and treatment line.' };
    } catch {
      enriched.planned_therapies = { ...enriched.planned_therapies, writable: false, retryable: true,
        reason: disease ? 'Could not load planned therapies.' : 'Select a disease to see available therapies.' };
    }
    return enriched;
  };

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
        const fields = await this.enrichFields({ ...response.data }, values);
        if (revision !== this.readRevision) return;
        this.publish({ values, fields, personId, loading: false, datesStale: false,
          errors: this.withoutStaleErrors(),
          writable: data.write_enabled !== false, error: undefined });
      } catch {
        if (revision === this.readRevision) this.publish({ loading: false, writable: false,
          error: 'Could not load the patient record and field permissions. Please retry.' });
      } finally { this.loading = undefined; }
    })();
    return this.loading;
  };
  private enqueueEntries = (entries: Record<string, unknown>) => {
    const names = Object.keys(entries);
    for (const name of names) {
      this.queue[name] = entries[name];
      this.revisions[name] = ++this.revision;
    }
    const errors = { ...this.snapshot.errors };
    for (const name of names) delete errors[name];
    this.publish({ pending: { ...this.snapshot.pending, ...entries }, errors });
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush(); }, 500);
  };

  enqueue = (name: string, value: unknown) => {
    const field = this.snapshot.fields[name];
    if (!this.snapshot.writable || !field?.writable || field.target !== 'patient_record') return;
    // A dated edit carries its correction target. After a failed descriptor
    // refresh that target is the pre-save date, so refuse it here as well as in
    // the editor and in flush().
    if (isDatedEdit(value) && this.snapshot.datesStale) {
      this.publish({ errors: { ...this.snapshot.errors, [name]: STALE_DATES_ERROR } });
      return;
    }
    this.enqueueEntries({ [name]: value });
  };

  /** Queue a source value and its companion unit as one assertion (#5700).
   *
   * The unit descriptor is selectable (`writable: false`), because the value is
   * what the write lands in; the pair is still one clinical answer. Both keys
   * travel in the same PATCH so a refusal cannot save a number without the unit
   * the reader chose. The server converts to the canonical unit; the client
   * never does. */
  enqueuePair = (name: string, value: unknown, unitField: string, unit: string) => {
    const field = this.snapshot.fields[name];
    const unitDescriptor = this.snapshot.fields[unitField];
    if (!this.snapshot.writable || !field?.writable || field.target !== 'patient_record') return;
    if (!unitDescriptor || unitDescriptor.qualifies !== name) return;
    this.enqueueEntries({ [name]: value, [unitField]: unit });
  };
  private reloadFields = async (): Promise<boolean> => {
    const personId = this.snapshot.personId;
    if (!personId) return false;
    const readRevision = this.readRevision;
    // A retry can overlap the post-save refresh it is recovering from. Only the
    // newest attempt may publish, or a late failure would re-arm the flag after
    // a successful retry.
    const attempt = ++this.reloadSequence;
    const current = () => readRevision === this.readRevision && attempt === this.reloadSequence;
    try {
      const { data } = await this.client.get<Record<string, FormFieldDescriptor>>(
        `${this.base}/v1/patient-records/writable-fields/`,
        { params: { person_id: personId }, headers: { 'X-Patient-Form': '1' } });
      const fields = await this.enrichFields({ ...data }, this.snapshot.values);
      if (current()) {
        this.publish({ fields, datesStale: false, errors: this.withoutStaleErrors() });
      }
      return true;
    } catch {
      // A failed metadata refresh must not undo the value the PATCH acknowledged.
      // It does leave `latest_date` at the pre-save date, so record that the
      // dates are stale: the editor and the queue refuse the next dated save
      // until a refresh succeeds, instead of pre-filling a correction target
      // that is no longer the newest result (#5608).
      if (current()) {
        const fields = { ...this.snapshot.fields };
        const planned = fields.planned_therapies;
        if (planned) {
          // The disease/line may have changed; the old catalog is no longer
          // safe to offer. Disable it until a retry refreshes the descriptors.
          fields.planned_therapies = {
            ...planned, writable: false, retryable: true,
            reason: 'Could not refresh planned therapies for the current disease. Retry.',
          };
        }
        this.publish({ datesStale: true, fields });
      }
      return false;
    }
  };

  /** Drop the "retry first" errors a successful refresh has answered. */
  private withoutStaleErrors(): Record<string, string> {
    const errors = { ...this.snapshot.errors };
    for (const [name, message] of Object.entries(errors)) {
      if (message === STALE_DATES_ERROR && !(name in this.snapshot.pending)) delete errors[name];
    }
    return errors;
  }

  /** Retry the descriptor refresh a failed post-save reload left behind. */
  reloadDates = () => this.refreshDates();

  /** The newest date refresh, awaited by `flush()` before it dispatches again. */
  private datesRefresh?: Promise<boolean>;

  private refreshDates = (): Promise<boolean> => {
    const attempt = this.reloadFields();
    this.datesRefresh = attempt;
    void attempt.finally(() => { if (this.datesRefresh === attempt) this.datesRefresh = undefined; });
    return attempt;
  };

  /**
   * Wait until no date refresh is in flight, including one that supersedes the
   * attempt being awaited: awaiting a captured promise would dispatch a queued
   * dated edit while its replacement is still deciding the same dates.
   */
  private settleDateRefresh = async (): Promise<void> => {
    for (;;) {
      const refresh = this.datesRefresh;
      if (!refresh) return;
      await refresh.catch(() => false);
      // `refreshDates` clears the slot as the attempt settles; a superseding
      // attempt replaces it, so keep waiting until nothing is in flight.
      if (this.datesRefresh === refresh) return;
    }
  };

  flush = async (): Promise<void> => {
    clearTimeout(this.timer);
    if (this.flight || !Object.keys(this.queue).length || !this.snapshot.personId) return;
    // Never dispatch ahead of a date refresh: its outcome decides whether a
    // queued dated edit still has a valid correction target (#5608).
    for (;;) {
      await this.settleDateRefresh();
      if (this.flight || !Object.keys(this.queue).length || !this.snapshot.personId) return;
      // No await between this check and taking the batch below, so a refresh
      // cannot start in the gap and be overtaken by the dispatch.
      if (!this.datesRefresh) break;
    }
    const batch = this.queue;
    const revisions = { ...this.revisions };
    const explicitAtStart = this.explicitRevision;
    this.queue = {};
    // An edit accepted while the previous save was still refreshing cannot be
    // sent against the pre-save date once that refresh has failed (#5608).
    // Hold it instead of sending it: pending clears, the field says why, and a
    // successful Retry does not silently re-send a date the patient never
    // confirmed against the new latest result.
    const staleDated = this.snapshot.datesStale
      ? Object.keys(batch).filter((name) => isDatedEdit(batch[name])) : [];
    if (staleDated.length) {
      const pending = { ...this.snapshot.pending };
      const errors = { ...this.snapshot.errors };
      for (const name of staleDated) {
        if (this.revisions[name] === revisions[name]) delete pending[name];
        errors[name] = STALE_DATES_ERROR;
        delete batch[name];
      }
      this.publish({ pending, errors });
      if (!Object.keys(batch).length) return;
    }
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
      // A saved result becomes the correction target next time. The server owns
      // which fact wins: another session or an import may have added a newer
      // result, so ask for the fresh descriptor instead of inferring the date
      // from the batch we submitted.
      const datedApplied = data.write.applied.some((name) => {
        const sent = batch[name];
        return sent && typeof sent === 'object' && !Array.isArray(sent)
          && typeof (sent as { date?: unknown }).date === 'string';
      });
      // A disease/treatment-line change changes the planned-therapy catalog;
      // refresh descriptors so the picker does not keep the old context.
      const contextChanged = ['disease', 'therapy_lines_count', 'lines_of_therapy']
        .some((name) => data.write.applied.includes(name));
      if (datedApplied || contextChanged) await this.refreshDates();
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
      // A refresh started while this batch was settling decides whether the
      // queued dated edits still have a valid correction target: never dispatch
      // them ahead of it, and never ahead of one that supersedes it.
      await this.settleDateRefresh();
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
