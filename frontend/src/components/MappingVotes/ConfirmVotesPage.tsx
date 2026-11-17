import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { publicApi } from "@/api/publicAxios";
import { ProposalDestination, ProposalSource } from "./ProposalSummary";
import { counted, errorStatus, tokenFromHash, type PendingVotes } from "./voteTypes";

type State =
  | { state: "loading" }
  | { state: "invalid" }
  | { state: "error" }
  | { state: "ready"; pending: PendingVotes }
  | { state: "confirmed"; title: string; recorded: number; skipped: number };

const CHOICE_LABEL = { yes: "Yes", no: "No", "": "Comment only" } as const;
const PREVIEW = "/v1/public/mapping-votes/confirm/preview/";
const CONFIRM = "/v1/public/mapping-votes/confirm/";

/**
 * Where the emailed confirmation link lands (`/vote/confirm#<token>`).
 * Opening it only asks for a preview of what was submitted: whatever follows
 * the link records nothing. The votes are recorded by the button, once. The
 * token is the URL fragment and only ever leaves in a request body.
 */
export default function ConfirmVotesPage() {
  const token = tokenFromHash(useLocation().hash);
  const [view, setView] = useState<State>({ state: "loading" });
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!token) {
        if (!cancelled) setView({ state: "invalid" });
        return;
      }
      try {
        // The preview: it shows what was submitted and records nothing.
        const response = await publicApi.post<PendingVotes>(PREVIEW, { token });
        if (!cancelled) setView({ state: "ready", pending: response.data });
      } catch (err) {
        if (!cancelled) setView({ state: errorStatus(err) === 404 ? "invalid" : "error" });
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  const confirm = async (title: string) => {
    setBusy(true);
    setFailed(false);
    try {
      const response = await publicApi.post<{ recorded: number; skipped: number }>(CONFIRM, { token });
      setView({ state: "confirmed", title, ...response.data });
    } catch (err) {
      if (errorStatus(err) === 404) setView({ state: "invalid" });
      else setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-dvh bg-slate-50 px-4 py-8">
      <div className="mx-auto max-w-2xl rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        {view.state === "loading" && <p role="status" className="text-sm text-slate-600">Loading your votes…</p>}
        {view.state === "invalid" && (
          <>
            <h1 className="text-xl font-semibold text-slate-900">This confirmation link is no longer valid</h1>
            <p className="mt-2 text-sm text-slate-600">
              A link works once and expires after 48 hours. If you still want to vote, open the voting
              page again and submit your votes to get a new link.
            </p>
          </>
        )}
        {view.state === "error" && (
          <p role="alert" className="text-sm text-red-700">
            Your votes could not be loaded. Reload the page to try again.
          </p>
        )}
        {view.state === "confirmed" && (
          <>
            <h1 className="text-xl font-semibold text-slate-900">Thank you</h1>
            <p role="status" className="mt-3 rounded-md border border-green-200 bg-green-50 p-4 text-sm text-green-900">
              {view.recorded === 0
                ? "None of your votes could be recorded."
                : `${counted(view.recorded, "vote")} recorded for ${view.title}.`}
            </p>
            {view.skipped > 0 && (
              <p className="mt-3 text-sm text-slate-700" data-testid="skipped-votes">
                {counted(view.skipped, "proposal")} {view.skipped === 1 ? "was" : "were"} skipped: {view.skipped === 1 ? "it was" : "they were"}{" "}
                approved, rejected or changed after you voted, so there was nothing left to vote on.
              </p>
            )}
            <p className="mt-3 text-sm text-slate-600">
              The curators who maintain the mappings read the votes and decide. To change a vote, open
              the voting page again and submit a new one from the same address.
            </p>
          </>
        )}
        {view.state === "ready" && (
          <>
            <h1 className="text-xl font-semibold text-slate-900">Confirm your votes</h1>
            <p className="mt-2 text-sm text-slate-700">
              {view.pending.title}, from <span className="font-medium">{view.pending.email}</span>. Nothing
              is recorded until you confirm.
            </p>
            <ul className="mt-4 space-y-2">
              {view.pending.votes.map((vote, index) => (
                <li key={vote.proposal?.id ?? `closed-${index}`} className="rounded-lg border border-slate-200 p-3">
                  {vote.proposal ? (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <ProposalSource proposal={vote.proposal} />
                      <ProposalDestination proposal={vote.proposal} />
                    </div>
                  ) : (
                    <p className="text-sm text-slate-600">
                      A proposal that has since been approved, rejected or changed. This vote will be skipped.
                    </p>
                  )}
                  <p className="mt-2 text-sm text-slate-900">
                    <span className="font-semibold">Your vote: {CHOICE_LABEL[vote.choice] ?? vote.choice}</span>
                  </p>
                  {vote.comment && <p className="mt-1 whitespace-pre-line break-words text-sm text-slate-700">{vote.comment}</p>}
                </li>
              ))}
            </ul>
            {failed && (
              <p role="alert" className="mt-3 text-sm text-red-700">
                Your votes could not be confirmed. Try again.
              </p>
            )}
            <button
              type="button" disabled={busy} onClick={() => { void confirm(view.pending.title); }}
              className="mt-4 w-full rounded-md bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-700 focus:outline-2 focus:outline-offset-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60 sm:w-auto"
            >
              {busy ? "Confirming…" : "Confirm my votes"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
