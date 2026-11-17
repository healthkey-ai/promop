import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useCustomPatientFields } from "./useCustomPatientFields";

const mockedApi = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/axios", () => ({ default: mockedApi }));

const FIELD = {
  id: 1, field_name: "tumor_note", display_name: "Tumor note", tab: "disease",
  field_type: "text", mode: "editable", unit: "", scope: "disease", diseases: ["MM"],
};

describe("useCustomPatientFields", () => {
  beforeEach(() => vi.clearAllMocks());

  it("reads the added fields once", async () => {
    mockedApi.get.mockResolvedValue({ data: [FIELD] });
    const { result, rerender } = renderHook(() => useCustomPatientFields());
    expect(result.current).toEqual([]);
    await waitFor(() => expect(result.current).toEqual([FIELD]));
    rerender();
    expect(mockedApi.get).toHaveBeenCalledTimes(1);
    expect(mockedApi.get).toHaveBeenCalledWith("/v1/custom-patient-fields/");
  });

  it("accepts a paginated envelope", async () => {
    mockedApi.get.mockResolvedValue({ data: { results: [FIELD] } });
    const { result } = renderHook(() => useCustomPatientFields());
    await waitFor(() => expect(result.current).toEqual([FIELD]));
  });

  it("is empty for anything that is not a list, and when the read fails", async () => {
    mockedApi.get.mockResolvedValue({ data: "<!doctype html>" });
    const html = renderHook(() => useCustomPatientFields());
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalledTimes(1));
    expect(html.result.current).toEqual([]);

    mockedApi.get.mockRejectedValue(new Error("offline"));
    const failed = renderHook(() => useCustomPatientFields());
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalledTimes(2));
    expect(failed.result.current).toEqual([]);
  });
});
