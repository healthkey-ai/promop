// Self-contained "widget" build of the PatientInfo form for hosts that CANNOT share a
// single React tree with this remote — specifically CB's `ui/`, which is React 18 while
// this remote is React 19. The Module-Federation remote (vite.remote.config.ts) shares
// React as a singleton; this build BUNDLES its own React 19 + QueryClient + axios and
// exposes an imperative, framework-agnostic mount(el, opts)/unmount(el) the host calls
// with plain values (a token + an apiBase), never a React reference. Isolated by
// construction. Mirrors EXACT's federation widget.
//
// Consume from a host:
//   const { mount } = await import("<promop>/widget/promop-patient-info.js");
//   const dispose = mount(el, { apiBase: "/api/v1", token, readOnly: true });
import { StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axios from "axios";

import { PatientInfo } from "./PatientInfo";
import { injectStyles } from "./injectStyles";

export interface MountOptions {
  /** API base the widget's axios points at. The form calls `${apiBase}/patient-info/me/`. */
  apiBase?: string;
  /** DRF token; sent as `Authorization: Token <token>` when present. */
  token?: string;
  /** Phase-1 default: read-only (display the PatientRecord, no descriptor-driven write). */
  readOnly?: boolean;
}

const roots = new WeakMap<HTMLElement, Root>();

export function mount(el: HTMLElement, opts: MountOptions = {}): () => void {
  unmount(el);
  injectStyles(); // idempotent; ensures the widget's CSS is present even without LabsProvider

  const apiClient = axios.create({
    baseURL: opts.apiBase ?? "/",
    headers: opts.token ? { Authorization: `Token ${opts.token}` } : undefined,
  });
  const queryClient = new QueryClient();

  const root = createRoot(el);
  roots.set(el, root);
  root.render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <PatientInfo apiClient={apiClient} readOnly={opts.readOnly ?? true} />
      </QueryClientProvider>
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
