import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "../../api/axios";
import type { ResolveRunStatus } from "../../types/sourceCodes";
import PatientSourceCodesTab from "./PatientSourceCodesTab";
import { generateOmopOutcome } from "./generateOmop";

const auth = vi.hoisted(() => ({ currentUser: { email: "admin@example.com", is_staff: true } as Record<string, unknown> }));
const knobs = vi.hoisted(() => ({ watchMs: 60_000 }));

vi.mock("../../hooks/useAuth", () => ({ useAuth: () => auth }));

// The real interval is two seconds and the real limit fifteen minutes; the
// watch itself is what is under test.
vi.mock("./generateOmop", async (original) => ({
  ...(await original<typeof import("./generateOmop")>()),
  GENERATE_POLL_MS: 1,
  get GENERATE_WATCH_MS() { return knobs.watchMs; },
}));

vi.mock("../../api/axios", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const sourceCodes = {
  person_id: 48095,
  summary: { total: 0, unmapped: 0, proposed: 0, approved: 0 },
  source_codes: [],
};

const run = (overrides: Partial<ResolveRunStatus> = {}): ResolveRunStatus => ({
  run_id: "run-1", state: "completed", total: 1, done: 1, resolved: 0, created: 0, provisional: 0,
  reverted: 0, skipped: 0, already_resolved: 0, errors: 0, record_refresh_errors: 0, busy_errors: 0,
  code_details: [],
  ...overrides,
});

const RUN_URL = "/v1/resolve-runs/run-1/";
const sourceCodeReads = () =>
  vi.mocked(api.get).mock.calls.filter(([url]) => String(url).includes("source-codes")).length;
const polls = () => vi.mocked(api.get).mock.calls.filter(([url]) => url === RUN_URL).length;
const pause = (ms: number) => new Promise((resolve) => { setTimeout(resolve, ms); });

/** Answer the run's polls with *polls*, in order; the last one repeats. */
function serve(polls: Array<ResolveRunStatus | Error>) {
  let next = 0;
  vi.mocked(api.get).mockImplementation(((url: string) => {
    if (url === RUN_URL) {
      const answer = polls[Math.min(next, polls.length - 1)];
      next += 1;
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve({ data: answer });
    }
    return Promise.resolve({ data: url.includes("source-codes") ? sourceCodes : {} });
  }) as never);
  vi.mocked(api.post).mockResolvedValue({ data: { run_id: "run-1", total: 1 } } as never);
}

const start = async (onRecordRefreshed = vi.fn()) => {
  render(<PatientSourceCodesTab personId="48095" onRecordRefreshed={onRecordRefreshed} />);
  fireEvent.click(await screen.findByRole("button", { name: "Generate OMOP" }));
  return onRecordRefreshed;
};

beforeEach(() => {
  vi.clearAllMocks();
  auth.currentUser = { email: "admin@example.com", is_staff: true };
  knobs.watchMs = 60_000;
});

describe("Generate OMOP on a patient's Source Codes tab (#2822)", () => {
  it("runs for this patient, then re-reads the list and the page's record", async () => {
    serve([run({ state: "pending", done: 0 }), run({ state: "running", done: 0 }), run({ resolved: 2, created: 1 })]);
    const onRecordRefreshed = await start();

    expect(await screen.findByText(
      "Generate OMOP finished: 2 rows moved to an approved concept, 1 row created. "
      + "The patient record was refreshed.",
    )).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith("/v1/patient-records/48095/resolve-source-codes/", {});
    expect(onRecordRefreshed).toHaveBeenCalledTimes(1);
    // Once on load, once after the run.
    await waitFor(() => expect(sourceCodeReads()).toBe(2));
    expect(screen.getByRole("button", { name: "Generate OMOP" })).toBeEnabled();
  });

  it("says what it is doing while the run is going, and takes no second click", async () => {
    serve([run({ state: "running", done: 0 })]);
    await start();

    const button = await screen.findByRole("button", { name: /Generating OMOP/ });
    expect(button).toBeDisabled();
    expect(screen.getByText(/then refreshing their record\. Other patients are not changed\./)).toBeInTheDocument();
    fireEvent.click(button);
    expect(api.post).toHaveBeenCalledTimes(1);
  });

  it("reports a run that changed nothing as a refreshed record", async () => {
    serve([run()]);
    await start();

    expect(await screen.findByText(
      "Generate OMOP finished: no rows needed changing. The patient record was refreshed.",
    )).toBeInTheDocument();
  });

  it("says so when the rows were generated and the record could not be refreshed", async () => {
    serve([run({ created: 3, errors: 1, record_refresh_errors: 1 })]);
    const onRecordRefreshed = await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Generate OMOP finished: 3 rows created. The patient record could not be refreshed, "
      + "so it does not show these rows yet. Run Generate OMOP again.",
    );
    // The rows are committed, so the list is still re-read.
    await waitFor(() => expect(sourceCodeReads()).toBe(2));
    expect(onRecordRefreshed).toHaveBeenCalledTimes(1);
  });

  it("reports a run that did not finish", async () => {
    serve([run({ state: "failed" })]);
    await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Generate OMOP did not finish. Some rows may have been updated; run it again.",
    );
  });

  it("says a refusal in its own words, never the server's diagnostic, and watches nothing", async () => {
    serve([run()]);
    vi.mocked(api.post).mockRejectedValue({
      response: { status: 403, data: { detail: "Only administrators can perform this action. | identity=7 | grants=[]" } },
    });
    const onRecordRefreshed = await start();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Only staff and organization admins can run Generate OMOP for this patient.");
    expect(alert).not.toHaveTextContent("identity=7");
    expect(polls()).toBe(0);
    expect(onRecordRefreshed).not.toHaveBeenCalled();
    expect(sourceCodeReads()).toBe(1);
    expect(screen.getByRole("button", { name: "Generate OMOP" })).toBeEnabled();
  });

  it("re-reads the list and the record when the request died, since it may have run", async () => {
    serve([run()]);
    vi.mocked(api.post).mockRejectedValue(new Error("Network Error"));
    const onRecordRefreshed = await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Generate OMOP did not answer. It may have run all the same: check the list and the record before running it again.",
    );
    await waitFor(() => expect(sourceCodeReads()).toBe(2));
    expect(onRecordRefreshed).toHaveBeenCalledTimes(1);
    expect(polls()).toBe(0);
  });

  it("says another update holds the patient's rows", async () => {
    serve([run({ errors: 1, busy_errors: 1 })]);
    await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Another update is working on this patient's rows, so this run changed none. "
      + "Run Generate OMOP again once it has finished.",
    );
  });

  it("stops watching a run that never finishes", async () => {
    knobs.watchMs = 30;
    serve([run({ state: "pending", done: 0 })]);
    const onRecordRefreshed = await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Generate OMOP is taking longer than expected. It may still be running; "
      + "reload the page in a few minutes to see the result.",
    );
    expect(screen.getByRole("button", { name: "Generate OMOP" })).toBeEnabled();
    expect(onRecordRefreshed).not.toHaveBeenCalled();
    const asked = polls();
    await pause(20);
    expect(polls()).toBe(asked);
  });

  it("stops asking once the tab is gone", async () => {
    serve([run({ state: "running", done: 0 })]);
    const onRecordRefreshed = vi.fn();
    const { unmount } = render(<PatientSourceCodesTab personId="48095" onRecordRefreshed={onRecordRefreshed} />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate OMOP" }));
    await waitFor(() => expect(polls()).toBeGreaterThan(1));

    unmount();
    await pause(10);
    const asked = polls();
    await pause(20);

    expect(polls()).toBe(asked);
    expect(onRecordRefreshed).not.toHaveBeenCalled();
  });

  it("rides out one failed poll", async () => {
    serve([new Error("network"), run({ resolved: 1 })]);
    await start();

    expect(await screen.findByText(/1 row moved to an approved concept\. The patient record was refreshed\./)).toBeInTheDocument();
  });

  it("stops watching after repeated failed polls and says the run may still be going", async () => {
    serve([new Error("network")]);
    const onRecordRefreshed = await start();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Lost contact with Generate OMOP. It may still be running; reload the page in a few minutes to see the result.",
    );
    expect(polls()).toBe(3);
    expect(onRecordRefreshed).not.toHaveBeenCalled();
  });

  it("is offered to an org admin", async () => {
    serve([run()]);
    auth.currentUser = { email: "admin@example.com", is_staff: false, is_org_admin: true };
    render(<PatientSourceCodesTab personId="48095" />);

    expect(await screen.findByRole("button", { name: "Generate OMOP" })).toBeEnabled();
  });

  it("is not offered to anyone else", async () => {
    serve([run()]);
    auth.currentUser = { email: "analyst@example.com", is_staff: false };
    render(<PatientSourceCodesTab personId="48095" />);

    await screen.findByText("No source codes found.");
    expect(screen.queryByRole("button", { name: "Generate OMOP" })).not.toBeInTheDocument();
  });
});

describe("generateOmopOutcome", () => {
  it("lists only what the run did", () => {
    expect(generateOmopOutcome(run({ resolved: 1, provisional: 4, reverted: 2 }))).toEqual({
      ok: true,
      message: "Generate OMOP finished: 1 row moved to an approved concept, 4 rows provisional from proposed mappings, "
        + "2 rows returned to unresolved. The patient record was refreshed.",
    });
  });

  it("does not claim a refresh for a run that stopped on another error", () => {
    expect(generateOmopOutcome(run({ errors: 1 }))).toEqual({
      ok: false,
      message: "Generate OMOP stopped on an error, so some rows may not have been updated. Run it again.",
    });
  });

  it("does not call a run finished when the resolve and the record both failed", () => {
    expect(generateOmopOutcome(run({ errors: 2, record_refresh_errors: 1 }))).toEqual({
      ok: false,
      message: "Generate OMOP stopped on an error, so some rows may not have been updated "
        + "and the patient record was not refreshed. Run it again.",
    });
  });

  it("reads a run from a server that does not report record errors", () => {
    const { record_refresh_errors: _omitted, busy_errors: _busy, ...older } = run({ created: 1 });
    expect(generateOmopOutcome(older).ok).toBe(true);
  });
});
