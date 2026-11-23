import type { ResolveRunStatus } from "../../types/sourceCodes";

/** How often the tab asks where its Generate OMOP run has got to. */
export const GENERATE_POLL_MS = 2000;

/** Polls in a row that may fail before the tab stops watching a run. */
export const GENERATE_POLL_RETRIES = 3;

export type GenerateOmopOutcome = {
  /** False when the run failed or left something undone; shown as an alert. */
  ok: boolean;
  message: string;
};

const rows = (n: number) => `${n} row${n === 1 ? "" : "s"}`;

/** What a finished run did to this patient's rows, as one sentence. */
function rowSummary(run: ResolveRunStatus): string {
  const parts = [
    run.resolved > 0 && `${rows(run.resolved)} moved to an approved concept`,
    run.created > 0 && `${rows(run.created)} created`,
    run.provisional > 0 && `${rows(run.provisional)} provisional from proposed mappings`,
    run.reverted > 0 && `${rows(run.reverted)} returned to unresolved`,
  ].filter(Boolean);
  return parts.length
    ? `Generate OMOP finished: ${parts.join(", ")}.`
    : "Generate OMOP finished: no rows needed changing.";
}

/**
 * What to tell the curator when one patient's Generate OMOP run ends (#2822).
 * The run resolves the patient's rows and then derives their record, so it
 * can end with the rows done and the record not.
 */
export function generateOmopOutcome(run: ResolveRunStatus): GenerateOmopOutcome {
  if (run.state === "failed") {
    return {
      ok: false,
      message: "Generate OMOP did not finish. Some rows may have been updated; run it again.",
    };
  }
  if ((run.record_refresh_errors ?? 0) > 0) {
    return {
      ok: false,
      message: `${rowSummary(run)} The patient record could not be refreshed, so it does not show these rows yet. Run Generate OMOP again.`,
    };
  }
  if (run.errors > 0) {
    return {
      ok: false,
      message: "Generate OMOP stopped on an error, so some rows may not have been updated. Run it again.",
    };
  }
  return { ok: true, message: `${rowSummary(run)} The patient record was refreshed.` };
}
