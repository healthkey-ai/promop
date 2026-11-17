import { counted } from "@/components/MappingVotes/voteTypes";

/** Confirmed public votes on a mapping's current destination (#2678). */
export interface VoteCounts {
  yes: number;
  no: number;
  comments: number;
}

/** "3 yes · 1 no · 2 comments", or '' when nobody has voted on the row's destination. */
export function tallyText(votes?: VoteCounts | null): string {
  if (!votes) return "";
  return [
    votes.yes ? `${votes.yes} yes` : "",
    votes.no ? `${votes.no} no` : "",
    votes.comments ? counted(votes.comments, "comment") : "",
  ].filter(Boolean).join(" · ");
}
