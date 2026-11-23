import type { ResolveRunStatus } from "../../types/sourceCodes";

/** How often the tab asks where its Generate OMOP run has got to. */
export const GENERATE_POLL_MS = 2000;

/** Polls in a row that may fail before the tab stops watching a run. */
export const GENERATE_POLL_RETRIES = 3;

/**
 * How long the tab watches one run. A run whose worker was lost, or that no
 * worker ever took, stays unfinished on the server; the tab must not spin
 * for ever on it. It is the worker's task time limit: a run still unfinished
 * after it is not being worked on.
 */
export const GENERATE_WATCH_MS = 15 * 60 * 1000;

/** The run was started (or may have been) and the tab no longer knows how it ended. */
export const GENERATE_LOST =
  "Lost contact with Generate OMOP. It may still be running; reload the page in a few minutes to see the result.";

export const GENERATE_SLOW =
  "Generate OMOP is taking longer than expected. It may still be running; reload the page in a few minutes to see the result.";

/**
 * Why a run could not be started, from the HTTP status of the refusal. The
 * server's own text is not shown: its 403 describes the caller's grants.
 * No status, or a 5xx, is a request that died, which with no worker is after
 * the rows were written; the tab re-reads the list and the record for those.
 */
export function generateStartFailure(status: number | undefined): { message: string; mayHaveRun: boolean } {
  if (status === 403) {
    return { message: "Only staff and organization admins can run Generate OMOP for this patient.", mayHaveRun: false };
  }
  if (status !== undefined && status < 500) {
    return { message: "Generate OMOP could not be started.", mayHaveRun: false };
  }
  return {
    message: "Generate OMOP did not answer. It may have run all the same: check the list and the record before running it again.",
    mayHaveRun: true,
  };
}

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
  const recordErrors = run.record_refresh_errors ?? 0;
  if ((run.busy_errors ?? 0) > 0) {
    return {
      ok: false,
      message: "Another update is working on this patient's rows, so this run changed none. "
        + "Run Generate OMOP again once it has finished.",
    };
  }
  // An error that is not the record's is the resolve's: the counts are then
  // of a run that stopped, so they are not reported as what it "finished".
  if (run.errors > recordErrors) {
    return {
      ok: false,
      message: "Generate OMOP stopped on an error, so some rows may not have been updated"
        + (recordErrors > 0 ? " and the patient record was not refreshed" : "")
        + ". Run it again.",
    };
  }
  if (recordErrors > 0) {
    return {
      ok: false,
      message: `${rowSummary(run)} The patient record could not be refreshed, so it does not show these rows yet. Run Generate OMOP again.`,
    };
  }
  return { ok: true, message: `${rowSummary(run)} The patient record was refreshed.` };
}
