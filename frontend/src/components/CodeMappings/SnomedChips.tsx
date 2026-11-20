import { useState } from "react";
import type { AxisExcludedCandidate } from "./loincAxes";
import {
  REFERENCE_BASIS_LABELS, basisLabel, isSnomedExcluded,
  type SnomedCandidateInfo, type SnomedVerdict, type SourceSnomed,
} from "./snomed";

const VERDICT_STYLE: Record<SnomedVerdict, string> = {
  match: "bg-green-100 text-green-800",
  general: "bg-amber-100 text-amber-800",
  specific: "bg-orange-100 text-orange-800",
  conflict: "bg-rose-100 text-rose-800 font-semibold",
  unknown: "bg-slate-100 text-slate-500",
};

const PRIMITIVE_GAP = "Primitive concept: attribute not defined, not a mismatch";

/** A SNOMED candidate's tag, relation to the reference, attributes and side. */
export function SnomedLine({ snomed }: { snomed?: SnomedCandidateInfo | null }) {
  if (!snomed) return null;
  const { relation, levels } = snomed;
  return <span className="mt-0.5 flex flex-wrap items-center gap-0.5 text-[11px]" aria-label="SNOMED evidence">
    {snomed.semantic_tag && <span className="text-slate-500" title={snomed.hierarchy}>({snomed.semantic_tag})</span>}
    {relation === "exact" && <span data-relation="exact" className="rounded bg-green-100 px-1 py-px font-medium text-green-800"
      title="Means what the source's reference concept means">exact</span>}
    {snomed.lateralised && <span className="text-green-700" title="Exact because it restores the side the source names">lateralised</span>}
    {relation === "general" && <span data-relation="general" className="rounded bg-amber-100 px-1 py-px font-medium text-amber-800"
      title="An ancestor of the reference concept: allowed, ranked lower">↑ {levels ?? 1} more general</span>}
    {relation === "specific" && <span data-relation="specific" className="rounded bg-orange-100 px-1 py-px font-medium text-orange-800"
      title="A descendant of the reference concept: needs curator evidence">↓ {levels ? `${levels} ` : ""}more specific</span>}
    {snomed.status === "Primitive" && <span className="rounded bg-slate-200 px-1 py-px text-slate-600"
      title="Primitive concept: its definition is incomplete">primitive</span>}
    {snomed.attributes?.map(attribute => {
      const verdict: SnomedVerdict = attribute.verdict ?? "unknown";
      const values = attribute.values.join(", ") || "—";
      return <span key={attribute.relationship_id} data-attribute={attribute.relationship_id} data-verdict={verdict}
        title={attribute.primitive_gap ? PRIMITIVE_GAP : `${attribute.label}: ${values} (${verdict})`}
        className={`rounded px-1 py-px ${VERDICT_STYLE[verdict]}`}>
        {attribute.label}: {values}
      </span>;
    })}
    {snomed.laterality && <span className="text-slate-600">Side: {snomed.laterality}</span>}
    {snomed.laterality_not_captured && <span role="note" className="basis-full rounded bg-amber-50 px-1 text-amber-800">
      Source says {snomed.laterality_not_captured}; this concept has no side — record laterality separately.
    </span>}
  </span>;
}

