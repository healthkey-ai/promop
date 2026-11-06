/** @vitest-environment jsdom */
import { act, fireEvent } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PatientFormStore } from "./patientFormStore";

// Imperative `createRoot` outside RTL's renderer: tell React that updates are
// wrapped in act so the dialog is flushed before the assertion.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const storeRef: { current?: PatientFormStore } = {};
const clientRef: { current?: Record<string, unknown> } = {};
const disposers: Array<() => void> = [];

vi.mock("./patientFormRuntime", () => ({
  patientFormRuntime: () => ({
    // The findings modal must talk through the widget's own client, not a
    // module-level transport that another mounted widget could have replaced.
    client: clientRef.current,
    queryClient: new QueryClient(),
    store: storeRef.current,
  }),
  retainPatientFormRuntime: () => () => {},
}));

vi.mock("./injectStyles", () => ({ injectStyles: () => {} }));

import { mountGenomics } from "./widget.patient-info";

const saved = { id: 123, gene: "BRCA1", variant: "c.68_69delAG", interpretation: "Pathogenic" };

interface Options {
  readOnly?: boolean;
  onClose?: () => void;
  onPatientUpdated?: () => void;
  apiBase?: string;
  variant?: string;
  /** What this widget's own descriptor says about genomic findings. */
  writable?: boolean;
  patientRecord?: () => Promise<unknown>;
}

async function mount(options: Options = {}) {
  const apiBase = options.apiBase ?? "/api";
  const client = {
    get: vi.fn(async (url: string) => {
      if (url.endsWith("/patient-info/me/")) {
        return { data: options.patientRecord
          ? await options.patientRecord()
          : { patient_info: { person_id: 7, disease: "Breast Cancer" }, write_enabled: true } };
      }
      if (url.includes("writable-fields")) {
        return { data: { genetic_mutations: { writable: options.writable ?? true, target: "patient_record" } } };
      }
      if (url.includes("genomics-legacy-cytogenetics")) return { data: { results: [], next_cursor: null } };
      if (url.includes("genomics-catalog")) return { data: { markers: [], attributes: [] } };
      return { data: [{ ...saved, variant: options.variant ?? saved.variant }] };
    }),
    post: vi.fn(async (_url: string, data: unknown) => ({ data: { ...(data as object), id: 456 } })),
    patch: vi.fn(async (_url: string, data: unknown) => ({ data })),
    delete: vi.fn(async () => ({})),
  };
  clientRef.current = client;
  const store = new PatientFormStore(client as never, apiBase);
  storeRef.current = store;
  // Deliberately not awaited: one case keeps the record GET pending forever to
  // assert the dialog is still closeable while it loads.
  void store.load();

  const root = document.createElement("div");
  document.body.appendChild(root);
  await act(async () => {
    disposers.push(mountGenomics(root, { apiBase, readOnly: false, ...options }));
  });
  await act(async () => {});
  return { client };
}

afterEach(async () => {
  await act(async () => {
    for (const dispose of disposers.splice(0)) dispose();
  });
  document.body.innerHTML = "";
});

const button = (label: string) => [...document.querySelectorAll("button")]
  .find((b) => b.textContent === label);

