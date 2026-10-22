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
import { createRoot, type Root } from "react-dom/client";
import axios from "axios";

import { PatientInfo } from "./PatientInfo";
import { injectStyles } from "./injectStyles";

export interface MountOptions {
  /** Base the form calls: `${apiBase}/patient-info/me/`, and the clinical helpers'
   *  prefix for `/v1/…`. In CB this is `<origin>/api/v1/federation`. */
  apiBase?: string;
  /** DRF token; sent as `Authorization: Token <token>` when present. */
  token?: string;
  /** The host's write gate: display the record without offering edits. */
  readOnly?: boolean;
}

const roots = new WeakMap<HTMLElement, Root>();

export function mount(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles(); // idempotent; ensures the widget's CSS is present even without LabsProvider

  // No baseURL: patient-info is requested as `${apiBasePath}/patient-info/me/`, and
  // the clinical helpers append `/v1/…` to the same prefix. Passing the prefix as
  // `apiBasePath` (not as axios's baseURL) is what keeps both paths correct.
  const apiClient = axios.create({
    headers: opts.token ? { Authorization: `Token ${opts.token}` } : undefined,
  });
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
        readOnly={opts.readOnly ?? true}
        federated={federated}
      />
    </StrictMode>,
  );

  return () => unmount(el);
}

export function unmount(el: HTMLElement): void {
  const root = roots.get(el);
  if (root) {
    root.unmount();
    roots.delete(el);
  }
}
