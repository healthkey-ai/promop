import { tallyText, type VoteCounts } from "./voteCounts";

/**
 * The confirmed public votes on a row's current destination (#2678), on the
 * mapping list. Nothing at all for a row nobody voted on, so the other tabs
 * look as they did. A vote is advice: it never changes the mapping.
 */
export default function VoteTally({ votes }: { votes?: VoteCounts | null }) {
  const text = tallyText(votes);
  if (!text) return null;
  return (
    <div data-testid="vote-tally" title="Confirmed public votes on this destination. Votes do not change a mapping."
      className="mt-1 inline-block rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] font-medium text-indigo-800">
      <span className="sr-only">Votes: </span>{text}
    </div>
  );
}
