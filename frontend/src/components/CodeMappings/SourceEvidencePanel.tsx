export interface SourceDistribution {
  min?: number | null;
  p5?: number | null;
  p25?: number | null;
  p50?: number | null;
  p75?: number | null;
  p95?: number | null;
  max?: number | null;
}

export interface SourceUnitEvidence {
  display?: string;
  code?: string;
  count: number;
  patients?: number;
  values?: number;
  suppressed?: boolean;
  distribution?: SourceDistribution | null;
}

interface SourceMetadata {
  records?: number;
  patients?: number;
  codings?: number;
  unit_coverage?: { records?: number; percent?: number | null };
  reference_range?: {
    records?: number;
    percent?: number | null;
    low_p50?: number | null;
    high_p50?: number | null;
    unit?: string;
  };
  category?: { top?: string; mix?: string };
  value_types?: Record<string, number>;
}

export interface SourceEvidence {
  organization: { id: number; slug: string; name: string } | null;
  occurrence_count: number;
  group_occurrence_count?: number | null;
  first_seen?: string | null;
  last_seen?: string | null;
  metadata?: SourceMetadata;
  units?: SourceUnitEvidence[];
}

const distributionOrder: Array<[keyof SourceDistribution, string]> = [
  ["min", "Min"], ["p5", "P5"], ["p25", "P25"], ["p50", "Median"],
  ["p75", "P75"], ["p95", "P95"], ["max", "Max"],
];

function number(value: number | null | undefined) {
  return value == null ? "—" : new Intl.NumberFormat(undefined, { maximumFractionDigits: 5 }).format(value);
}

function Stat({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return <div className="rounded border border-slate-200 bg-white px-3 py-2">
    <dt className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
    <dd className="mt-0.5 text-sm font-medium text-slate-900">{value}</dd>
    {detail && <dd className="mt-0.5 text-xs text-slate-500">{detail}</dd>}
  </div>;
}

export default function SourceEvidencePanel({ evidence, loading, error }: {
  evidence: SourceEvidence | null;
  loading: boolean;
  error?: string;
}) {
  const metadata = evidence?.metadata || {};
  const units = evidence?.units || [];
  const range = metadata.reference_range;
  const valueTypes = metadata.value_types || {};
  const selectedValueTypeTotal = Object.values(valueTypes).reduce((sum, value) => sum + value, 0);
  return <section aria-label="Source evidence" className="mb-5 rounded-md border border-slate-200 bg-slate-50 p-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">Source evidence</h3>
      <span className="text-xs text-slate-500">Read-only evidence supplied by the source</span>
    </div>
    {loading && <p role="status" className="mt-3 text-sm text-slate-600">Loading source evidence…</p>}
    {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
    {evidence && <>
      <dl className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Organization" value={evidence.organization?.name || "Global / unattributed"}
          detail={evidence.organization?.slug || "Awaiting hospital provenance"} />
        <Stat label="Records (this code)" value={number(evidence.occurrence_count)} detail="Distinct source records" />
        {metadata.patients != null && <Stat label="Patients" value={number(metadata.patients)} />}
        {metadata.codings != null && metadata.codings !== evidence.occurrence_count && (
          <Stat label="Coding occurrences" value={number(metadata.codings)}
            detail="A source record can repeat the same code" />
        )}
        {metadata.unit_coverage?.records != null && <Stat label="Records with units"
          value={number(metadata.unit_coverage.records)}
          detail={metadata.unit_coverage.percent != null ? `${number(metadata.unit_coverage.percent)}% of source records` : undefined} />}
        {evidence.group_occurrence_count != null && <Stat label="Records across same-label codes"
          value={number(evidence.group_occurrence_count)}
          detail="Deduplicated records across codes with this description" />}
      </dl>

      {units.length > 0 && <div className="mt-4 overflow-x-auto">
        <table aria-label="Observed source units" className="w-full text-left text-xs">
          <thead className="text-slate-500"><tr>
            <th className="pb-2 pr-3 font-semibold">Raw unit</th>
            <th className="pb-2 pr-3 text-right font-semibold">Records</th>
            <th className="pb-2 font-semibold">Value distribution</th>
          </tr></thead>
          <tbody className="divide-y divide-slate-200">
            {units.map((unit, index) => <tr key={`${unit.display || ""}|${unit.code || ""}|${index}`}>
              <td className="py-2 pr-3 font-mono text-slate-900">
                {unit.display || unit.code || "<no unit>"}
                {unit.code && unit.code !== unit.display && <span className="ml-1 text-slate-500">({unit.code})</span>}
              </td>
              <td className="py-2 pr-3 text-right text-slate-700">
                <span>{number(unit.count)}</span>
                {(unit.patients != null || unit.values != null) && <span className="block text-[10px] text-slate-500">
                  {unit.patients != null ? `${number(unit.patients)} patients` : ""}
                  {unit.patients != null && unit.values != null ? " · " : ""}
                  {unit.values != null ? `${number(unit.values)} numeric values` : ""}
                </span>}
              </td>
              <td className="py-2 text-slate-700">
                {unit.suppressed
                  ? <span className="font-medium text-amber-700">Suppressed for a small cohort</span>
                  : unit.distribution
                    ? <dl className="flex flex-wrap gap-x-3 gap-y-1">
                        {distributionOrder.map(([key, label]) => unit.distribution?.[key] == null ? null :
                          <div key={key} className="flex gap-1"><dt className="text-slate-500">{label}</dt><dd>{number(unit.distribution[key])}</dd></div>)}
                      </dl>
                    : "No numeric distribution supplied"}
              </td>
            </tr>)}
          </tbody>
        </table>
      </div>}

      {range && <div className="mt-4 rounded border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-950">
        <span className="font-semibold">Source reference-range median: </span>
        {number(range.low_p50)}–{number(range.high_p50)}{range.unit ? ` ${range.unit}` : ""}
        {range.records != null && <span className="ml-2 text-xs text-sky-800">
          ({number(range.records)} records{range.percent != null ? `, ${number(range.percent)}%` : ""})
        </span>}
        <p className="mt-1 text-xs text-sky-800">Recognition evidence only; FHIR ranges can vary by age, sex, and population.</p>
      </div>}

      {(metadata.category?.top || metadata.category?.mix || metadata.value_types) && <dl className="mt-4 grid gap-2 text-xs sm:grid-cols-2">
        {(metadata.category?.top || metadata.category?.mix) && <div><dt className="font-semibold text-slate-600">Observation category</dt>
          <dd className="text-slate-800">{metadata.category.mix || metadata.category.top}</dd></div>}
        {metadata.value_types && <div><dt className="font-semibold text-slate-600">Selected FHIR value types</dt>
          <dd className="text-slate-800">{Object.entries(metadata.value_types).map(([key, value]) => `${key} ${number(value)}%`).join(" · ")}</dd>
          {selectedValueTypeTotal === 0 && <dd className="mt-0.5 text-slate-500">
            Values use another FHIR type or are absent; the source extract does not distinguish those cases.
          </dd>}
        </div>}
      </dl>}
    </>}
  </section>;
}
