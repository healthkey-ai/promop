/** What an answer's concept mapping means to a curator (#1225).
 *
 *  The server holds one decision in force and at most one open proposal per
 *  answer and role. The editor only works with `value_as_concept`: the other
 *  roles (`main_concept`, `context_recipe`) are recorded by the API but nothing
 *  projects them yet. */

export interface ConceptBrief {
  concept_id: number;
  vocabulary_id: string;
  concept_code: string;
  concept_name: string;
  domain_id?: string;
}

export interface ValueMapping {
  id: number;
  role: string;
  outcome: "mapped" | "no_equivalent";
  status: "proposed" | "approved" | "rejected" | "superseded";
  target: ConceptBrief | null;
  vocabulary_id: string;
  concept_code: string;
  source_resolution: string;
  target_state: "ok" | "retired" | "non_standard" | "invalid" | "unresolved" | "missing" | null;
  rationale: string;
  vocabulary_release: string;
  proposed_by: string | null;
  reviewer: string | null;
}

export type AnswerStateKind =
  | "unmapped"
  | "proposed"
  | "approved"
  | "approved_no_equivalent"
  | "approved_invalid";

export interface AnswerState {
  kind: AnswerStateKind;
  /** One line for the curator. */
  summary: string;
  /** What is wrong, when the decision in force cannot be used. */
  warning: string | null;
  approved: ValueMapping | null;
  proposed: ValueMapping | null;
}

const label = (m: ValueMapping): string =>
  m.target
    ? `${m.target.vocabulary_id} ${m.target.concept_code} ${m.target.concept_name}`
    : m.vocabulary_id && m.concept_code
      ? `${m.vocabulary_id} ${m.concept_code}`
      : "no concept";

const INVALID_TARGET: Record<string, string> = {
  retired: "The approved concept has been retired. The value is saved without a concept until a current one is approved.",
  non_standard: "The approved concept is no longer standard. The value is saved without a concept until a standard one is approved.",
  invalid: "The approved concept no longer fits this field. The value is saved without a concept until it is replaced.",
  unresolved: "The approved concept is not loaded in this instance.",
  missing: "The approved concept is missing.",
};

/** The state of one answer from its `value_mappings`, as the choice list carries them. */
export function answerState(mappings: ValueMapping[] | undefined): AnswerState {
  const own = (mappings ?? []).filter((m) => m.role === "value_as_concept");
  const approved = own.find((m) => m.status === "approved") ?? null;
  const proposed = own.find((m) => m.status === "proposed") ?? null;

  if (approved) {
    if (approved.outcome === "no_equivalent") {
      return {
        kind: "approved_no_equivalent", approved, proposed, warning: null,
        summary: "Reviewed: no standard concept for this answer. No concept is written; the value is saved as normal.",
      };
    }
    if (approved.target_state && approved.target_state !== "ok") {
      return {
        kind: "approved_invalid", approved, proposed,
        summary: `Approved: ${label(approved)}`,
        warning: INVALID_TARGET[approved.target_state] ?? "The approved concept cannot be used.",
      };
    }
    return { kind: "approved", approved, proposed, warning: null, summary: `Approved: ${label(approved)}` };
  }
  if (proposed) {
    const unresolved = proposed.outcome === "mapped" && !proposed.target;
    return {
      kind: "proposed", approved: null, proposed,
      warning: unresolved
        ? `${label(proposed)} is not loaded here, or is no longer a valid concept, so it cannot be approved yet.`
        : null,
      summary: proposed.outcome === "no_equivalent"
        ? "Proposed: no standard concept for this answer (waiting for approval)"
        : `Proposed: ${label(proposed)} (waiting for approval)`,
    };
  }
  return {
    kind: "unmapped", approved: null, proposed: null, warning: null,
    summary: "No concept approved. An answer with a legacy code may still write that code's concept; one whose proposal was rejected, or whose approval was withdrawn, writes none.",
  };
}

