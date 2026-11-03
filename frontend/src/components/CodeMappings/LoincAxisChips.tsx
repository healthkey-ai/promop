import { useState } from "react";
import {
  AXIS_BASIS_LABELS, LOINC_AXES, LOINC_AXIS_LABELS, axisValueText, axisVariantTitle, describeAxisValues,
  type AxisExcludedCandidate, type AxisVariant, type AxisVerdict, type LoincAxis, type SourceAxes,
} from "./loincAxes";
import { isSnomedExcluded } from "./snomed";

const VERDICT_STYLE: Record<AxisVerdict, string> = {
  match: "bg-green-100 text-green-800",
  compatible: "bg-amber-100 text-amber-800",
  conflict: "bg-rose-100 text-rose-800 font-semibold",
  unknown: "bg-slate-100 text-slate-500",
};

/** The blue "n axis values" flag next to a source code. */
export function AxisVariantFlag({ count }: { count?: number | null }) {
  if (!count || count < 2) return null;
  return <span className="ml-1.5 whitespace-nowrap font-sans text-xs font-medium text-blue-600"
    title={axisVariantTitle(count)}>{count} axis values</span>;
}

/**
 * A source code's axis variants under its row: the axes its units imply, the
 * units, and the LOINC each is routed to, with Approve for a proposed one (#2012).
 */
export function AxisVariantList({ variants, canApprove, busy = false, onApprove }: {
  variants?: AxisVariant[] | null;
  canApprove: boolean;
  busy?: boolean;
  onApprove?: (variant: AxisVariant) => void;
}) {
  if (!variants || variants.length < 2) return null;
  return <ul aria-label="Axis variants" className="mt-1 space-y-0.5 font-sans text-xs text-slate-700">
    {variants.map(variant => <li key={variant.axis_key} className="flex flex-wrap items-center gap-x-1.5">
      <span>{describeAxisValues(variant.axis_values)}</span>
      <span className="font-mono text-slate-500">{variant.units?.join(", ") || "—"}</span>
      <span aria-hidden="true">→</span>
      {variant.target_concept
        ? <span>{variant.target_concept.concept_name}
            <span className="ml-1 text-slate-500">{variant.target_concept.vocabulary_id}:{variant.target_concept.concept_code}</span></span>
        : <span className="text-slate-500">no destination yet</span>}
      <span className={variant.status === "approved" ? "font-medium text-green-800" : "text-slate-500"}>{variant.status}</span>
      {canApprove && onApprove && variant.status !== "approved" && variant.target_concept && <button type="button"
        className="rounded border border-green-300 bg-green-50 px-1.5 py-0.5 text-[11px] font-medium text-green-800 hover:bg-green-100 disabled:opacity-40"
        disabled={busy} aria-label={`Approve ${describeAxisValues(variant.axis_values)}`}
        title="Approve and re-point this variant's stored rows" onClick={() => onApprove(variant)}>Approve</button>}
    </li>)}
  </ul>;
}

/** A candidate LOINC's six axes, each coloured by how it stands against the source. */
export function AxisChips({ axes, verdicts, conflicts, matched }: {
  axes?: Partial<Record<LoincAxis, string>> | null;
  verdicts?: Partial<Record<LoincAxis, AxisVerdict>>;
  conflicts?: LoincAxis[];
  matched?: number;
}) {
  if (!axes) return null;
  return <span className="mt-0.5 flex flex-wrap items-center gap-0.5" aria-label="LOINC axes">
    {LOINC_AXES.map(axis => {
      const verdict: AxisVerdict = conflicts?.includes(axis) ? "conflict" : verdicts?.[axis] ?? "unknown";
      const value = axes[axis] || "—";
      return <span key={axis} data-axis={axis} data-verdict={verdict}
        title={`${LOINC_AXIS_LABELS[axis]}: ${value} (${verdict})`}
        className={`rounded px-1 py-px text-[11px] ${VERDICT_STYLE[verdict]}`}>
        {verdict === "compatible" ? "≈" : ""}{value}
      </span>;
    })}
    {matched != null && <span className="ml-1 text-[11px] text-slate-500">{matched}/6 axes</span>}
  </span>;
}

/** What the source settles on each axis, above a candidate list. */
export function SourceAxesSummary({ sourceAxes }: { sourceAxes?: SourceAxes | null }) {
  if (!sourceAxes) return null;
  const settled = LOINC_AXES.filter(axis => sourceAxes.axes?.[axis]);
  const hints = LOINC_AXES.filter(axis => sourceAxes.hints?.[axis]);
  const expected = sourceAxes.expected_vocabulary;
  if (!settled.length && !hints.length && !expected) return null;
  return <div className="mt-1 text-xs text-slate-600" aria-label="Source LOINC axes">
    {expected && <div className="text-blue-700" data-testid="expected-vocabulary">
      <span className="font-medium">{expected.vocabulary_id} preferred:</span> {expected.reason}
    </div>}
    <span className="font-medium text-slate-700">Source axes: </span>
    {settled.length ? settled.map((axis, index) => {
      const entry = sourceAxes.axes[axis]!;
      const basis = `${AXIS_BASIS_LABELS[entry.basis] ?? entry.basis}${entry.evidence ? `: ${entry.evidence}` : ""}`;
      return <span key={axis} title={Array.isArray(entry.value) ? `${axisValueText(entry.value, { full: true })} — ${basis}` : basis}>
        {index > 0 && " · "}{LOINC_AXIS_LABELS[axis]} {axisValueText(entry.value, { empty: "(methodless)" })}
      </span>;
    }) : "none settled"}
    {hints.length > 0 && <span className="ml-2 text-slate-500">
      (Hints: {hints.map(axis => `${LOINC_AXIS_LABELS[axis]} ${sourceAxes.hints[axis]}`).join("; ")})
    </span>}
  </div>;
}

/** "k candidates hidden: conflict on System, Property", expandable. */
export function AxisExcludedDisclosure({ excluded: all }: { excluded?: AxisExcludedCandidate[] }) {
  const [open, setOpen] = useState(false);
  // SNOMED entries have their own disclosure (SnomedExcludedDisclosure).
  const excluded = all?.filter(candidate => !isSnomedExcluded(candidate));
  if (!excluded?.length) return null;
  const axes = LOINC_AXES.filter(axis => excluded.some(c => c.axis_conflicts?.includes(axis)));
  const n = excluded.length;
  return <div className="mt-1 text-xs">
    <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}
      className="text-left text-slate-600 underline underline-offset-2 hover:text-slate-900">
      {n} candidate{n === 1 ? "" : "s"} hidden: conflict on {axes.map(axis => LOINC_AXIS_LABELS[axis]).join(", ") || "an axis"}
    </button>
    {open && <ul aria-label="Candidates hidden by LOINC axes" className="mt-1 space-y-1 rounded border border-slate-200 bg-white p-2">
      {excluded.map(candidate => <li key={candidate.concept_id}>
        <span>{candidate.concept_name}</span>
        <span className="ml-1 text-slate-500">{candidate.vocabulary_id}:{candidate.concept_code}</span>
        <AxisChips axes={candidate.loinc_axes} verdicts={candidate.axis_match} conflicts={candidate.axis_conflicts} />
      </li>)}
    </ul>}
  </div>;
}
