import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { publicApi } from "@/api/publicAxios";
import { ProposalDestination, ProposalSource } from "./ProposalSummary";
import {
  counted, errorDetail, errorStatus, tokenFromHash,
  type VoteChoice, type VotePage, type VoteProposal,
} from "./voteTypes";

type Draft = { choice: VoteChoice; comment: string; commenting: boolean };
type Load =
  | { state: "loading" }
  | { state: "unavailable" }
  | { state: "error" }
  | { state: "ready"; page: VotePage };

const EMPTY: Draft = { choice: "", comment: "", commenting: false };
const UNCATEGORIZED = "Uncategorized";

/**
 * The public voting page behind a shared link: no account, no app chrome.
 *
 * A visitor marks Yes or No, or writes a comment, on any of the proposals,
 * gives an email address and submits. Nothing is recorded by that: the
 * server emails a confirmation link, and this page says so in the same words
 * whoever the address belongs to. It calls only the public endpoints, and
 * the link's token (the URL fragment) only ever leaves in a request body.
 */
export default function PublicVotePage() {
  const token = tokenFromHash(useLocation().hash);
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [drafts, setDrafts] = useState<Record<number, Draft>>({});
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!token) {
        if (!cancelled) setLoad({ state: "unavailable" });
        return;
      }
      try {
        // A read, sent as a POST: the token goes in the body, never the URL.
        const response = await publicApi.post<VotePage>("/v1/public/mapping-votes/", { token });
        if (!cancelled) setLoad({ state: "ready", page: response.data });
      } catch (err) {
        if (!cancelled) setLoad({ state: errorStatus(err) === 404 ? "unavailable" : "error" });
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  const acted = useMemo(
    () => Object.entries(drafts)
      .map(([id, draft]) => ({ mapping: Number(id), choice: draft.choice, comment: draft.comment.trim() }))
      .filter((vote) => vote.choice || vote.comment),
    [drafts],
  );
  const voteCount = acted.filter((vote) => vote.choice).length;
  const commentCount = acted.filter((vote) => vote.comment).length;

  const update = (id: number, change: Partial<Draft>) => {
    setError(null);
    setDrafts((current) => ({ ...current, [id]: { ...(current[id] ?? EMPTY), ...change } }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!acted.length) {
      setError("Choose Yes or No, or leave a comment, on at least one proposal.");
      return;
    }
    if (!email.trim()) {
      setError("Enter your email address.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await publicApi.post<{ detail: string }>(
        "/v1/public/mapping-votes/submit/", { token, email: email.trim(), votes: acted },
      );
      setSent(response.data.detail);
    } catch (err) {
      const status = errorStatus(err);
      if (status === 404) setLoad({ state: "unavailable" });
      else if (status === 429) setError("Too many attempts from this network. Wait a while and try again.");
      else if (status === 503) setError(errorDetail(err) ?? "Voting is temporarily unavailable. Please try again later.");
      else setError(errorDetail(err) ?? "Your votes could not be sent. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (load.state === "loading") {
    return <Shell><p role="status" className="text-sm text-slate-600">Loading the proposals…</p></Shell>;
  }
  if (load.state === "unavailable") {
    return (
      <Shell>
        <h1 className="text-xl font-semibold text-slate-900">This link is not available</h1>
        <p className="mt-2 text-sm text-slate-600">
          It may have expired or been withdrawn. Ask whoever sent it to you for a new one.
        </p>
      </Shell>
    );
  }
  if (load.state === "error") {
    return (
      <Shell>
        <p role="alert" className="text-sm text-red-700">
          The proposals could not be loaded. Reload the page to try again.
        </p>
      </Shell>
    );
  }
  const { page } = load;
  if (sent) {
    return (
      <Shell>
        <h1 className="text-xl font-semibold text-slate-900">{page.title}</h1>
        <p role="status" className="mt-4 rounded-md border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          {sent}
        </p>
        <p className="mt-3 text-sm text-slate-600">
          The email holds a link that shows your votes and a button to confirm them. It works once and
          expires in 48 hours. You can close this page.
        </p>
      </Shell>
    );
  }

  const total = page.groups.reduce((sum, group) => sum + group.proposals.length, 0);
  return (
    <form onSubmit={submit} noValidate className="min-h-dvh bg-slate-50 pb-64 sm:pb-40">
      <div className="mx-auto max-w-4xl px-4 py-6">
        <h1 className="text-2xl font-semibold text-slate-900">{page.title}</h1>
        {page.note && <p className="mt-2 whitespace-pre-line text-sm text-slate-700">{page.note}</p>}
        <p className="mt-3 text-sm text-slate-600">
          Each proposal below maps a code from a wearable or phone platform to the standard concept its
          values would be stored as. Choose Yes or No on any of them, or leave a comment; you do not have
          to answer them all. When you submit, you get an email with a link to confirm your votes. They
          are not recorded until you confirm, and a vote does not change a mapping: the curators who
          maintain them read the votes and decide.
        </p>
        {total === 0 && (
          <p role="status" className="mt-6 rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-600">
            There are no proposals open for voting right now.
          </p>
        )}
        {page.groups.map((group) => (
          <section key={group.category} aria-label={group.category || UNCATEGORIZED} className="mt-6">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-700">
              {group.category || UNCATEGORIZED}
              <span className="ml-1 font-normal text-slate-500">({group.proposals.length})</span>
            </h2>
            <ul className="mt-2 space-y-2">
              {group.proposals.map((proposal) => (
                <ProposalCard
                  key={proposal.id} proposal={proposal} draft={drafts[proposal.id] ?? EMPTY}
                  maxLength={page.comment_max_length} onChange={(change) => update(proposal.id, change)}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>

      <div className="fixed inset-x-0 bottom-0 border-t border-slate-300 bg-white shadow-[0_-2px_8px_rgba(15,23,42,0.08)]">
        <div className="mx-auto flex max-w-4xl flex-col gap-2 px-4 py-3 sm:flex-row sm:items-end">
          <p aria-live="polite" data-testid="vote-counts" className="text-sm font-medium text-slate-800 sm:mr-auto sm:pb-2">
            {counted(voteCount, "vote")}, {counted(commentCount, "comment")}
          </p>
          <div className="flex flex-col sm:w-72">
            <label htmlFor="voter-email" className="text-xs font-medium text-slate-700">Your email address</label>
            <input
              id="voter-email" type="email" required autoComplete="email" inputMode="email" maxLength={254}
              value={email} onChange={(event) => { setEmail(event.target.value); setError(null); }}
              aria-describedby="voter-email-help"
              className="mt-1 rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:outline-2 focus:outline-slate-700"
            />
            {/* Said to everyone, in view: what is kept, and who reads it. */}
            <span id="voter-email-help" className="mt-1 text-xs text-slate-500">
              Your address and comments are stored and shown to this organisation's mapping
              administrators. Votes are advisory.
            </span>
          </div>
          <button
            type="submit" disabled={busy}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700 focus:outline-2 focus:outline-offset-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? "Sending…" : "Submit votes"}
          </button>
        </div>
        {error && <p role="alert" className="mx-auto max-w-4xl px-4 pb-3 text-sm text-red-700">{error}</p>}
      </div>
    </form>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh items-start justify-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-xl rounded-lg border border-slate-200 bg-white p-6 shadow-sm">{children}</div>
    </div>
  );
}

function ProposalCard({ proposal, draft, maxLength, onChange }: {
  proposal: VoteProposal;
  draft: Draft;
  maxLength: number;
  onChange: (change: Partial<Draft>) => void;
}) {
  const name = `${proposal.source} ${proposal.source_code}`;
  const commentId = `comment-${proposal.id}`;
  const showComment = draft.commenting || draft.comment !== "";
  const choose = (choice: "yes" | "no") => onChange({ choice: draft.choice === choice ? "" : choice });
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <ProposalSource proposal={proposal} />
        <ProposalDestination proposal={proposal} />
      </div>
      <div role="group" aria-label={`Your vote on ${name}`} className="mt-3 flex flex-wrap items-center gap-2">
        <ChoiceButton label="Yes" pressed={draft.choice === "yes"} name={name} tone="yes" onClick={() => choose("yes")} />
        <ChoiceButton label="No" pressed={draft.choice === "no"} name={name} tone="no" onClick={() => choose("no")} />
        <button
          type="button" aria-expanded={showComment} aria-controls={commentId}
          aria-label={`Comment on ${name}`}
          onClick={() => onChange({ commenting: !showComment, ...(showComment ? { comment: "" } : {}) })}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700"
        >
          {showComment ? "Remove comment" : "Comment"}
        </button>
      </div>
      {showComment && (
        <div id={commentId} className="mt-2">
          <label htmlFor={`${commentId}-text`} className="text-xs font-medium text-slate-700">
            Comment on {name}
          </label>
          <textarea
            id={`${commentId}-text`} rows={3} maxLength={maxLength} value={draft.comment}
            onChange={(event) => onChange({ comment: event.target.value })}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:outline-2 focus:outline-slate-700"
          />
          <div className="text-right text-xs text-slate-500">{draft.comment.length} / {maxLength}</div>
        </div>
      )}
    </li>
  );
}

function ChoiceButton({ label, pressed, name, tone, onClick }: {
  label: string;
  pressed: boolean;
  name: string;
  tone: "yes" | "no";
  onClick: () => void;
}) {
  const on = tone === "yes" ? "border-green-700 bg-green-700 text-white" : "border-red-700 bg-red-700 text-white";
  return (
    <button
      type="button" aria-pressed={pressed} aria-label={`${label} to ${name}`} onClick={onClick}
      className={`min-w-16 rounded-md border px-3 py-1.5 text-sm font-semibold focus:outline-2 focus:outline-offset-2 focus:outline-slate-700 ${
        pressed ? on : "border-slate-300 bg-white text-slate-800 hover:bg-slate-100"
      }`}
    >
      {label}
    </button>
  );
}
