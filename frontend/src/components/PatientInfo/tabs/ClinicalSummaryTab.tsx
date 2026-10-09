/* eslint-disable promop/no-duplicate-tab-field -- read-only summary; no editable ClinicalField */
import { useCallback, useMemo } from "react";
import Section from "../Section";
import { useInfiniteOmopQuery } from "@/hooks/useInfiniteOmopQuery";
import { InfiniteScrollSentinel } from "@/components/UI/InfiniteScrollSentinel";
import { LabTrendChart } from "@/components/labs/LabTrendChart";
import { formatShortDate } from "@/lib/format";
import type {
  ConditionRow,
  DrugExposureRow,
  MeasurementRow,
  ObservationRow,
  ProcedureRow,
} from "@/types/omop";
import type { LabResultValue, LabValueStatus } from "@/federation/types";

interface Props {
  formData: Record<string, unknown>;
  onNavigateToLabs: () => void;
}

// ── Wearable field groups ──────────────────────────────────────────────────

interface WearableMetric {
  field: string;
  label: string;
  unit: string;
}

const WEARABLE_GROUPS: { title: string; metrics: WearableMetric[] }[] = [
  {
    title: "Cardiovascular",
    metrics: [
      { field: "resting_heart_rate_avg_30d", label: "Resting HR", unit: "bpm" },
      { field: "hrv_sdnn_avg_30d", label: "HRV SDNN", unit: "ms" },
      { field: "hrv_rmssd_avg_30d", label: "HRV RMSSD", unit: "ms" },
      { field: "oxygen_saturation_avg_30d", label: "SpO\u2082", unit: "%" },
      { field: "respiratory_rate_avg_30d", label: "Respiratory Rate", unit: "breaths/min" },
    ],
  },
  {
    title: "Activity",
    metrics: [
      { field: "median_daily_steps_30d", label: "Steps", unit: "steps/day" },
      { field: "active_minutes_per_day_30d", label: "Active Minutes", unit: "min/day" },
      { field: "distance_km_per_day_30d", label: "Distance", unit: "km/day" },
      { field: "activity_trend_30d", label: "Trend", unit: "" },
    ],
  },
  {
    title: "Sleep & Recovery",
    metrics: [
      { field: "sleep_duration_hours_avg_30d", label: "Sleep Duration", unit: "hrs" },
      { field: "vo2_max_avg_30d", label: "VO\u2082 Max", unit: "mL/kg/min" },
    ],
  },
  {
    title: "Gait & Mobility",
    metrics: [
      { field: "walking_speed_avg_30d", label: "Walking Speed", unit: "km/hr" },
      { field: "walking_step_length_avg_30d", label: "Step Length", unit: "cm" },
      { field: "walking_double_support_pct_avg_30d", label: "Double Support", unit: "%" },
      { field: "walking_hr_avg_30d", label: "Walking HR", unit: "bpm" },
    ],
  },
  {
    title: "Energy & Body",
    metrics: [
      { field: "active_energy_per_day_30d", label: "Active Energy", unit: "kcal/day" },
      { field: "basal_energy_per_day_30d", label: "Basal Energy", unit: "kcal/day" },
      { field: "flights_climbed_per_day_30d", label: "Flights Climbed", unit: "/day" },
      { field: "body_mass_avg_30d", label: "Body Mass", unit: "kg" },
    ],
  },
];

// ── Helpers ────────────────────────────────────────────────────────────────

function fmtDate(d: string | null | undefined): string {
  if (!d) return "\u2014";
  return formatShortDate(d);
}

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

interface MeasurementGroup {
  key: string;
  label: string;
  unit: string;
  values: LabResultValue[];
}

function groupMeasurements(rows: MeasurementRow[]): MeasurementGroup[] {
  const map = new Map<string, MeasurementRow[]>();
  for (const m of rows) {
    const key = m.measurement_source_value || `concept-${m.measurement_concept}`;
    const group = map.get(key);
    if (group) group.push(m);
    else map.set(key, [m]);
  }
  const groups: MeasurementGroup[] = [];
  for (const [key, items] of map) {
    const first = items[0];
    groups.push({
      key,
      label:
        first.concept_name ||
        first.measurement_source_value ||
        `Concept ${first.measurement_concept}`,
      unit: first.unit_source_value ?? "",
      values: items.map(measurementToLabValue),
    });
  }
  // Sort groups with most readings first
  groups.sort((a, b) => b.values.length - a.values.length);
  return groups;
}

function hasWearableData(formData: Record<string, unknown>): boolean {
  return WEARABLE_GROUPS.some((g) =>
    g.metrics.some((m) => formData[m.field] != null && formData[m.field] !== ""),
  );
}

function formatWearableValue(val: unknown, unit: string): string {
  if (val == null || val === "") return "\u2014";
  if (typeof val === "number") {
    const formatted = Number.isInteger(val) ? val.toLocaleString() : Number(val).toFixed(1);
    return unit ? `${formatted} ${unit}` : formatted;
  }
  return unit ? `${String(val)} ${unit}` : String(val);
}

// ── Sub-components ─────────────────────────────────────────────────────────

