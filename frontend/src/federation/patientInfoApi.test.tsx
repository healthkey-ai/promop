/**
 * Tests for usePatchPatientInfo — issue #113
 *
 * After a successful PATCH the mutation must update the React Query cache
 * via setQueryData (not invalidateQueries).  invalidateQueries triggers a
 * refetch that returns the DB state, which can have disease=null when
 * refresh_patient_info wiped it — causing PatientInfoInner to reset editedInfo
 * and revert the disease tab label back to "Disease Specific".
 */

import { renderHook, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { AxiosInstance } from "axios";
import { usePatchPatientInfo } from "./patientInfoApi";

function createMockClient() {
  return {
    patch: vi.fn(),
  } as unknown as AxiosInstance;
}

let qc: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
});

describe("usePatchPatientInfo — cache update strategy (issue #113)", () => {
  it("updates the cache with the PATCH response instead of invalidating the query", async () => {
    const client = createMockClient();
    const patchResponse = { disease: "Follicular Lymphoma", id: 1 };
    (client.patch as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: patchResponse,
    });

    // Seed existing cache data so setQueryData has something to merge into
    qc.setQueryData(["patient-info", "me"], {
      patient_info: { disease: null, id: 1 },
      user: { email: "test@test.com" },
      patient_name: "Test Patient",
    });

    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    const setDataSpy = vi.spyOn(qc, "setQueryData");

    const { result } = renderHook(
      () => usePatchPatientInfo(client, "/api"),
      { wrapper },
    );

    result.current.mutate({ disease: "Follicular Lymphoma" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Must NOT invalidate (that would refetch and potentially return null disease)
    expect(invalidateSpy).not.toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: ["patient-info", "me"] }),
    );

    // MUST update the cache directly so the mutation result is visible immediately
    expect(setDataSpy).toHaveBeenCalledWith(
      ["patient-info", "me"],
      expect.any(Function),
    );
  });

  it("merges PATCH response fields into the existing patient_info cache entry", async () => {
    const client = createMockClient();
    (client.patch as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { disease: "Follicular Lymphoma", stage: "II", id: 1 },
    });

    const initial = {
      patient_info: { disease: null, stage: null, weight_kg: 65, id: 1 },
      user: { email: "test@test.com" },
      patient_name: "Test Patient",
    };
    qc.setQueryData(["patient-info", "me"], initial);

    const { result } = renderHook(
      () => usePatchPatientInfo(client, "/api"),
      { wrapper },
    );

    result.current.mutate({ disease: "Follicular Lymphoma", stage: "II" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const cached = qc.getQueryData<typeof initial>(["patient-info", "me"]);
    // Updated fields from PATCH response
    expect(cached?.patient_info.disease).toBe("Follicular Lymphoma");
    expect(cached?.patient_info.stage).toBe("II");
    // Pre-existing unrelated field must be preserved
    expect(cached?.patient_info.weight_kg).toBe(65);
  });

  it("does not include previous_values in the merged patient_info", async () => {
    const client = createMockClient();
    (client.patch as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        disease: "Multiple Myeloma",
        id: 1,
        previous_values: { disease: null },
      },
    });

    qc.setQueryData(["patient-info", "me"], {
      patient_info: { disease: null, id: 1 },
      user: {},
      patient_name: "",
    });

    const { result } = renderHook(
      () => usePatchPatientInfo(client, "/api"),
      { wrapper },
    );

    result.current.mutate({ disease: "Multiple Myeloma" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const cached = qc.getQueryData<{ patient_info: Record<string, unknown> }>(
      ["patient-info", "me"],
    );
    expect(cached?.patient_info.disease).toBe("Multiple Myeloma");
    // previous_values must not leak into patient_info
    expect(cached?.patient_info.previous_values).toBeUndefined();
  });
});
