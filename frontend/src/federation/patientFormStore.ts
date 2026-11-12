import type { AxiosInstance } from 'axios';
import type { FieldDescriptor } from '@/hooks/useWritableFields';
import { diseaseToDiseaseCode, plannedTherapyRound } from '@/api/therapyContext';
import { parseWritableDescriptor } from './descriptorVersion';

/**
 * A dated edit is `{value, date}`: the pair is one clinical assertion, so a
 * stale date is a wrong clinical day rather than a formatting detail.
 */
function isDatedEdit(value: unknown): value is { value: unknown; date: string } {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && 'date' in (value as Record<string, unknown>);
}

const MISSING_RECORD_ERROR = 'No patient record linked to this account.';

/** An HTTP 404 answer, as axios reports it. */
function isNotFound(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 404;
}

/** A save that failed without a refusal from the server keeps its draft. */
const FAILED_SAVE_ERROR = 'Could not save this value. Retry to send it again.';
/** The server refused the request as a whole (a 4xx without a field map). */
const REFUSED_SAVE_ERROR = 'The server refused this value. Your last saved value has been restored.';
/** A 2xx that did not acknowledge the fields: the server may have saved them. */
const UNCONFIRMED_SAVE_ERROR =
  'The server did not confirm this save. The record was reloaded; check the value.';

/** An edit queued on a store whose client switched to another person. */
const RETIRED_SAVE_ERROR =
  'Not sent: the signed-in patient changed before this edit was saved.';

/** An edit made on a store that no longer belongs to the signed-in patient. */
const RETIRED_EDIT_ERROR =
  'Not saved: this form no longer belongs to the signed-in patient. Reload to edit.';

/** Statuses that say "try again later", not "this value is wrong". */
const TRANSIENT_STATUSES = new Set([401, 408, 429]);

const STALE_DATES_ERROR =
  'The result dates could not be refreshed. Retry before saving so this result lands on the right day.';