function sectionTitle(label: string, count: number): string {
  return count > 0 ? `${label} (${count})` : label;
}

function EmptySection({ label }: { label: string }) {
  return (
    <p className="py-3 text-sm italic text-muted-foreground">No {label} recorded.</p>
  );
}

function DataTable({
  headers,
  children,
}: {
  headers: string[];
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs font-medium text-muted-foreground">
            {headers.map((h) => (
              <th key={h} className="px-3 py-2">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">{children}</tbody>
      </table>
    </div>
  );
}

function StatCard({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 font-mono text-lg font-semibold text-foreground">{value}</p>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export default function ClinicalSummaryTab({ formData, onNavigateToLabs }: Props) {
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;

  // Fetch all five domains
  const conditions = useInfiniteOmopQuery<ConditionRow>("conditions", personId);
  const drugs = useInfiniteOmopQuery<DrugExposureRow>("drug-exposures", personId);
  const measurements = useInfiniteOmopQuery<MeasurementRow>("measurements", personId);
  const observations = useInfiniteOmopQuery<ObservationRow>("observations", personId);
  const procedures = useInfiniteOmopQuery<ProcedureRow>("procedures", personId);

  const measGroups = useMemo(
    () => groupMeasurements(measurements.allResults),
    [measurements.allResults],
  );

  const wearablePresent = hasWearableData(formData);

  const allEmpty =
    conditions.totalCount === 0 &&
    drugs.totalCount === 0 &&
    measurements.totalCount === 0 &&
    observations.totalCount === 0 &&
    procedures.totalCount === 0 &&
    !wearablePresent;

  const allLoaded =
    !conditions.isLoading &&
    !drugs.isLoading &&
    !measurements.isLoading &&
    !observations.isLoading &&
    !procedures.isLoading;

  // Stable callbacks for sentinels — deps intentionally list the three
  // properties rather than the whole query object to avoid churn.
  /* eslint-disable react-hooks/exhaustive-deps */
  const fetchMoreConditions = useCallback(() => {
    if (conditions.hasNextPage && !conditions.isFetchingNextPage) conditions.fetchNextPage();
  }, [conditions.hasNextPage, conditions.isFetchingNextPage, conditions.fetchNextPage]);
  const fetchMoreDrugs = useCallback(() => {
    if (drugs.hasNextPage && !drugs.isFetchingNextPage) drugs.fetchNextPage();
  }, [drugs.hasNextPage, drugs.isFetchingNextPage, drugs.fetchNextPage]);
  const fetchMoreMeasurements = useCallback(() => {
    if (measurements.hasNextPage && !measurements.isFetchingNextPage) measurements.fetchNextPage();
  }, [measurements.hasNextPage, measurements.isFetchingNextPage, measurements.fetchNextPage]);
  const fetchMoreObservations = useCallback(() => {
    if (observations.hasNextPage && !observations.isFetchingNextPage) observations.fetchNextPage();
  }, [observations.hasNextPage, observations.isFetchingNextPage, observations.fetchNextPage]);
  const fetchMoreProcedures = useCallback(() => {
    if (procedures.hasNextPage && !procedures.isFetchingNextPage) procedures.fetchNextPage();
  }, [procedures.hasNextPage, procedures.isFetchingNextPage, procedures.fetchNextPage]);
  /* eslint-enable react-hooks/exhaustive-deps */

  // Global empty state
  if (allLoaded && allEmpty) {
    return (
      <div className="rounded-md border border-dashed border-border bg-card p-8 text-center">
        <p className="text-base font-semibold text-foreground">No clinical data yet</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Go to the{" "}
          <button
            type="button"
            className="font-medium text-portal-brand hover:underline"
            onClick={onNavigateToLabs}
          >
            Labs tab
          </button>{" "}
          to enter data, or upload a FHIR bundle.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* ── Conditions ─────────────────────────────────────────────────── */}
      <Section title={sectionTitle("Conditions", conditions.totalCount)}>
        {conditions.totalCount === 0 && !conditions.isLoading ? (
          <EmptySection label="conditions" />
        ) : (
          <>
            <DataTable headers={["Condition", "Start Date", "End Date", "Status"]}>
              {conditions.allResults.map((c) => (
                <tr key={c.condition_occurrence_id}>
                  <td className="px-3 py-2 text-foreground">
                    {c.concept_name || c.condition_source_value || `Concept ${c.condition_concept}`}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(c.condition_start_date)}</td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(c.condition_end_date)}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {c.condition_status_source_value || "\u2014"}
                  </td>
                </tr>
              ))}
            </DataTable>
            {conditions.hasNextPage && (
              <InfiniteScrollSentinel
                onIntersect={fetchMoreConditions}
                loading={conditions.isFetchingNextPage}
              />
            )}
          </>
        )}
      </Section>

      {/* ── Medications ────────────────────────────────────────────────── */}
      <Section title={sectionTitle("Medications", drugs.totalCount)}>
        {drugs.totalCount === 0 && !drugs.isLoading ? (
          <EmptySection label="medications" />
        ) : (
          <>
            <DataTable headers={["Drug", "Start Date", "End Date", "Days Supply"]}>
              {drugs.allResults.map((d) => (
                <tr key={d.drug_exposure_id}>
                  <td className="px-3 py-2 text-foreground">
                    {d.concept_name || d.drug_source_value || `Concept ${d.drug_concept}`}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(d.drug_exposure_start_date)}</td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(d.drug_exposure_end_date)}</td>
                  <td className="px-3 py-2 text-muted-foreground">{d.days_supply ?? "\u2014"}</td>
                </tr>
              ))}
            </DataTable>
            {drugs.hasNextPage && (
              <InfiniteScrollSentinel
                onIntersect={fetchMoreDrugs}
                loading={drugs.isFetchingNextPage}
              />
            )}
          </>
        )}
      </Section>

      {/* ── Procedures ─────────────────────────────────────────────────── */}
      <Section title={sectionTitle("Procedures", procedures.totalCount)}>
        {procedures.totalCount === 0 && !procedures.isLoading ? (
          <EmptySection label="procedures" />
        ) : (
          <>
            <DataTable headers={["Procedure", "Date", "End Date"]}>
              {procedures.allResults.map((p) => (
                <tr key={p.procedure_occurrence_id}>
                  <td className="px-3 py-2 text-foreground">
                    {p.concept_name || p.procedure_source_value || `Concept ${p.procedure_concept}`}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(p.procedure_date)}</td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(p.procedure_end_date)}</td>
                </tr>
              ))}
            </DataTable>
            {procedures.hasNextPage && (
              <InfiniteScrollSentinel
                onIntersect={fetchMoreProcedures}
                loading={procedures.isFetchingNextPage}
              />
            )}
          </>
        )}
      </Section>

      {/* ── Lab Results (Measurements) ──────────────────────────────── */}
      <Section title={sectionTitle("Lab Results", measurements.totalCount)}>
        {measurements.totalCount === 0 && !measurements.isLoading ? (
          <EmptySection label="lab results" />
        ) : (
          <>
            {measGroups.map((group) => (
              <div key={group.key} className="mb-6 last:mb-0">
                <h4 className="mb-2 text-sm font-semibold text-foreground">{group.label}</h4>
                {group.values.length >= 3 ? (
                  <LabTrendChart values={group.values} unit={group.unit} />
                ) : (
                  <DataTable headers={["Date", "Value", "Unit"]}>
                    {group.values.map((v) => (
                      <tr key={v.measurement_id}>
                        <td className="px-3 py-2 text-muted-foreground">{fmtDate(v.measured_at)}</td>
                        <td className="px-3 py-2 text-foreground">
                          {v.value != null ? v.value : v.value_string ?? "\u2014"}
                        </td>
                        <td className="px-3 py-2 text-muted-foreground">{v.unit ?? "\u2014"}</td>
                      </tr>
                    ))}
                  </DataTable>
                )}
              </div>
            ))}
            {measurements.hasNextPage && (
              <InfiniteScrollSentinel
                onIntersect={fetchMoreMeasurements}
                loading={measurements.isFetchingNextPage}
              />
            )}
          </>
        )}
      </Section>

      {/* ── Observations ───────────────────────────────────────────────── */}
      <Section title={sectionTitle("Observations", observations.totalCount)}>
        {observations.totalCount === 0 && !observations.isLoading ? (
          <EmptySection label="observations" />
        ) : (
          <>
            <DataTable headers={["Observation", "Date", "Value"]}>
              {observations.allResults.map((o) => (
                <tr key={o.observation_id}>
                  <td className="px-3 py-2 text-foreground">
                    {o.concept_name || o.observation_source_value || `Concept ${o.observation_concept}`}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{fmtDate(o.observation_date)}</td>
                  <td className="px-3 py-2 text-foreground">
                    {o.value_as_number != null
                      ? o.value_as_number
                      : o.value_as_string ?? o.value_source_value ?? "\u2014"}
                  </td>
                </tr>
              ))}
            </DataTable>
            {observations.hasNextPage && (
              <InfiniteScrollSentinel
                onIntersect={fetchMoreObservations}
                loading={observations.isFetchingNextPage}
              />
            )}
          </>
        )}
      </Section>

      {/* ── Wearables ──────────────────────────────────────────────────── */}
      <Section title="Wearables (30-Day)">
        {!wearablePresent ? (
          <EmptySection label="wearable data" />
        ) : (
          <>
            {formData.wearable_last_sync_at && (
              <p className="mb-4 text-xs text-muted-foreground">
                Last sync:{" "}
                {new Date(formData.wearable_last_sync_at as string).toLocaleString()}
              </p>
            )}
            {WEARABLE_GROUPS.map((group) => {
              const populated = group.metrics.filter(
                (m) => formData[m.field] != null && formData[m.field] !== "",
              );
              if (populated.length === 0) return null;
              return (
                <div key={group.title} className="mb-5 last:mb-0">
                  <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {group.title}
                  </h4>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                    {populated.map((m) => (
                      <StatCard
                        key={m.field}
                        label={m.label}
                        value={formatWearableValue(formData[m.field], m.unit)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </>
        )}
      </Section>
    </div>
  );
}
