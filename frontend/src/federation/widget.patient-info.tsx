// Self-contained "widget" build of the PatientInfo form for hosts that CANNOT share a
// single React tree with this remote. The DOM path is legacy for the trial editors:
// hosts on the same React major use the React exports (`./editors`), which take the
// host's authenticated client instead of a token. The Module-Federation remote (vite.remote.config.ts)
// shares React as a singleton; this build BUNDLES its own React 19 + axios and exposes
// an imperative, framework-agnostic mount(el, opts)/unmount(el) the host calls with
// plain values (a token + an apiBase), never a React reference. Isolated by
// construction. Mirrors EXACT's federation widget.
//
// Consume from a host:
//   const { mount } = await import("<promop>/widget/promop-patient-info.js");
//   const dispose = mount(el, { apiBase: "/api/v1/federation", token, readOnly: false });
import { StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";

import { PatientInfo } from "./PatientInfo";
import { injectStyles } from "./injectStyles";
import { patientFormRuntime, retainPatientFormRuntime, type PatientFormConnection } from './patientFormRuntime';
import { subscribeSaveState, type SaveState } from './patientFormStore';
import FieldSlots from './FieldSlots';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import TherapyLinesPanel from './TherapyLinesPanel';
import GenomicsFindingsModal from './GenomicsFindingsModal';
import RuntimeStoreScope from './RuntimeStoreScope';
import type { PatientFormResult } from './patientFormStore';

export interface MountOptions extends PatientFormConnection {
  /** Base the form calls: `${apiBase}/patient-info/me/`, and the clinical helpers'
   *  prefix for `/v1/…`. In CB this is `<origin>/api/v1/federation`. */
  apiBase?: string;
  /** DRF token; sent as `Authorization: Token <token>` when present. */
  token?: string;
  /** The host's write gate: display the record without offering edits. */
  readOnly?: boolean;
  infoHref?: string;
  onPatientUpdated?: (result?: PatientFormResult) => unknown;
  /** Host-visible aggregate save state. `pending` covers the PATCH queue and
   *  every bypass write; `failed` stays true until a later successful write.
   *  Callers must not treat `pending: false` alone as acknowledgement. */
  onSaveStateChange?: (state: SaveState) => void;
}

/** `mountFields` only. Its slots are bare controls in the host's card, with no
 *  room for the form's own "could not be saved" banner, so the host is told
 *  instead. The full form shows that banner itself, and the therapy and
 *  genomics editors write outside the save queue and show their errors inline;
 *  neither calls this, so their options do not offer it. */
export interface FieldsMountOptions extends MountOptions {
  onSaveError?: () => unknown;
}

/** Subscribe one mount to the store's save state; emits the initial state and
 *  re-targets if a renewal replaces the store for this connection. */
function watchSaveState(
  runtime: ReturnType<typeof patientFormRuntime>,
  opts: MountOptions,
): () => void {
  const onChange = opts.onSaveStateChange;
  if (!onChange) return () => {};
  let watched = runtime.store;
  let stop = subscribeSaveState(watched, onChange);
  const reattach = () => {
    const current = runtime.store;
    if (current === watched) return;
    stop();
    watched = current;
    stop = subscribeSaveState(watched, onChange);
  };
  runtime.storeRenewals ??= new Set();
  runtime.storeRenewals.add(reattach);
  return () => { stop(); runtime.storeRenewals?.delete(reattach); };
}

export interface GenomicsMountOptions extends MountOptions {
  /** Called when the reader dismisses the genomic-findings dialog, so the host
   *  that opened it can unmount the widget. */
  onClose?: () => void;
}

const roots = new WeakMap<HTMLElement, Root>();
const subscriptions = new WeakMap<HTMLElement, () => void>();

export function mount(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles(); // idempotent; ensures the widget's CSS is present even without LabsProvider

  // No baseURL: patient-info is requested as `${apiBasePath}/patient-info/me/`, and
  // the clinical helpers append `/v1/…` to the same prefix. Passing the prefix as
  // `apiBasePath` (not as axios's baseURL) is what keeps both paths correct.
  const runtime = patientFormRuntime(opts);
  const stopSaveState = watchSaveState(runtime, opts);
  const apiClient = runtime.client;
  const apiBasePath = (opts.apiBase ?? "").replace(/\/+$/, "");
  // CB mounts this against its in-process /federation surface, where the wearable
  // upload endpoints do not exist. Detect that from the base and hide the tab
  // instead of rendering a control that can only 404.
  const federated = apiBasePath.includes("federation");

  const root = createRoot(el);
  roots.set(el, root);
  const release = retainPatientFormRuntime(opts);
  subscriptions.set(el, () => { stopSaveState(); release(); });
  root.render(
    <StrictMode>
      <PatientInfo
        apiClient={apiClient}
        apiBasePath={apiBasePath}
        queryClient={runtime.queryClient}
        onPatientUpdated={opts.onPatientUpdated}
        readOnly={opts.readOnly ?? true}
        federated={federated}
      />
    </StrictMode>,
  );

  return () => unmount(el);
}

/** One remote React root/queue for all patient-value slots in a host card.
 * The host supplies only field identifiers and empty DOM targets, not controls
 * or clinical metadata. Portals retain the same recursive form context. */
export function mountFields(el: HTMLElement, opts: FieldsMountOptions = {}): () => void {
  unmount(el);
  injectStyles();
  const runtime = patientFormRuntime(opts);
  const container = el.closest('[data-promop-form]');
  if (!container) throw new Error('Patient field editor requires a data-promop-form container.');
  const slots = [...container.querySelectorAll<HTMLElement>('[data-promop-field]')];
  const release = retainPatientFormRuntime(opts);
  const stopSaveState = watchSaveState(runtime, opts);
  subscriptions.set(el, () => { stopSaveState(); release(); });
  const root = createRoot(el);
  roots.set(el, root);
  root.render(<StrictMode><FieldSlots runtime={runtime} opts={opts} slots={slots} /></StrictMode>);
  return () => unmount(el);
}


/** The therapy-line editor only: a host with a modal (trial details) mounts
 * this instead of the whole patient form. Same store, same editor as the
 * Treatment tab, so authoring a line there and here are the same write. */
export function mountTherapyLines(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles();
  const runtime = patientFormRuntime(opts);
  const apiBasePath = (opts.apiBase ?? "").replace(/\/+$/, "");
  __resetWritableFieldsCache();
  const releaseRuntime = retainPatientFormRuntime(opts);
  const stopSaveState = watchSaveState(runtime, opts);
  subscriptions.set(el, () => {
    stopSaveState();
    releaseRuntime();
    __resetWritableFieldsCache();
  });
  const root = createRoot(el);
  roots.set(el, root);
  root.render(
    <StrictMode>
      {/* Follows renewals on this runtime (a mountFields card's or the form's
          Retry) instead of holding the store it found at mount. */}
      <RuntimeStoreScope runtime={runtime} apiBase={apiBasePath}>{(store) =>
        <TherapyLinesPanel
          store={store}
          // The panel takes the connection explicitly: two mounted widgets
          // must not act through a shared module-level transport.
          connection={{ client: runtime.client, basePath: apiBasePath }}
          readOnly={opts.readOnly ?? true}
          // Therapy mutations bypass the store's save queue, so `store.load()`
          // alone never reaches the host's `onUpdated` listeners. The host
          // still has to invalidate its own queries and re-render the row.
          onChanged={() => { void opts.onPatientUpdated?.(); }}
        />}
      </RuntimeStoreScope>
    </StrictMode>,
  );
  return () => unmount(el);
}

/** The genomic-findings editor as a host-owned modal: a trial-details row
 * mounts this when the reader wants to answer "Mutation Genes" /
 * "Mutation Interpretations" without opening the whole patient form.
 *
 * PROMOP owns the dialog, the fields and the write. The host only supplies the
 * credentials and an empty element, then unmounts it from `onClose`. Same store
 * and the same editor as the Genomics tab, so the two surfaces cannot drift. */
export function mountGenomics(el: HTMLElement, opts: GenomicsMountOptions = {}): () => void {
  unmount(el);
  injectStyles();
  const runtime = patientFormRuntime(opts);
  const apiBasePath = (opts.apiBase ?? "").replace(/\/+$/, "");
  const releaseRuntime = retainPatientFormRuntime(opts);
  const stopSaveState = watchSaveState(runtime, opts);
  subscriptions.set(el, () => {
    stopSaveState();
    releaseRuntime();
    // Permission is per patient and can change with the session; the next host
    // mount must ask again.
    __resetWritableFieldsCache();
  });
  const root = createRoot(el);
  roots.set(el, root);
  root.render(
    <StrictMode>
      <RuntimeStoreScope runtime={runtime} apiBase={apiBasePath}>{(store) =>
      <GenomicsFindingsModal
        store={store}
        client={runtime.client}
        apiBase={apiBasePath}
        readOnly={opts.readOnly ?? true}
        onClose={opts.onClose}
        // The findings endpoints bypass the store's save queue, so `store.load()`
        // never reaches the host's `onUpdated` listeners. The host still has to
        // invalidate its own trial queries and re-render the row. Return the
        // promise: the dialog awaits it and holds the bypass until it settles.
        onChanged={() => opts.onPatientUpdated?.()}
      />}
      </RuntimeStoreScope>
    </StrictMode>,
  );
  return () => unmount(el);
}

export function unmount(el: HTMLElement): void {
  subscriptions.get(el)?.();
  subscriptions.delete(el);
  const root = roots.get(el);
  if (root) {
    root.unmount();
    roots.delete(el);
  }
}
