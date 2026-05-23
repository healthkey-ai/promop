import { renderHook } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import axios from "axios";
import type { ReactNode } from "react";
import { LabsContext, useLabsContext } from "./LabsContext";

describe("useLabsContext", () => {
  it("throws when used outside LabsProvider", () => {
    expect(() => {
      renderHook(() => useLabsContext());
    }).toThrow("useLabsContext must be used inside <LabsProvider>");
  });

  it("returns context value when inside provider", () => {
    const client = axios.create();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <LabsContext.Provider value={{ apiClient: client, apiBasePath: "/custom" }}>
        {children}
      </LabsContext.Provider>
    );

    const { result } = renderHook(() => useLabsContext(), { wrapper });
    expect(result.current.apiClient).toBe(client);
    expect(result.current.apiBasePath).toBe("/custom");
  });
});
