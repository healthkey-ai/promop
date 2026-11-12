/** @vitest-environment jsdom */
import { act } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PatientFormStore, type SaveState } from "./patientFormStore";

// Imperative `createRoot` outside RTL's renderer: tell React that updates are
// wrapped in act so the portal is flushed before the assertion.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const storeRef: { current?: PatientFormStore } = {};
const disposers: Array<() => void> = [];

vi.mock("./patientFormRuntime", () => ({
  patientFormRuntime: () => ({
    client: {},
    queryClient: new QueryClient(),
    store: storeRef.current,
  }),
  retainPatientFormRuntime: () => () => {},
}));

vi.mock("./injectStyles", () => ({ injectStyles: () => {} }));

import { mountFields } from "./widget.patient-info";

async function mountWith(
  label: string | null,
  onSaveStateChange?: (state: SaveState) => void,
) {
  const client = {
    get: vi.fn(async (url: string) => ({
      data: url.includes("writable-fields")
        ? { height: { writable: true, target: "patient_record", control: "number", label: "Height" } }
        : { patient_info: { person_id: 1, height: 170 }, write_enabled: true },
    })),
    patch: vi.fn(),
  };
  const store = new PatientFormStore(client as never, "/api");
  storeRef.current = store;
  await store.load();

  const container = document.createElement("div");
  container.setAttribute("data-promop-form", "");
  const root = document.createElement("div");
  const slot = document.createElement("div");
  slot.setAttribute("data-promop-field", "height");
  if (label !== null) slot.setAttribute("data-promop-label", label);
  container.append(root, slot);
  document.body.appendChild(container);

  // The root and the slots are siblings: createRoot manages its own element and
  // would clear portalled slots if they were its children.
  await act(async () => {
    disposers.push(mountFields(root, { apiBase: "/api", readOnly: false, onSaveStateChange }));
  });
  await act(async () => {});
  return { container, slot };
}

afterEach(async () => {
  await act(async () => {
    for (const dispose of disposers.splice(0)) dispose();
  });
  document.body.innerHTML = "";
});

describe("mountFields labels", () => {
  it("names the control from the slot's data-promop-label", async () => {
    await mountWith("Height at intake");

    await vi.waitFor(() => {
      const input = document.querySelector<HTMLInputElement>(
        '[data-promop-field="height"] input',
      );
      expect(input?.getAttribute("aria-label")).toBe("Height at intake");
    });
  });

  it("falls back to the descriptor label when the slot has none", async () => {
    await mountWith(null);

    await vi.waitFor(() => {
      const input = document.querySelector<HTMLInputElement>(
        '[data-promop-field="height"] input',
      );
      expect(input?.getAttribute("aria-label")).toBe("Height");
    });
  });

  it("treats a blank label as absent", async () => {
    await mountWith("   ");

    await vi.waitFor(() => {
      const input = document.querySelector<HTMLInputElement>(
        '[data-promop-field="height"] input',
      );
      expect(input?.getAttribute("aria-label")).toBe("Height");
    });
  });
});

describe("mountFields save state", () => {
  it("emits initial state and tracks a bypass write", async () => {
    const states: SaveState[] = [];
    await mountWith("Height", (state) => states.push(state));

    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: false, failed: false });
    });

    await act(async () => { storeRef.current!.beginBypassWrite(); });
    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: true });
    });

    await act(async () => { storeRef.current!.failBypassWrite('therapy failed'); });
    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: true, failed: true, bypassError: 'therapy failed' });
    });

    await act(async () => { storeRef.current!.endBypassWrite(); });
    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: false, failed: true });
    });

    await act(async () => { storeRef.current!.beginBypassWrite(); });
    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: true, failed: false });
    });
    await act(async () => { storeRef.current!.endBypassWrite(); });
    await vi.waitFor(() => {
      expect(states.at(-1)).toMatchObject({ pending: false, failed: false });
    });
  });
});
