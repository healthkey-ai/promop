import { useMemo } from "react";
import { useInfiniteOmopQuery } from "@/hooks/useInfiniteOmopQuery";
import type { MeasurementRow } from "@/types/omop";
import type { LabResultValue, LabValueStatus } from "@/federation/types";

function deriveStatus(
  value: number | null,
  rangeLow: number | null,
  rangeHigh: number | null,
): LabValueStatus {
  if (value == null || (rangeLow == null && rangeHigh == null)) return "unknown";
  if (rangeLow != null && value < rangeLow) return "below";
  if (rangeHigh != null && value > rangeHigh) return "above";
  return "in_range";
}

function measurementToLabValue(m: MeasurementRow): LabResultValue {
  return {
    measurement_id: m.measurement_id,
    value: m.value_as_number,
    value_string: m.value_as_string,
    unit: m.unit_source_value,
    status: deriveStatus(m.value_as_number, m.range_low, m.range_high),
    measured_at: m.measurement_date ?? m.measurement_datetime ?? "",
    range_low: m.range_low,
    range_high: m.range_high,
    source: m.measurement_source_value,
    lab_name: null,
    report_filename: null,
  };
}

export interface LabMeasurementGroup {
  sourceValue: string;
  unit: string;
  values: LabResultValue[];
}

/**
 * Fetches all measurements for a person and groups them by source_value.
 * Used by LabsTab to show trend chart icons next to fields with 3+ data points.
 */
export function useLabMeasurements(personId: number | undefined) {
  const { allResults, isLoading } = useInfiniteOmopQuery<MeasurementRow>(
    "measurements",
    personId,
  );

  const grouped = useMemo(() => {
    const map = new Map<string, MeasurementRow[]>();
    for (const m of allResults) {
      const key = m.measurement_source_value;
      if (!key) continue;
      const group = map.get(key);
      if (group) group.push(m);
      else map.set(key, [m]);
    }
    const result = new Map<string, LabMeasurementGroup>();
    for (const [key, items] of map) {
      result.set(key, {
        sourceValue: key,
        unit: items[0].unit_source_value ?? "",
        values: items.map(measurementToLabValue),
      });
    }
    return result;
  }, [allResults]);

  return { grouped, isLoading };
}
