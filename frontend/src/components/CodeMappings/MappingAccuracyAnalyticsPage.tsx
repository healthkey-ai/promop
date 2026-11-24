import PageTitle from '@/components/Branding/PageTitle';
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "@/api/axios";

/** One analytics row: curator statuses and the ranker's record on the same mappings (#2383). */
export interface AccuracyRow {
  key: string;
  label: string;
  total: number;
  approved: number;
  proposed: number;
  rejected: number;
  /** Decisions that began as a ranker suggestion: the denominator of the three scores. */
  scored: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}
export interface AccuracySection {
  key: string;
  label: string;
  rows: AccuracyRow[];
}
/** One row of the auto-approved block: what curators have since made of Suggest's own approvals (#2856). */
export interface AutoApprovedRow {
  key: string;
  label: string;
  total: number;
  /** Auto-approvals a curator has since given a first disposition. */
  reviewed: number;
  /** Reviewed and accepted: the auto-approval held. */
  confirmed: number;
  /** Reviewed and overridden or rejected: it did not. */
  reversed: number;
  unreviewed: number;
  /** confirmed / reviewed; null until a curator has reviewed one. A sample, not all of them. */
  precision_of_reviewed: number | null;
}
export interface AutoApprovedBlock {
  key: string;
  label: string;
  rows: AutoApprovedRow[];
}
interface AnalyticsResponse {
  sections: AccuracySection[];
  auto_approved?: AutoApprovedBlock;
  /** Reviewed decisions on a suggestion no ranking model made; in no Scored count (#2743). */
  unscored_fallbacks?: number;
}

const metric = (value: number | null) => value === null ? "—" : `${(value * 100).toFixed(1)}%`;
const AUTO_COLUMNS = ["Confidence", "Total", "Reviewed", "Confirmed", "Reversed", "Not yet reviewed", "Precision of reviewed"];
const COLUMNS = ["Confidence", "Total", "Approved", "Proposed", "Rejected", "Scored", "Precision", "Recall", "F1"];

export default function MappingAccuracyAnalyticsPage() {
  const [sections, setSections] = useState<AccuracySection[]>([]);
  const [auto, setAuto] = useState<AutoApprovedBlock | null>(null);
  const [unscoredFallbacks, setUnscoredFallbacks] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    void api.get<AnalyticsResponse>("/v1/code-mappings/accuracy/analytics/")
      .then(({ data }) => { setSections(data.sections || []); setAuto(data.auto_approved ?? null); setUnscoredFallbacks(data.unscored_fallbacks ?? 0); })
      .catch(() => setError("Failed to load mapping accuracy analytics."));
  }, []);
  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-6xl">
    <Link to="/code-mappings" className="text-sm text-slate-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Mapping accuracy analytics</PageTitle>
    <p className="mt-1 text-sm text-slate-600">
      Every mapping a curator has saved, by the page it was saved from and by the confidence the ranker had in its
      destination at the time. Approved, Proposed and Rejected are the mappings' current status. Precision, recall and
      F1 are computed over the Scored decisions only, those that began as a ranker suggestion: a kept suggestion is a
      hit, an overridden or rejected one a miss, and — means nothing to score yet. A decision no ranker scored counts
      in the Overall row only.
    </p>
    {unscoredFallbacks > 0 && <p className="mt-2 text-sm text-slate-600">
      {unscoredFallbacks.toLocaleString()} reviewed {unscoredFallbacks === 1 ? "decision was" : "decisions were"} on
      a fallback destination, written while the ranking model was unavailable. {unscoredFallbacks === 1 ? "It counts" : "They count"} in
      the status columns and {unscoredFallbacks === 1 ? "is" : "are"} not Scored: the model never proposed {unscoredFallbacks === 1 ? "it" : "them"}.
    </p>}
    {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
    {sections.map(section => <section key={section.key} aria-labelledby={`accuracy-${section.key}`} className="mt-6">
      <h2 id={`accuracy-${section.key}`} className="text-lg font-semibold text-slate-950">{section.label}</h2>
      <div className="mt-2 overflow-hidden rounded-md border border-slate-200 bg-white"><table className="w-full text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>{COLUMNS.map(h => <th key={h} scope="col" className="px-3 py-3">{h}</th>)}</tr></thead>
        <tbody>
          {section.rows.map(row => <tr key={row.key} className={`border-t border-slate-200 ${row.key === "overall" ? "bg-slate-50 font-semibold" : ""}`}>
            <th scope="row" className="px-3 py-3 font-medium">{row.label}</th>
            <td className="px-3 py-3 tabular-nums">{row.total}</td>
            <td className="px-3 py-3 tabular-nums">{row.approved}</td>
            <td className="px-3 py-3 tabular-nums">{row.proposed}</td>
            <td className="px-3 py-3 tabular-nums">{row.rejected}</td>
            <td className="px-3 py-3 tabular-nums">{row.scored}</td>
            <td className="px-3 py-3 tabular-nums">{metric(row.precision)}</td>
            <td className="px-3 py-3 tabular-nums">{metric(row.recall)}</td>
            <td className="px-3 py-3 tabular-nums">{metric(row.f1)}</td>
          </tr>)}
        </tbody>
      </table></div>
    </section>)}
    {auto && auto.rows.length > 0 && <section aria-labelledby="accuracy-auto-approved" className="mt-6">
      <h2 id="accuracy-auto-approved" className="text-lg font-semibold text-slate-950">{auto.label}</h2>
      <p className="mt-1 text-sm text-slate-600">
        Mappings Suggest approved itself, by the confidence it had when it approved them. These are not curator
        decisions, so they are in no table above until a curator reviews one. Confirmed is a first review that accepted
        the destination, Reversed one that moved it to another concept, rejected it or removed it, whether or not the
        curator un-approved it first. An approved mapping nobody has changed is Not yet reviewed, even if it was
        re-saved. Precision of reviewed covers only the auto-approvals a curator has looked at, so it is a sample, not
        all of them.
      </p>
      <div className="mt-2 overflow-hidden rounded-md border border-slate-200 bg-white"><table className="w-full text-left text-sm">
        <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>{AUTO_COLUMNS.map(h => <th key={h} scope="col" className="px-3 py-3">{h}</th>)}</tr></thead>
        <tbody>
          {auto.rows.map(row => <tr key={row.key} className={`border-t border-slate-200 ${row.key === "overall" ? "bg-slate-50 font-semibold" : ""}`}>
            <th scope="row" className="px-3 py-3 font-medium">{row.label}</th>
            <td className="px-3 py-3 tabular-nums">{row.total.toLocaleString()}</td>
            <td className="px-3 py-3 tabular-nums">{row.reviewed.toLocaleString()}</td>
            <td className="px-3 py-3 tabular-nums">{row.confirmed.toLocaleString()}</td>
            <td className="px-3 py-3 tabular-nums">{row.reversed.toLocaleString()}</td>
            <td className="px-3 py-3 tabular-nums">{row.unreviewed.toLocaleString()}</td>
            <td className="px-3 py-3 tabular-nums">{metric(row.precision_of_reviewed)}</td>
          </tr>)}
        </tbody>
      </table></div>
    </section>}
  </main></div>;
}
