import { renderHook, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { AxiosInstance } from "axios";
import { LabsContext } from "./LabsContext";
import { useLabResultsSummary, useLabValues } from "./hooks";

function createMockClient() {
  return {
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  } as unknown as AxiosInstance;
}

let qc: QueryClient;
let client: AxiosInstance;

function createWrapper(apiBasePath = "/api") {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <LabsContext.Provider value={{ apiClient: client, apiBasePath }}>
          {children}
        </LabsContext.Provider>
      </QueryClientProvider>
    );
  };
}

beforeEach(() => {
  qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client = createMockClient();
});

describe("useLabResultsSummary (context-wrapped)", () => {
  it("uses apiClient and apiBasePath from context", async () => {
    (client.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { count: 0, next: null, previous: null, results: [] },
    });

    const { result } = renderHook(
      () => useLabResultsSummary({ page: 1, pageSize: 25 }),
      { wrapper: createWrapper("/custom") },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith(
      "/custom/lab-results/summary/",
      expect.objectContaining({ params: { page: 1, page_size: 25 } }),
    );
  });
});

describe("useLabValues (context-wrapped)", () => {
  it("uses apiClient and apiBasePath from context", async () => {
    (client.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        count: 0, next: null, previous: null, results: [],
        concept_id: 1, concept_code: "718-7", concept_name: "Hgb",
        vocabulary_id: "LOINC", category: "CHEM",
      },
    });

    const { result } = renderHook(
      () => useLabValues({ conceptCode: "718-7", page: 1 }),
      { wrapper: createWrapper("/v2") },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.get).toHaveBeenCalledWith(
      "/v2/lab-results/values/",
      expect.objectContaining({ params: { concept_code: "718-7", page: 1, page_size: 50 } }),
    );
  });
});
