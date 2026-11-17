import type { VoteProposal } from "./voteTypes";

/**
 * What a proposal says, on both public pages: the metric, where the code
 * comes from, and the concept it is proposed to be stored as. Every value is
 * rendered as text.
 */
export function ProposalSource({ proposal }: { proposal: VoteProposal }) {
  return (
    <div className="min-w-0">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
        {proposal.source}
        {proposal.metric_key && <span className="ml-2 normal-case tracking-normal text-slate-700">{proposal.metric_key}</span>}
      </div>
      <div className="break-all font-mono text-sm text-slate-900">{proposal.source_code}</div>
      {proposal.description && <div className="text-sm text-slate-700">{proposal.description}</div>}
      {proposal.measure_class && <div className="text-xs text-slate-500">{proposal.measure_class}</div>}
    </div>
  );
}

export function ProposalDestination({ proposal }: { proposal: VoteProposal }) {
  return (
    <div className="min-w-0">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Proposed destination</div>
      <div className="break-all font-mono text-sm text-slate-900">
        {proposal.destination_vocabulary}:{proposal.destination_code}
      </div>
      <div className="text-sm text-slate-700">{proposal.destination_name}</div>
      <div className="text-xs text-slate-500">Unit: {proposal.unit || "none set"}</div>
    </div>
  );
}