/** The server's own reason, else a fixed sentence: never the raw error. */
export function errorText(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

/** Why a save was refused. A serializer answers with `{field: [messages]}` and
 *  the mapping endpoints with `{detail}`; both are the server's own wording.
 *  Anything else is the fixed fallback, never the raw error. */
export function refusalText(error: unknown, fallback: string): string {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  const parts: string[] = [];
  // A list field's errors are keyed by position: `{aliases: {"0": ["Too long."]}}`.
  const collect = (value: unknown, depth: number) => {
    if (typeof value === "string") parts.push(value);
    else if (Array.isArray(value)) value.forEach((v) => collect(v, depth + 1));
    else if (value && typeof value === "object" && depth < 3) {
      Object.values(value as Record<string, unknown>).forEach((v) => collect(v, depth + 1));
    }
  };
  if (data && typeof data === "object") collect(data, 0);
  return parts.length ? parts.join(" ") : fallback;
}

/** One transition of an answer's concept decision, as the revisions endpoint returns it. */
export interface MappingRevision {
  revision: number;
  action: string;
  status: string;
  outcome: "mapped" | "no_equivalent";
  role: string;
  vocabulary_id: string;
  concept_code: string;
  rationale: string;
  vocabulary_release: string;
  changed_by: string;
  created_at: string;
}

export interface HistoryEntry extends MappingRevision {
  /** Which decision it belongs to; revisions of one decision share it. */
  mapping_id: number;
}

const ACTION_TEXT: Record<string, string> = {
  proposed: "Proposed",
  approved: "Approved",
  rejected: "Rejected",
  resolved: "Resolved by the system",
};

export function actionText(action: string): string {
  return ACTION_TEXT[action] ?? action;
}

export function revisionConcept(r: MappingRevision): string {
  if (r.outcome === "no_equivalent") return "no standard concept";
  return r.vocabulary_id && r.concept_code ? `${r.vocabulary_id} ${r.concept_code}` : "no concept";
}

/** Newest first; a stable order when two share a moment. */
export function newestFirst(entries: HistoryEntry[]): HistoryEntry[] {
  return [...entries].sort((a, b) =>
    a.created_at === b.created_at
      ? b.mapping_id - a.mapping_id || b.revision - a.revision
      : a.created_at < b.created_at ? 1 : -1);
}

/** Where an answer stands for review, one bucket each. `attention` is an
 *  approved concept the server can no longer use; `unmapped` means no decision
 *  and no open proposal, not "writes no concept": an answer with a legacy code
 *  may still write that code's concept. */
export type CoverageBucket = "unmapped" | "proposed" | "attention" | "done" | "retired";
export type CoverageFilter = "all" | CoverageBucket;

interface Reviewable {
  retired?: boolean;
  value_mappings?: ValueMapping[];
}

export function coverageBucket(choice: Reviewable): CoverageBucket {
  if (choice.retired) return "retired";
  switch (answerState(choice.value_mappings).kind) {
    case "approved":
    case "approved_no_equivalent":
      return "done";
    case "approved_invalid":
      return "attention";
    case "proposed":
      return "proposed";
    default:
      return "unmapped";
  }
}

export interface AnswerCoverage {
  /** Answers still offered; retired ones are not counted. */
  offered: number;
  mapped: number;
  no_equivalent: number;
  proposed: number;
  attention: number;
  unmapped: number;
  retired: number;
}

/** The field's answer coverage from the list the editor already holds, apart
 *  from the field's own question concept, which is mapped on the field. */
export function answerCoverage(choices: Reviewable[]): AnswerCoverage {
  const out: AnswerCoverage = { offered: 0, mapped: 0, no_equivalent: 0, proposed: 0, attention: 0, unmapped: 0, retired: 0 };
  for (const choice of choices) {
    const bucket = coverageBucket(choice);
    if (bucket === "retired") {
      out.retired += 1;
      continue;
    }
    out.offered += 1;
    if (bucket === "done") {
      if (answerState(choice.value_mappings).kind === "approved") out.mapped += 1;
      else out.no_equivalent += 1;
    } else {
      out[bucket] += 1;
    }
  }
  return out;
}
