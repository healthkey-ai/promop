import { describe, expect, it } from "vitest";
import { linkedMetricKey, wearableMetricMappingsUrl } from "./mappingLinks";
import { rowMatchesSearch } from "./mappingSearch";
import type { CodeMappingRow } from "./codeMappingTypes";

describe("a link to a wearable metric's source codes", () => {
  it("round-trips a metric key through the address", () => {
    const url = wearableMetricMappingsUrl("resting_hr");
    expect(url).toBe("/code-mappings?direction=forward&metric=resting_hr");
    expect(linkedMetricKey(new URL(url, "http://x").searchParams)).toBe("resting_hr");
  });

  it("ignores a value that is not a metric key", () => {
    for (const bad of ["", "Steps", "9steps", "a b", "x".repeat(70), "a;b"]) {
      expect(linkedMetricKey(new URLSearchParams({ metric: bad }))).toBeUndefined();
    }
    expect(linkedMetricKey(new URLSearchParams())).toBeUndefined();
  });

  it("searches a metric key whole, as the server does", () => {
    const row = { metric_key: "resting_hr" } as CodeMappingRow;
    expect(rowMatchesSearch(row, "metric:resting_hr")).toBe(true);
    expect(rowMatchesSearch(row, "Metric_Key: RESTING_HR")).toBe(true);
    expect(rowMatchesSearch(row, "metric:hr")).toBe(false);
  });
});
