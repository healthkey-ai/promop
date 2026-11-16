import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { usePatientFormStore } from '@/federation/PatientFormContext';
import type { PatientFormStore } from '@/federation/patientFormStore';

/** The negative-answer endpoint's answer (#5666): the stored value, whether an
 * attestation exists, and the source fact the confirmation was made against. */
export interface ExplicitAnswerState {
  field?: string;
  value?: unknown;
  confirmed?: boolean;
  source?: unknown;
}

interface Props {
  personId: number;
  /** The PatientRecord field being answered. */
  name: string;
  /** The descriptor's endpoint segment, e.g. `negative-answer`. */
  endpoint: string;
  /** The actions the descriptor offers; an action without a label is dropped. */
  actions?: string[];
  actionLabels?: Record<string, string>;
  /** The value the record shows now, used only when the snapshot has no value. */
  value: unknown;
  /** A read-only form still shows the answer, but offers no button. */
  readOnly?: boolean;
  /** The endpoint's response, applied to the record view without a PATCH. */
  onSaved: (values: Record<string, unknown>) => void | Promise<void>;
  /** The editor's store. A federated host injects its own authenticated client
   *  and base path there, so the answer must travel on that connection rather
   *  than the module-level transport (#2606 review). */
  store?: PatientFormStore | null;
}

/**
 * The actions an explicit answer offers, from the descriptor (#2606).
 *
 * `no_active_infection_status` and `no_other_active_malignancies` are
 * attestations, not column writes: their `writable` is false and the write goes
 * through the negative-answer endpoint with a snapshot. Without this the rows
 * rendered as read-only text and the confirm/withdraw actions the server
 * advertised had no client, so a value that feeds screening was silently
 * uneditable on `cb-v2`.
 *
 * The wording is the descriptor's `action_labels`. An action with no reviewed
 * label is not offered — a client must never invent clinical wording.
 */
export default function ExplicitAnswerActions({
  personId, name, endpoint, actions, actionLabels, value, readOnly = false, onSaved,
  store: providedStore,
}: Props) {
  const contextStore = usePatientFormStore();
  const store = providedStore ?? contextStore;
  // Another mounted editor of the same answer publishes through the store;
  // re-read the snapshot instead of keeping a stale "Explicitly confirmed".
  const [answersRevision, setAnswersRevision] = useState(
    () => store?.getSnapshot().answersRevision ?? 0,
  );
  useEffect(() => {
    if (!store) return undefined;
    return store.subscribe(() => setAnswersRevision(store.getSnapshot().answersRevision));
  }, [store]);
  // Keyed by the question, so a changed row never shows the previous row's
  // answer while the new one loads (and no state is set synchronously in the
  // effect body).
  const queryKey = `${personId}:${endpoint}:${name}`;
  const [loaded, setLoaded] = useState<{ key: string; data: ExplicitAnswerState } | null>(null);
  const [loadFailure, setLoadFailure] = useState<{ key: string; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const snapshot = loaded?.key === queryKey ? loaded.data : null;
  const loadError = loadFailure?.key === queryKey ? loadFailure.message : '';

  useEffect(() => {
    let cancelled = false;
    const load = store
      ? store.explicitAnswerState(personId, endpoint, name) as Promise<ExplicitAnswerState>
      : clinicalClient()
        .get<ExplicitAnswerState>(
          clinicalUrl(`/v1/patient-records/${personId}/${endpoint}/`),
          { params: { field: name } },
        )
        .then(({ data }) => data);
    load
      .then((data) => {
        if (!cancelled) setLoaded({ key: queryKey, data });
      })
      .catch(() => {
        if (!cancelled) {
          // Confirming against an unknown starting point is exactly what the
          // endpoint's snapshot refuses, so do not offer the button.
          setLoadFailure({
            key: queryKey,
            message: 'Could not load whether this answer is confirmed. Reload before answering.',
          });
        }
      });
    return () => { cancelled = true; };
  }, [personId, endpoint, name, queryKey, store, answersRevision]);

  // The snapshot is the authority: the record value alone cannot tell a
  // confirmation from a derived answer, and the endpoint checks the source fact.
  const ready = snapshot !== null;
  const confirmed = snapshot?.confirmed === true;
  const offered = (actions ?? []).filter((action) => actionLabels?.[action]);
  // Withdrawing a confirmation that does not exist answers nothing; every other
  // action is offered as the descriptor lists it.
  const visible = offered.filter((action) => action !== 'withdraw' || confirmed);

  const send = async (action: string) => {
    setBusy(true);
    setError('');
    setMessage('');
    // The answer bypasses the save queue, so hold the host's listeners until the
    // response and `onSaved` land.
    store?.beginBypassWrite();
    try {
      const payload = {
        field: name,
        action,
        // The snapshot is the authority once loaded: a legitimate `null`
        // expectation (never assessed) must not fall through to a value read
        // at a different time.
        expected_value: snapshot ? snapshot.value ?? null : value ?? null,
        expected_source: snapshot?.source ?? null,
      };
      const data = store
        ? await store.explicitAnswer(personId, endpoint, payload) as ExplicitAnswerState
        : (await clinicalClient().post<ExplicitAnswerState>(
          clinicalUrl(`/v1/patient-records/${personId}/${endpoint}/`), payload,
        )).data;
      setLoaded({ key: queryKey, data });
      await onSaved({ [name]: data.value });
      setMessage(data.confirmed ? 'Answer confirmed.' : 'Confirmation withdrawn.');
    } catch (caught) {
      const response = (caught as { response?: { status?: number; data?: { detail?: string } } }).response;
      const status = response?.status;
      const detail = response?.data?.detail;
      // The server's own sentence answers any refusal (a conflict, a source
      // fact to correct, read-only access); the generic text is only for a
      // failure that never reached it.
      if (detail) setError(detail);
      else if (status === 409) setError('This answer changed. Reload before answering.');
      else setError('Could not verify whether the answer was saved. Reload before retrying.');
      // Every non-2xx means nothing was written, so the host's aggregate state
      // must not claim everything saved.
      store?.failBypassWrite('Could not save the explicit answer.');
    } finally {
      store?.endBypassWrite();
      setBusy(false);
    }
  };

  const stateText = loadError || (!ready ? 'Loading the current answer…'
    : confirmed ? 'Explicitly confirmed.'
      : 'Not confirmed; the value is derived from the recorded facts.');

  return <div className="space-y-1" data-explicit-answer={name}>
    <p className="text-xs text-muted-foreground">{stateText}</p>
    {!loadError && visible.length > 0 && (
      <div className="flex flex-wrap gap-3">
        {visible.map((action) => (
          <button key={action} type="button"
            className="text-sm underline disabled:opacity-50"
            disabled={readOnly || busy || !ready}
            onClick={() => void send(action)}>
            {actionLabels![action]}
          </button>
        ))}
      </div>
    )}
    {busy && <p role="status" className="text-xs">Saving answer…</p>}
    {message && <p role="status" className="text-xs">{message}</p>}
    {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
  </div>;
}