describe("mountGenomics", () => {
  it("opens PROMOP's own dialog for the linked patient and their disease", async () => {
    const { client } = await mount();

    await vi.waitFor(() => {
      expect(document.querySelector('[data-promop-dialog]')?.textContent).toContain("Genomic findings");
    });
    expect(document.body.textContent).toContain("c.68_69delAG");
    expect(client.get).toHaveBeenCalledWith("/api/v1/patient-records/7/genomics/");
    expect(client.get).toHaveBeenCalledWith(
      "/api/v1/patient-records/7/genomics-catalog/",
      { params: { disease: "Breast Cancer" } },
    );
  });

  it("lets the reader answer the row and reports the write to the host", async () => {
    const onPatientUpdated = vi.fn();
    const { client } = await mount({ onPatientUpdated });

    await vi.waitFor(() => expect(button("Add genomic finding")).toBeTruthy());
    await act(async () => { button("Add genomic finding")!.click(); });

    const gene = [...document.querySelectorAll("label")]
      .find((l) => l.textContent?.startsWith("Genomic feature"))?.querySelector("input");
    expect(gene).toBeTruthy();
    const featureType = [...document.querySelectorAll("label")]
      .find((l) => l.textContent?.startsWith("Feature type"))?.querySelector("select");
    await act(async () => {
      fireEvent.change(gene!, { target: { value: "TP53" } });
      fireEvent.change(featureType!, { target: { value: "Gene" } });
    });

    await act(async () => { button("Save genomic finding")!.click(); });

    await vi.waitFor(() => expect(client.post).toHaveBeenCalled());
    expect(client.post).toHaveBeenCalledWith(
      "/api/v1/patient-records/7/genomics/",
      expect.objectContaining({ genomic_feature: "TP53" }),
    );
    expect(onPatientUpdated).toHaveBeenCalled();
  });

  it("passes the host's dismissal through so it can unmount", async () => {
    const onClose = vi.fn();
    await mount({ onClose });

    await vi.waitFor(() => expect(button("Close")).toBeTruthy());
    await act(async () => { button("Close")!.click(); });

    expect(onClose).toHaveBeenCalled();
  });

  it("stays closeable while the patient record is still loading", async () => {
    const onClose = vi.fn();
    await mount({ onClose, patientRecord: () => new Promise(() => {}) });

    await vi.waitFor(() => expect(document.body.textContent).toContain("Loading genomic findings"));
    expect(button("Close")).toBeTruthy();
    await act(async () => { button("Close")!.click(); });

    expect(onClose).toHaveBeenCalled();
  });

  it("stays closeable when the patient record fails to load", async () => {
    const onClose = vi.fn();
    await mount({ onClose, patientRecord: async () => { throw new Error("offline"); } });

    await vi.waitFor(() => expect(document.body.textContent).toContain("Could not load the patient record"));
    expect(document.body.textContent).toContain("Retry");
    await act(async () => { button("Close")!.click(); });

    expect(onClose).toHaveBeenCalled();
  });

  it("takes writability from its own descriptor when two widgets disagree", async () => {
    await mount({ apiBase: "/host-a", variant: "editable-finding", writable: true });
    await mount({ apiBase: "/host-b", variant: "read-only-finding", writable: false });

    await vi.waitFor(() => expect(document.body.textContent).toContain("read-only-finding"));
    const dialogs = [...document.querySelectorAll<HTMLElement>("[data-promop-dialog]")];
    expect(dialogs).toHaveLength(2);
    expect(dialogs[0].textContent).toContain("Add genomic finding");
    // Read-only comes from the descriptor this widget loaded, not from the
    // shared module cache another mount may have filled.
    expect(dialogs[1].textContent).toContain("read-only-finding");
    expect(dialogs[1].textContent).not.toContain("Add genomic finding");
  });

  it("calls its own transport when two widgets with different credentials are mounted", async () => {
    const first = await mount({ apiBase: "/host-a", variant: "first-finding" });
    const second = await mount({ apiBase: "/host-b", variant: "second-finding" });
    expect(second.client).not.toBe(first.client);

    await vi.waitFor(() => expect(document.body.textContent).toContain("first-finding"));
    // The first widget must still write through its own client and base.
    const editButtons = [...document.querySelectorAll("button")].filter((b) => b.textContent === "Edit");
    await act(async () => { editButtons[0].click(); });
    await act(async () => { button("Save genomic finding")!.click(); });

    await vi.waitFor(() => expect(first.client.patch).toHaveBeenCalled());
    expect(first.client.patch).toHaveBeenCalledWith(
      "/host-a/v1/patient-records/7/genomics/123/",
      expect.objectContaining({ variant: "first-finding" }),
    );
    expect(second.client.patch).not.toHaveBeenCalled();
    expect(second.client.get).toHaveBeenCalledWith(expect.stringMatching(/^\/host-b\//));
  });
});
