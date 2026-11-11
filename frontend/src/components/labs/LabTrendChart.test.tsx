import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { LabResultValue } from "@/federation/types";

import { LabTrendChart } from "./LabTrendChart";

function value(v: number, measured_at: string, unit: string | null): LabResultValue {
  return {
    measurement_id: Number(measured_at.replaceAll("-", "")),
    value: v,
    value_string: null,
    unit,
    status: "unknown" as LabResultValue["status"],
    measured_at,
    range_low: null,
    range_high: null,
    source: "ehr",
    lab_name: null,
    report_filename: null,
  };
}

describe("LabTrendChart", () => {
  it("charts unitless values when the page passes an empty unit", () => {
    // Respiratory rate from an EHR import: every value has unit null, and
    // LabResults passes `latest?.unit ?? ""`. Comparing null to "" dropped all
    // of them and the chart said "No data yet" above a full history.
    const values = [value(14.4, "2026-07-18", null), value(13.7, "2026-07-17", null)];

    render(<LabTrendChart values={values} unit="" />);

    expect(screen.queryByText("No data yet")).not.toBeInTheDocument();
  });

  it("still leaves out values recorded in a different unit", () => {
    const values = [value(5.1, "2026-07-18", "mmol/L"), value(92, "2026-07-17", "mg/dL")];

    render(<LabTrendChart values={values} unit="mmol/L" />);

    // One point in the chart's unit: the single-point view, not the empty state.
    expect(screen.queryByText("No data yet")).not.toBeInTheDocument();
    expect(screen.getByText(/Not enough data for a trend yet/)).toBeInTheDocument();
  });

  it("shows the empty state when no value is numeric", () => {
    const values = [{ ...value(0, "2026-07-18", null), value: null, value_string: "positive" }];

    render(<LabTrendChart values={values} unit="" />);

    expect(screen.getByText("No data yet")).toBeInTheDocument();
  });
});
