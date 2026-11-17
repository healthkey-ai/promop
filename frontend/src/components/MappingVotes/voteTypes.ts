/** One wearable mapping proposal as the public pages show it: reference data only. */
export interface VoteProposal {
  id: number;
  metric_key: string;
  measure_class: string;
  /** What a reader calls the source: HealthKit, Health Connect, ... */
  source: string;
  source_code: string;
  description: string;
  destination_vocabulary: string;
  destination_code: string;
  destination_name: string;
  /** The unit values would be stored in; '' when none has been set. */
  unit: string;
}

export interface VotePage {
  title: string;
  note: string;
  expires_at: string | null;
  comment_max_length: number;
  groups: { category: string; proposals: VoteProposal[] }[];
}

export type VoteChoice = "yes" | "no" | "";

export interface PendingVotes {
  title: string;
  email: string;
  expires_at: string;
  votes: {
    /** Null when the proposal closed or changed after the votes were sent. */
    proposal: VoteProposal | null;
    choice: VoteChoice;
    comment: string;
  }[];
}

export const errorStatus = (error: unknown): number | undefined =>
  (error as { response?: { status?: number } })?.response?.status;

export const errorDetail = (error: unknown): string | undefined => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : undefined;
};

/** "1 vote", "2 votes". */
export const counted = (n: number, noun: string): string => `${n} ${noun}${n === 1 ? "" : "s"}`;

/**
 * The token of a public voting link. It is the URL's fragment
 * (`/vote#<token>`), which a browser never sends to a server: it is in no
 * request line, access log or Referer. The pages read it here and send it
 * in a request body.
 */
export const tokenFromHash = (hash: string): string => hash.replace(/^#/, "").trim();
