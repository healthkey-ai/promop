// Self-contained "widget" build of the PatientInfo form for hosts that CANNOT share a
// single React tree with this remote — specifically CB's `ui.v2`, whose React is 18
// while this remote runs React 19. The Module-Federation remote (vite.remote.config.ts)
// shares React as a singleton; this build BUNDLES its own React 19 + axios and exposes
// an imperative, framework-agnostic mount(el, opts)/unmount(el) the host calls with
// plain values (a token + an apiBase), never a React reference. Isolated by
// construction. Mirrors EXACT's federation widget.
//
// Consume from a host:
//   const { mount } = await import("<promop>/widget/promop-patient-info.js");
//   const dispose = mount(el, { apiBase: "/api/v1/federation", token, readOnly: false });
import { StrictMode } from "react";
import { createPortal } from 'react-dom';
import { createRoot, type Root } from "react-dom/client";

import { PatientInfo } from "./PatientInfo";
import { injectStyles } from "./injectStyles";
import { patientFormRuntime, retainPatientFormRuntime, type PatientFormConnection } from './patientFormRuntime';
import PatientFieldEditor from './PatientFieldEditor';
import TherapyLinesPanel from './TherapyLinesPanel';
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
  onPatientUpdated?: (result: PatientFormResult) => unknown;
  onSaveError?: () => unknown;
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
  subscriptions.set(el, retainPatientFormRuntime(opts));
  const apiClient = runtime.client;
  const apiBasePath = (opts.apiBase ?? "").replace(/\/+$/, "");
  // CB mounts this against its in-process /federation surface, where the wearable
  // upload endpoints do not exist. Detect that from the base and hide the tab
  // instead of rendering a control that can only 404.
  const federated = apiBasePath.includes("federation");

  const root = createRoot(el);
  roots.set(el, root);
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
export function mountFields(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles();
  const runtime = patientFormRuntime(opts);
  const container = el.closest('[data-promop-form]');
  if (!container) throw new Error('Patient field editor requires a data-promop-form container.');
  const slots = [...container.querySelectorAll<HTMLElement>('[data-promop-field]')];
  const release = retainPatientFormRuntime(opts);
  const stopUpdates = opts.onPatientUpdated ? runtime.store.onUpdated(opts.onPatientUpdated) : () => {};
  let lastErrors = runtime.store.getSnapshot().errors;
  const stopErrors = runtime.store.subscribe(() => {
    const errors = runtime.store.getSnapshot().errors;
    if (errors !== lastErrors && Object.keys(errors).length) opts.onSaveError?.();
    lastErrors = errors;
  });
  subscriptions.set(el, () => { stopUpdates(); stopErrors(); release(); });
  void runtime.store.load();
  const root = createRoot(el);
  roots.set(el, root);
  root.render(<StrictMode>{slots.map((slot, index) => createPortal(
    <PatientFieldEditor store={runtime.store} name={slot.dataset.promopField!}
      readOnly={opts.readOnly ?? true} infoHref={opts.infoHref} compact />,
    slot, String(index),
  ))}</StrictMode>);
  return () => unmount(el);
}

/** The therapy-line editor only: a host with a modal (trial details) mounts
 * this instead of the whole patient form. Same store, same editor as the
 * Treatment tab, so authoring a line there and here are the same write. */
export function mountTherapyLines(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles();
  const runtime = patientFormRuntime(opts);
  subscriptions.set(el, retainPatientFormRuntime(opts));
  void runtime.store.load();
  const root = createRoot(el);
  roots.set(el, root);
  root.render(
    <StrictMode>
      <TherapyLinesPanel store={runtime.store} readOnly={opts.readOnly ?? true} />
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
