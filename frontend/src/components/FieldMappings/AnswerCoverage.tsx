import type { AnswerCoverage as Coverage, CoverageFilter } from "./answerConcept";

interface Props {
  coverage: Coverage;
  filter: CoverageFilter;
  onFilter: (filter: CoverageFilter) => void;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** How many of a field's answers have a concept decision, and a way to list
 *  just the ones that need work (#1225). This is the answers only: the
 *  field's own question concept is mapped on the field. */
export function AnswerCoverage({ coverage, filter, onFilter }: Props) {
  const decided = coverage.mapped + coverage.no_equivalent;
  const chips: { key: CoverageFilter; label: string; count: number }[] = [
    { key: "all", label: "All", count: coverage.offered + coverage.retired },
    { key: "unmapped", label: "No decision", count: coverage.unmapped },
    { key: "proposed", label: "Proposed", count: coverage.proposed },
    { key: "attention", label: "Needs attention", count: coverage.attention },
    { key: "done", label: "Decided", count: decided },
    { key: "retired", label: "Retired", count: coverage.retired },
  ];
  return (
    <div className="mb-3 rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-600">
      <div role="status">
        {decided} of {plural(coverage.offered, "answer", "answers")} decided
        {decided > 0 && ` (${coverage.mapped} with a concept, ${coverage.no_equivalent} with no standard equivalent)`}
        {coverage.proposed > 0 && `, ${coverage.proposed} waiting for approval`}
        {coverage.attention > 0 && `, ${coverage.attention} with an approved concept that can no longer be used`}
        {coverage.unmapped > 0 && `, ${coverage.unmapped} with no decision`}.
        {" "}Retired answers are not counted.
      </div>
      <div role="group" aria-label="Show answers" className="mt-1 flex flex-wrap gap-1">
        {chips.filter((c) => c.key === "all" || c.count > 0 || filter === c.key).map((c) => (
          <button key={c.key} type="button" aria-pressed={filter === c.key} onClick={() => onFilter(c.key)}
            className={`rounded border px-2 py-0.5 ${filter === c.key
              ? "border-primary bg-primary text-primary-foreground"
              : "border-gray-300 bg-white text-gray-700"}`}>
            {c.label} {c.count}
          </button>
        ))}
      </div>
      <div className="mt-1 text-[10px] text-gray-400">
        This counts the answers only. The field's own question concept is mapped on the field.
      </div>
    </div>
  );
}