export interface FormFieldDescriptor extends FieldDescriptor {
  label?: string;
  control?: 'text' | 'number' | 'date' | 'boolean' | 'select' | 'multiselect' | 'email';
  bindings?: string[];
  /** The reader already edited this value, so it may not be the newest fact on
   *  the record; the editor asks for the correction date explicitly. */
  user_edited?: boolean;
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
  /** Drafts whose save failed without a server refusal (offline, 5xx, an
   *  unreadable answer). They stay visible over the saved value until the
   *  reader retries them or edits the field again; they are never resent on
   *  their own. A field the server refused is not here: its saved value is
   *  shown again with the server's reason. */
  failed: Record<string, unknown>;
  errors: Record<string, string>;
  personId?: number;
  loading: boolean;
  saving: boolean;
  writable: boolean;
  error?: string;
  /** A post-save descriptor refresh failed, so `latest_date` may be stale. */
  datesStale?: boolean;
  /** This build cannot honour the record contract; no write may be sent. */
  contractError?: string;
  /** The client now answers for a different patient than this store holds.
   *  The store never adopts the new person (its queue belongs to the old one);
   *  the caller starts a fresh store instead. */
  identityChanged?: boolean;
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
    values: {}, fields: {}, pending: {}, failed: {}, errors: {}, loading: true, saving: false, writable: false,
  };
  private listeners = new Set<() => void>();
  /** Listener -> the registration that owns it. A later registration with the
   *  same function (StrictMode replay) supersedes the earlier one, so the
   *  earlier cleanup cannot delete the live subscription when it eventually
   *  runs. */
  private updated = new Map<(result: PatientFormResult) => unknown, number>();
  private updatedGeneration = 0;
  /** A bypass write (attestation/therapy/genomics) is in flight. Listeners are
   *  retained until it lands, because the notification comes after the request
   *  and the screen may already be gone. */
  private bypasses = 0;
  private queue: Record<string, unknown> = {};
  private revisions: Record<string, number> = {};
  private revision = 0;
  private explicitRevision = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private flight = false;
  private loading?: Promise<void>;
  private readRevision = 0;
  /** This store's client now speaks for someone else; see `retire()`. */
  private retired = false;
  /** Drafts `retire()` dropped without sending, consumed once by the caller
   *  that tells the reader. An in-flight save settles as usual and is not
   *  counted here. */
  private retiredDrafts = 0;

  constructor(private client: AxiosInstance, private base: string) {}

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  onUpdated(listener: (result: PatientFormResult) => unknown) {
    const token = ++this.updatedGeneration;
    this.updated.set(listener, token);
    return () => {
      // The host's invalidation must still run when navigation removed its
      // root while a save/follow-up is pending. Retire it only after draining.
      const removeWhenIdle = () => {
        if (this.flight || this.bypasses > 0
            || Object.keys(this.snapshot.pending).length) return false;
        // Only the registration this cleanup belongs to may remove it: a
        // replay that re-registered the same function owns the live entry.
        if (this.updated.get(listener) === token) this.updated.delete(listener);
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
  /** Hold the host's listeners while a write outside the PATCH queue runs. */
  beginBypassWrite() {
    this.bypasses += 1;
  }
  endBypassWrite() {
    this.bypasses = Math.max(0, this.bypasses - 1);
    if (this.bypasses === 0) this.publish({});
  }
  /** Tell the host's `onUpdated` listeners that a write bypassed the PATCH
   *  queue: an explicit attestation, a therapy line or a genomics finding.
   *  Those paths call `store.load()` for the screen, but the queue never runs,
   *  so without this the host keeps its stale eligibility/cache. */
  announceUpdated() {
    const data: PatientFormResult = {
      patient_info: { ...this.snapshot.values },
      write: { applied: [] },
    };
    void Promise.allSettled(
      [...this.updated.keys()].map((listener) =>
        Promise.resolve().then(() => listener(data))),
    );
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
      const round = plannedTherapyRound(values.therapy_lines_count, values.lines_of_therapy);
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

  /** Read the record. A read already in flight is shared, unless `fresh`:
   * then a new read starts and supersedes it (its answer may predate a write). */
  load = (fresh = false): Promise<void> => {
    if (this.loading && !fresh) return this.loading;
    const revision = ++this.readRevision;
    const run = (async () => {
      // Only the record read's 404 means "no linked record"; a 404 from the
      // descriptor request is a different failure.
      let readingRecord = true;
      try {
        const { data } = await this.client.get(`${this.base}/patient-info/me/`);
        readingRecord = false;
        const values = { ...data.patient_info, patient_name: data.patient_name };
        const personId = Number(values.person_id ?? values.person);
        if (!personId) throw new Error('No linked patient record.');
        if (this.snapshot.personId && this.snapshot.personId !== personId) {
          if (revision === this.readRevision) {
            this.publish({ loading: false, writable: false, identityChanged: true,
              error: 'The linked patient changed. Start a new patient context.' });
          }
          // The client now answers for another person, so nothing queued
          // here may go out through it any more.
          this.retire();
          return;
        }
        const response = await this.client.get<Record<string, unknown>>(`${this.base}/v1/patient-records/writable-fields/`, {
          params: { person_id: personId },
          headers: { 'X-Patient-Form': '1' },
        });
        const parsed = parseWritableDescriptor(response.data);
        if (parsed.error) {
          if (revision === this.readRevision) {
            this.publish({ values, personId, fields: {}, loading: false,
              datesStale: false, writable: false, contractError: parsed.error,
              error: parsed.error });
          }
          return;
        }
        const fields = await this.enrichFields(
          parsed.fields as unknown as Record<string, FormFieldDescriptor>,
          values,
        );
        if (revision !== this.readRevision) return;
        this.publish({ values, fields, personId, loading: false, datesStale: false,
          errors: this.withoutStaleErrors(), contractError: undefined, identityChanged: false,
          // A retired store never offers edits again, whatever the server says.
          writable: !this.retired && data.write_enabled !== false, error: undefined });
      } catch (error) {
        // The record read answers 404 when no patient is linked to the account.
        // That is a state the reader can act on, not a transient failure, so it
        // keeps its own sentence; everything else stays generic.
        if (revision === this.readRevision) this.publish({ loading: false, writable: false,
          error: readingRecord && isNotFound(error) ? MISSING_RECORD_ERROR
            : 'Could not load the patient record and field permissions. Please retry.' });
      }
    })();
    this.loading = run;
    // Only the newest read clears the slot: a superseded read settling late
    // must not drop the one that replaced it.
    void run.finally(() => { if (this.loading === run) this.loading = undefined; });
    return run;
  };
  private enqueueEntries = (entries: Record<string, unknown>) => {
    const names = Object.keys(entries);
    for (const name of names) {
      this.queue[name] = entries[name];
      this.revisions[name] = ++this.revision;
    }
    const errors = { ...this.snapshot.errors };
    const failed = { ...this.snapshot.failed };
    // A new edit replaces a failed draft of the same field.
    for (const name of names) { delete errors[name]; delete failed[name]; }
    this.publish({ pending: { ...this.snapshot.pending, ...entries }, failed, errors });
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { void this.flush(); }, 500);
  };

  /**
   * Stop this store for good: its client now authenticates someone else (the
   * host switched sign-in or impersonation on a reused axios instance, or a
   * load found another person). Unsent edits are not dispatched through the
   * new credentials; they move to `failed` with a reason, so nothing is
   * silently claimed as saved. A request already in flight settles as usual.
   * A plain unmount does NOT retire a store: its queue still drains then.
   */
  /** True once `retire()` ran. Caches evict such a store on lookup. */
  get isRetired() { return this.retired; }

  retire = () => {
    if (this.retired) return;
    this.retired = true;
    clearTimeout(this.timer);
    const pending = { ...this.snapshot.pending };
    const failed = { ...this.snapshot.failed };
    const errors = { ...this.snapshot.errors };
    // A draft already in the failed map is lost too. A batch in `pending` has
    // been dispatched; it settles as usual, so it is not counted as unsent.
    this.retiredDrafts = Object.keys(failed).length + Object.keys(this.queue).length;
    for (const [name, value] of Object.entries(this.queue)) {
      delete pending[name];
      failed[name] = value;
      errors[name] = RETIRED_SAVE_ERROR;
    }
    this.queue = {};
    this.publish({ pending, failed, errors, writable: false });
  };

  /** The drafts this store lost on retirement, read once by the caller that
   *  surfaces the notice. */
  takeRetiredDrafts = () => {
    const count = this.retiredDrafts;
    this.retiredDrafts = 0;
    return count;
  };

  /** An edit offered to a retired store is refused visibly, never dropped. */
  private refuseRetired(names: string[]) {
    const errors = { ...this.snapshot.errors };
    for (const name of names) errors[name] = RETIRED_EDIT_ERROR;
    this.publish({ errors });
  }

  enqueue = (name: string, value: unknown) => {
    if (this.retired) { this.refuseRetired([name]); return; }
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
  enqueuePair = (name: string, value: unknown, unitField: string, unit: string): boolean => {
    if (this.retired) { this.refuseRetired([name, unitField]); return false; }
    const field = this.snapshot.fields[name];
    const unitDescriptor = this.snapshot.fields[unitField];
    if (!this.snapshot.writable || !field?.writable || field.target !== 'patient_record') return false;
    if (!unitDescriptor || unitDescriptor.qualifies !== name) return false;
    // A dated pair carries the same correction-target guard as `enqueue`.
    if (isDatedEdit(value) && this.snapshot.datesStale) {
      this.publish({ errors: { ...this.snapshot.errors, [name]: STALE_DATES_ERROR } });
      return false;
    }
    this.enqueueEntries({ [name]: value, [unitField]: unit });
    return true;
  };
  /**
   * Send failed drafts again: all of them, or the named fields with their
   * companion units. Each goes back through the queue, so the revision,
   * serialisation and stale-date rules apply as to a fresh edit. A dated draft
   * waits while the result dates are stale, keeping its draft.
   */
  retryFailed = (names?: string[]) => {
    if (this.retired || !this.snapshot.writable) return;
    const failed = { ...this.snapshot.failed };
    const fields = this.snapshot.fields;
    const chosen = new Set(names ?? Object.keys(failed));
    for (const name of [...chosen]) {
      const owner = fields[name]?.qualifies;
      if (owner) chosen.add(owner);
    }
    for (const key of Object.keys(failed)) {
      const owner = fields[key]?.qualifies;
      if (owner && chosen.has(owner)) chosen.add(key);
    }
    const drafts: Record<string, unknown> = {};
    const errors = { ...this.snapshot.errors };
    let dropped = false;
    for (const name of chosen) {
      if (!(name in failed)) continue;
      const owner = fields[name]?.qualifies ?? name;
      // The same gate `enqueue` applies: a field (or the value a unit
      // qualifies) the descriptor no longer offers for writing is not resent.
      const target = fields[owner];
      if (!target?.writable || target.target !== 'patient_record') {
        delete failed[name];
        dropped = true;
        errors[name] = 'This field can no longer be edited here; the saved value is shown.';
        continue;
      }
      const dated = isDatedEdit(failed[owner]);
      if (dated && this.snapshot.datesStale) {
        errors[name] = STALE_DATES_ERROR;
        continue;
      }
      drafts[name] = failed[name];
    }
    const next = dropped ? { errors, failed } : { errors };
    if (!Object.keys(drafts).length) {
      this.publish(next);
      return;
    }
    this.publish(next);
    this.enqueueEntries(drafts);
    void this.flush();
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
      const { data } = await this.client.get<Record<string, unknown>>(
        `${this.base}/v1/patient-records/writable-fields/`,
        { params: { person_id: personId }, headers: { 'X-Patient-Form': '1' } });
      const parsed = parseWritableDescriptor(data);
      if (parsed.error) {
        if (current()) {
          this.publish({ fields: {}, writable: false, datesStale: false,
            contractError: parsed.error, error: parsed.error });
        }
        return false;
      }
      const fields = await this.enrichFields(
        { ...parsed.fields } as unknown as Record<string, FormFieldDescriptor>,
        this.snapshot.values,
      );
      if (current()) {
        this.publish({ fields, datesStale: false, contractError: undefined,
          errors: this.withoutStaleErrors() });
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
      if (message !== STALE_DATES_ERROR || name in this.snapshot.pending) continue;
      // A failed draft held back by stale dates can be retried again now.
      if (name in this.snapshot.failed) errors[name] = FAILED_SAVE_ERROR;
      else delete errors[name];
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
    if (this.retired) return;
    if (this.flight || !Object.keys(this.queue).length || !this.snapshot.personId) return;
    // Never dispatch ahead of a date refresh: its outcome decides whether a
    // queued dated edit still has a valid correction target (#5608).
    for (;;) {
      await this.settleDateRefresh();
      if (this.retired || this.flight || !Object.keys(this.queue).length
        || !this.snapshot.personId) return;
      // No await between this check and taking the batch below, so a refresh
      // cannot start in the gap and be overtaken by the dispatch.
      if (!this.datesRefresh) break;
    }
    if (this.snapshot.contractError) {
      // The record contract changed under this build. Sending the queue would
      // write under old semantics; reject it with the same sentence the screen
      // shows instead.
      const rejected = this.snapshot.contractError;
      const pending = { ...this.snapshot.pending };
      const errors = { ...this.snapshot.errors };
      for (const name of Object.keys(this.queue)) {
        delete pending[name];
        errors[name] = rejected;
      }
      this.queue = {};
      this.publish({ pending, errors });
      return;
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
      const failed = { ...this.snapshot.failed };
      const errors = { ...this.snapshot.errors };
      // Held, not discarded: the draft moves to `failed`, so it stays visible
      // and goes out again only from an explicit Retry once dates refresh.
      const hold = (key: string) => {
        if (this.revisions[key] === revisions[key]) {
          delete pending[key];
          failed[key] = batch[key];
        }
        delete batch[key];
      };
      for (const name of staleDated) {
        errors[name] = STALE_DATES_ERROR;
        hold(name);
        // A companion unit is not a fact on its own. Holding the dated value
        // must hold the unit too, or the next PATCH would write a lone unit.
        for (const key of Object.keys(batch)) {
          if (this.snapshot.fields[key]?.qualifies === name) hold(key);
        }
      }
      this.publish({ pending, failed, errors });
      if (!Object.keys(batch).length) return;
    }
    this.flight = true;
    this.publish({ saving: true });
    const batchErrors: Record<string, string> = {};
    // Fields whose request failed without a server refusal: their drafts stay.
    const unsent = new Set<string>();
    // A 2xx without the acknowledgement: the write may have landed.
    let unconfirmed = false;
    try {
      const { data } = await this.client.patch<PatientFormResult>(
        `${this.base}/v1/patient-records/${this.snapshot.personId}/`, batch,
        { headers: { 'X-Patient-Form': '1' } },
      );
      if (!data.patient_info || !Array.isArray(data.write?.applied)) {
        unconfirmed = true;
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
      await Promise.allSettled([...this.updated.keys()]
        .map((listener) => Promise.resolve().then(() => listener(data))));
    } catch (error) {
      const response = (error as { response?: { status?: number; data?: {
        rejected?: Record<string, string>; detail?: unknown;
      } } })?.response;
      const rejected = response?.data?.rejected;
      const status = response?.status;
      // A 4xx (other than auth expiry, timeout and rate limiting) is the
      // server's answer about this request: resending it gets the same answer.
      // Only a failure that says nothing about the value keeps the draft for
      // Retry: offline, a 5xx, 401/408/429.
      const refusal = status !== undefined && status >= 400 && status < 500
        && !TRANSIENT_STATUSES.has(status);
      const detail = typeof response?.data?.detail === 'string' && response.data.detail
        ? response.data.detail : undefined;
      // A refusal names the field and restores the saved value: a retry would
      // send the same refused value. A value and its unit are one assertion:
      // refusing either half restores both.
      const owner = (name: string) => this.snapshot.fields[name]?.qualifies ?? name;
      const refusedOwners = new Set(Object.keys(batch)
        .filter((name) => rejected?.[name]).map(owner));
      // When the refusal names fields of this batch, it is about those fields:
      // the others were valid and only went unsent with them, so they keep
      // their drafts for Retry. A request-wide refusal (no field of the batch
      // named) restores everything and shows `detail` when there is one.
      const namesBatchField = refusedOwners.size > 0;
      const sentence = (text: string) => (/[.!?]$/.test(text) ? text : `${text}.`);
      for (const name of Object.keys(batch)) {
        if (unconfirmed) batchErrors[name] = UNCONFIRMED_SAVE_ERROR;
        else if (rejected?.[name]) batchErrors[name] = rejected[name];
        else if (refusedOwners.has(owner(name))) {
          batchErrors[name] = 'Could not save this value. Your last saved value has been restored.';
        } else if (refusal && !namesBatchField) {
          batchErrors[name] = detail && !rejected
            ? `${sentence(detail)} Your last saved value has been restored.`
            : REFUSED_SAVE_ERROR;
        } else { batchErrors[name] = FAILED_SAVE_ERROR; unsent.add(name); }
      }
      // The server says this caller may not write this record at all.
      if (status === 403) this.publish({ writable: false });
    } finally {
      // A refresh started while this batch was settling decides whether the
      // queued dated edits still have a valid correction target: never dispatch
      // them ahead of it, and never ahead of one that supersedes it.
      await this.settleDateRefresh();
      const pending = { ...this.snapshot.pending };
      const failed = { ...this.snapshot.failed };
      const errors = { ...this.snapshot.errors };
      // A stale failure/success must not retire a newer edit to the same field.
      for (const name of Object.keys(batch)) {
        if (this.revisions[name] === revisions[name]) {
          delete pending[name];
          if (unsent.has(name)) failed[name] = batch[name];
          if (batchErrors[name]) errors[name] = batchErrors[name];
          else delete errors[name];
        }
      }
      this.flight = false;
      this.publish({ pending, failed, errors, saving: false });
      // The server may have saved what it did not acknowledge: read the record
      // back instead of guessing either way.
      // A read already in flight may predate the write, so start a fresh one.
      if (unconfirmed) void this.load(true);
      if (Object.keys(this.queue).length) await this.flush();
    }
  };
}
