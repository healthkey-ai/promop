import { renderHook, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { AxiosInstance } from "axios";
import {
  useLabResultsSummary,
  useLabValues,
  useUpdateMeasurement,
  useDeleteMeasurement,
} from "./api";

function createMockClient() {
  return {
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
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

describe("useLabResultsSummary", () => {
  it("fetches summary with default params", async () => {
    const client = createMockClient();
    (client.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { count: 1, next: null, previous: null, results: [{ concept_id: 1 }] },
    });

    const { result } = renderHook(
      () => useLabResultsSummary({ page: 1, pageSize: 50 }, client, "/api"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/api/lab-results/summary/", {
      params: { page: 1, page_size: 50 },
    });
    expect(result.current.data?.results).toHaveLength(1);
  });

  it("is disabled when apiClient is undefined", () => {
    const { result } = renderHook(
      () => useLabResultsSummary({ page: 1 }, undefined, "/api"),
      { wrapper },
    );
    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useLabValues", () => {
  it("fetches values for a concept code", async () => {
    const client = createMockClient();
    (client.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        count: 2,
        next: null,
        previous: null,
        results: [],
        concept_id: 100,
        concept_code: "718-7",
        concept_name: "Hemoglobin",
        vocabulary_id: "LOINC",
        category: "CHEM",
      },
    });

    const { result } = renderHook(
      () => useLabValues({ conceptCode: "718-7", page: 1, pageSize: 50 }, client, "/api"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith("/api/lab-results/values/", {
      params: { concept_code: "718-7", page: 1, page_size: 50 },
    });
    expect(result.current.data?.concept_name).toBe("Hemoglobin");
  });

  it("is disabled when conceptCode is empty", () => {
    const client = createMockClient();
    const { result } = renderHook(
      () => useLabValues({ conceptCode: "", page: 1 }, client, "/api"),
      { wrapper },
    );
    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useUpdateMeasurement", () => {
  it("sends PATCH and invalidates labs queries", async () => {
    const client = createMockClient();
    (client.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(
      () => useUpdateMeasurement(client, "/api"),
      { wrapper },
    );

    result.current.mutate({ measurementId: 42, value: 13.5 });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.patch).toHaveBeenCalledWith(
      "/api/lab-results/measurements/42/",
      { value: 13.5 },
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["labs"] });
  });
});

describe("useDeleteMeasurement", () => {
  it("sends DELETE and invalidates labs queries", async () => {
    const client = createMockClient();
    (client.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {} });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const { result } = renderHook(
      () => useDeleteMeasurement(client, "/api"),
      { wrapper },
    );

    result.current.mutate(99);

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.delete).toHaveBeenCalledWith("/api/lab-results/measurements/99/");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["labs"] });
  });
});
