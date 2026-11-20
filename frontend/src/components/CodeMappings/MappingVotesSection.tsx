import { useEffect, useState } from "react";
import api from "@/api/axios";
import SectionToggle from "./SectionToggle";
import { tallyText, type VoteCounts } from "./voteCounts";

interface MappingVote {
  id: number;
  choice: "yes" | "no" | "";
  comment: string;
  confirmed_at: string;
  share_id: number;
  share_title: string;
  /** Cast on a destination the mapping has since moved off; not in the tally. */
  stale: boolean;
  /** Present only for a reader who may write mappings. */
  voter_email?: string;
}

interface MappingVotesPayload {
  tally: VoteCounts;
  emails_included: boolean;
  votes: MappingVote[];
}

const CHOICE_LABEL = { yes: "Yes", no: "No", "": "Comment" } as const;

/**
 * The confirmed public votes on one mapping (#2678), in the Edit Mapping
 * dialog: closed until asked for, with the tally on its heading. Read-only:
 * a vote is advice and never changes the mapping. The server leaves the
 * voters' addresses out for a reader who cannot write mappings.
 */
export default function MappingVotesSection({ mappingId }: { mappingId: number }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MappingVotesPayload | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await api.get<MappingVotesPayload>(`/v1/code-mappings/${mappingId}/votes/`);
        if (!cancelled) { setData(response.data); setError(false); }
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => { cancelled = true; };
  }, [mappingId]);

  const votes = data?.votes ?? [];
  // Nothing to show and nothing wrong: the dialog of an ordinary mapping
  // carries no empty section.
  if (!error && votes.length === 0) return null;
  const stale = votes.filter((vote) => vote.stale).length;
  const marker = [tallyText(data?.tally), stale ? `${stale} on an earlier destination` : ""]
    .filter(Boolean).join(" · ");

  return (
    <section aria-label="Votes" className="mb-3 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs">
      <SectionToggle title="Votes" open={open} onToggle={() => setOpen((value) => !value)} marker={marker} />
      {error && <p role="alert" className="mt-1 text-rose-700">The votes could not be loaded.</p>}
      {open && !error && (
        <>
          <p className="mt-0.5 text-slate-500">
            Confirmed votes from the public voting page. They are advice: a vote never changes this
            mapping, and approving or rejecting it stays with you.
          </p>
          <ul className="mt-2 divide-y divide-slate-200">
            {votes.map((vote) => (
              <li key={vote.id} data-testid="mapping-vote" className={`py-1.5 ${vote.stale ? "text-slate-500" : "text-slate-800"}`}>
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-semibold">{CHOICE_LABEL[vote.choice] ?? vote.choice}</span>
                  {vote.voter_email && <span className="break-all">{vote.voter_email}</span>}
                  <span className="text-slate-500">{new Date(vote.confirmed_at).toLocaleDateString()}</span>
                  <span className="text-slate-500">{vote.share_title}</span>
                  {vote.stale && (
                    <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-800">
                      on an earlier destination
                    </span>
                  )}
                </div>
                {vote.comment && <p className="mt-0.5 whitespace-pre-line break-words">{vote.comment}</p>}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