/** "Source means at least: X (maps to) · Disorder · Right", above a candidate list. */
export function SourceSnomedSummary({ sourceSnomed }: { sourceSnomed?: SourceSnomed | null }) {
  if (!sourceSnomed) return null;
  const references = sourceSnomed.references ?? [];
  if (!references.length && !sourceSnomed.hierarchy && !sourceSnomed.laterality) return null;
  const basis = sourceSnomed.reference_basis ? REFERENCE_BASIS_LABELS[sourceSnomed.reference_basis] ?? sourceSnomed.reference_basis : "";
  const parts: { key: string; text: string; title?: string }[] = [];
  if (references.length) {
    parts.push({ key: "ref", text: `${references.map(r => r.concept_name).join(" / ")}${basis ? ` (${basis.toLowerCase()})` : ""}`,
      title: references.map(r => `SNOMED:${r.concept_code}`).join(", ") });
  }
  if (sourceSnomed.hierarchy) parts.push({ key: "hierarchy", text: sourceSnomed.hierarchy.value,
    title: `${basisLabel(sourceSnomed.hierarchy.basis)}${sourceSnomed.hierarchy.evidence ? `: ${sourceSnomed.hierarchy.evidence}` : ""}` });
  if (sourceSnomed.laterality) parts.push({ key: "laterality", text: sourceSnomed.laterality.value,
    title: `${basisLabel(sourceSnomed.laterality.basis)}${sourceSnomed.laterality.evidence ? `: ${sourceSnomed.laterality.evidence}` : ""}` });
  return <div className="mt-1 text-xs text-slate-600" aria-label="Source SNOMED meaning">
    <span className="font-medium text-slate-700">Source means at least: </span>
    {parts.map((part, index) => <span key={part.key} title={part.title}>{index > 0 && " · "}{part.text}</span>)}
  </div>;
}

/**
 * SNOMED candidates Suggest held back: those that contradict the source, and
 * those more specific than it supports. With onUseAnyway a more specific one
 * can still be chosen, when the curator has the evidence the source lacks.
 */
export function SnomedExcludedDisclosure({ excluded, onUseAnyway }: {
  excluded?: AxisExcludedCandidate[];
  onUseAnyway?: (candidate: AxisExcludedCandidate) => void;
}) {
  const snomed = (excluded ?? []).filter(isSnomedExcluded);
  const specific = snomed.filter(c => c.exclusion_reason === "more_specific");
  const conflicts = snomed.filter(c => c.exclusion_reason !== "more_specific");
  if (!snomed.length) return null;
  return <div className="mt-1 space-y-0.5 text-xs">
    <ExcludedGroup label={`${conflicts.length} contradict the source`} listLabel="SNOMED candidates that contradict the source"
      candidates={conflicts} conflicts />
    <ExcludedGroup label={`${specific.length} more specific than the source supports — need curator evidence`}
      listLabel="SNOMED candidates more specific than the source" candidates={specific} onUseAnyway={onUseAnyway} />
  </div>;
}

function ExcludedGroup({ label, listLabel, candidates, conflicts = false, onUseAnyway }: {
  label: string;
  listLabel: string;
  candidates: AxisExcludedCandidate[];
  conflicts?: boolean;
  onUseAnyway?: (candidate: AxisExcludedCandidate) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!candidates.length) return null;
  const labels = [...new Set(candidates.flatMap(c => c.snomed_conflicts ?? []))];
  return <div>
    <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}
      className={`text-left underline underline-offset-2 hover:text-slate-900 ${conflicts ? "text-rose-700" : "text-amber-800"}`}>
      {label}{conflicts && labels.length > 0 ? `: ${labels.join(", ")}` : ""}
    </button>
    {open && <ul aria-label={listLabel} className="mt-1 space-y-1 rounded border border-slate-200 bg-white p-2">
      {candidates.map(candidate => <li key={candidate.concept_id}>
        <span>{candidate.concept_name}</span>
        <span className="ml-1 text-slate-500">{candidate.vocabulary_id}:{candidate.concept_code}</span>
        {conflicts && (candidate.snomed_conflicts?.length ?? 0) > 0 && <span className="ml-1 font-medium text-rose-700">
          conflict on {candidate.snomed_conflicts!.join(", ")}</span>}
        {onUseAnyway && !conflicts && <button type="button"
          className="ml-2 rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 hover:bg-amber-100"
          aria-label={`Use ${candidate.concept_name} anyway`}
          title="You have evidence the source supports this more specific concept"
          onClick={() => onUseAnyway(candidate)}>Use anyway</button>}
        <SnomedLine snomed={candidate.snomed} />
      </li>)}
    </ul>}
  </div>;
}
