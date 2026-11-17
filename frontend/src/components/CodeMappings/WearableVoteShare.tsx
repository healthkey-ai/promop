import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Share2 } from "lucide-react";
import api from "@/api/axios";
import { blobErrorMessage, filenameFromDisposition, saveBlob } from "@/utils/download";

interface VoteShare {
  id: number;
  title: string;
  note: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  active: boolean;
  /** Addresses that confirmed at least one vote. */
  voters: number;
  votes: number;
  /** The public link; the server sends it only to those who may write mappings. */
  url?: string;
}

const SHARES = "/v1/code-mappings/vote-shares/";
const detailOf = (error: unknown): string | undefined => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : undefined;
};
const day = (iso: string) => new Date(iso).toLocaleDateString();

function status(share: VoteShare): string {
  if (share.revoked_at) return "Revoked";
  return share.active ? "Active" : "Expired";
}

/**
 * Share on the Wearables tab (#2678): makes a public link to a page where
 * people without an account vote Yes or No, or comment, on the wearable
 * mapping proposals. Shown only to those who may write mappings; the server
 * refuses everyone else.
 *
 * A vote is advice. It is recorded only after the voter confirms by email,
 * and it never changes a mapping.
 */
export default function WearableVoteShare() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button" onClick={() => setOpen(true)}
        title="Share the wearable mapping proposals for a public vote"
        className="ml-2 inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700"
      >
        <Share2 size={12} aria-hidden="true" /> Share
      </button>
      {open && <VoteShareDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function VoteShareDialog({ onClose }: { onClose: () => void }) {
  const [shares, setShares] = useState<VoteShare[] | null>(null);
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<VoteShare | null>(null);
  const [copied, setCopied] = useState<number | null>(null);
  const [working, setWorking] = useState<number | null>(null);
  const titleInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.get<{ results: VoteShare[] }>(SHARES);
      setShares(response.data.results);
    } catch (err) {
      setShares([]);
      setError(detailOf(err) ?? "The shares could not be loaded.");
    }
  }, []);

  useEffect(() => {
    (async () => {
      await load();
    })();
  }, [load]);

  useEffect(() => {
    titleInput.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) {
      setError("Give the share a title: voters see it at the top of the page.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await api.post<VoteShare>(SHARES, {
        title: title.trim(), note: note.trim(), ...(expires ? { expires_at: expires } : {}),
      });
      setCreated(response.data);
      setTitle(""); setNote(""); setExpires("");
      await load();
    } catch (err) {
      setError(detailOf(err) ?? "The share could not be created. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const copy = async (share: VoteShare) => {
    if (!share.url) return;
    try {
      await navigator.clipboard.writeText(share.url);
      setCopied(share.id);
    } catch {
      setError("Could not copy the link. Select it and copy it by hand.");
    }
  };

  const revoke = async (share: VoteShare) => {
    if (!window.confirm(`Revoke "${share.title}"? The link stops working at once. Confirmed votes are kept.`)) return;
    setWorking(share.id);
    setError(null);
    try {
      await api.post(`${SHARES}${share.id}/revoke/`);
      if (created?.id === share.id) setCreated(null);
      await load();
    } catch (err) {
      setError(detailOf(err) ?? "The share could not be revoked. Try again.");
    } finally {
      setWorking(null);
    }
  };

  const downloadVotes = async (share: VoteShare) => {
    setWorking(share.id);
    setError(null);
    try {
      const response = await api.get<Blob>(`${SHARES}${share.id}/votes-export/`, { responseType: "blob" });
      saveBlob(response.data, filenameFromDisposition(
        response.headers?.["content-disposition"], `mapping-votes-share-${share.id}.csv`));
    } catch (err) {
      setError((await blobErrorMessage(err)) ?? "Could not download the votes. Try again.");
    } finally {
      setWorking(null);
    }
  };

  const linkButton = "rounded border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-100 focus:outline-2 focus:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-60";
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/50 p-4">
      <div role="dialog" aria-modal="true" aria-labelledby="vote-share-title"
        className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-lg bg-white p-5 text-sm text-slate-800 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <h2 id="vote-share-title" className="text-base font-semibold text-slate-900">
            Share the wearable proposals for a vote
          </h2>
          <button type="button" onClick={onClose} aria-label="Close" className={linkButton}>Close</button>
        </div>
        <p className="mt-1 text-xs text-slate-600">
          Anyone with the link sees the proposed wearable mappings, without an account, and can vote Yes
          or No or comment on each, giving an email address. A vote is recorded only after the voter
          confirms it from an email. Votes are advice: they never change a mapping, and approval stays
          with you.
        </p>

        <form onSubmit={create} noValidate className="mt-4 space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3">
          <div>
            <label htmlFor="vote-share-name" className="text-xs font-medium text-slate-700">Title (shown to voters)</label>
            <input id="vote-share-name" ref={titleInput} value={title} maxLength={200}
              placeholder="HL7 PHR working group review"
              onChange={(event) => setTitle(event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div>
            <label htmlFor="vote-share-note" className="text-xs font-medium text-slate-700">Note to voters (optional)</label>
            <textarea id="vote-share-note" rows={2} value={note} maxLength={2000}
              onChange={(event) => setNote(event.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label htmlFor="vote-share-expires" className="text-xs font-medium text-slate-700">Expires (optional)</label>
              <input id="vote-share-expires" type="date" value={expires}
                onChange={(event) => setExpires(event.target.value)}
                className="mt-1 block rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            </div>
            <button type="submit" disabled={busy}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60">
              {busy ? "Creating…" : "Create link"}
            </button>
          </div>
        </form>

        {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}

        {created?.url && (
          <div role="status" className="mt-3 rounded-md border border-green-200 bg-green-50 p-3">
            <label htmlFor="vote-share-link" className="text-xs font-medium text-green-900">
              Public link for {created.title}
            </label>
            <div className="mt-1 flex gap-2">
              <input id="vote-share-link" readOnly value={created.url}
                onFocus={(event) => event.target.select()}
                className="min-w-0 flex-1 rounded-md border border-green-300 bg-white px-2 py-1.5 font-mono text-xs" />
              <button type="button" onClick={() => { void copy(created); }} className={linkButton}
                aria-label={`Copy the link for ${created.title}`}>
                {copied === created.id ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        )}

        <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-700">Shares</h3>
        {shares === null && <p role="status" className="mt-2 text-xs text-slate-500">Loading…</p>}
        {shares?.length === 0 && <p className="mt-2 text-xs text-slate-500">No link has been created yet.</p>}
        {shares && shares.length > 0 && (
          <ul className="mt-2 divide-y divide-slate-200 rounded-md border border-slate-200">
            {shares.map((share) => (
              <li key={share.id} data-testid="vote-share" className="flex flex-wrap items-center gap-x-3 gap-y-1 p-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium text-slate-900">{share.title}</div>
                  <div className="text-xs text-slate-500">
                    Created {day(share.created_at)}
                    {share.expires_at ? ` · expires ${day(share.expires_at)}` : " · no expiry"}
                    {" · "}{share.voters} confirmed {share.voters === 1 ? "voter" : "voters"}
                    {" · "}<span className={share.active ? "text-green-700" : "text-slate-600"}>{status(share)}</span>
                  </div>
                </div>
                {share.active && share.url && (
                  <button type="button" onClick={() => { void copy(share); }} className={linkButton}
                    aria-label={`Copy the link for ${share.title}`}>
                    {copied === share.id ? "Copied" : "Copy link"}
                  </button>
                )}
                <button type="button" disabled={working === share.id || share.votes === 0}
                  onClick={() => { void downloadVotes(share); }} className={linkButton}
                  title={share.votes === 0 ? "No confirmed votes yet" : "One line per vote, with the voters' email addresses"}
                  aria-label={`Download votes (CSV) for ${share.title}`}>
                  Download votes (CSV)
                </button>
                {share.active && (
                  <button type="button" disabled={working === share.id} onClick={() => { void revoke(share); }}
                    className={`${linkButton} text-red-700`} aria-label={`Revoke ${share.title}`}>
                    Revoke
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
