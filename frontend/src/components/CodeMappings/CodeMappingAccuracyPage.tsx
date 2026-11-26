import PageTitle from '@/components/Branding/PageTitle';
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "@/api/axios";

/** Suggest's own approvals and what curators made of the ones they reviewed (#2856). */
interface AutoApproved {
  total: number;
  reviewed: number;
  accepted: number;
  overridden: number;
  rejected: number;
  unreviewed: number;
  precision_of_reviewed: number | null;
}
interface ModelAccuracy {
  model_version: string;
  suggestions: number;
  reviewed: number;
  approved: number;
  rejected: number;
  overridden: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  auto_approved?: AutoApproved;
}
const metric = (value: number | null) => value === null ? "—" : `${(value * 100).toFixed(1)}%`;

interface DashboardResponse {
  models: ModelAccuracy[];
  overall: Omit<ModelAccuracy, 'model_version'> | null;
  /** Suggestions written while no ranking model could be reached; in no row below (#2743). */
  fallbacks?: { suggestions: number; reviewed: number };
}

export default function CodeMappingAccuracyPage() {
  const [models, setModels] = useState<ModelAccuracy[]>([]);
  const [overall, setOverall] = useState<Omit<ModelAccuracy, 'model_version'> | null>(null);
  const [fallbacks, setFallbacks] = useState({ suggestions: 0, reviewed: 0 });
  const [error, setError] = useState("");
  useEffect(() => { void api.get<DashboardResponse>("/v1/code-mappings/accuracy/dashboard/")
    .then(({ data }) => {
      setModels(data.models || []); setOverall(data.overall ?? null);
      setFallbacks(data.fallbacks ?? { suggestions: 0, reviewed: 0 });
    }).catch(() => setError("Failed to load suggestion accuracy.")); }, []);
  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-6xl">
    <Link to="/code-mappings" className="text-sm text-slate-700 underline">← Code Mapping</Link>
    <PageTitle className="mt-4 text-2xl font-semibold text-slate-950">Suggestion model accuracy</PageTitle>
    <p className="mt-1 text-sm text-slate-600">Latest model first. Approved means the curator retained the suggested destination. Metrics use reviews of each model; — means no reviews yet. Reviewed models with no accepted suggestions score 0%.</p>
    {fallbacks.suggestions > 0 && <p className="mt-2 text-sm text-slate-600">
      Not counted here: {fallbacks.suggestions.toLocaleString()} fallback {fallbacks.suggestions === 1 ? "destination" : "destinations"} written
      while the ranking model was unavailable ({fallbacks.reviewed.toLocaleString()} reviewed). The model never proposed them.
    </p>}
    {overall?.auto_approved && overall.auto_approved.total > 0 && <p className="mt-2 text-sm text-slate-600">
      Auto-approved by Suggest: {overall.auto_approved.total.toLocaleString()}, of which a curator has since reviewed {overall.auto_approved.reviewed.toLocaleString()} ({overall.auto_approved.accepted.toLocaleString()} accepted,
      {" "}{overall.auto_approved.overridden.toLocaleString()} given another destination, {overall.auto_approved.rejected.toLocaleString()} rejected; precision of those reviewed {metric(overall.auto_approved.precision_of_reviewed)}).
      They are counted in the rows below like any suggestion. The precision here covers only the ones reviewed, so it is a sample.
    </p>}
    {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
    <div className="mt-5 overflow-hidden rounded-md border border-slate-200 bg-white"><table className="w-full text-left text-sm">
      <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>{["Model", "Suggestions", "Approved", "Rejected", "Other destination", "Precision", "Recall", "F1"].map(h => <th key={h} className="px-3 py-3">{h}</th>)}</tr></thead>
      <tbody>
        {overall && models.length > 1 && <tr className="border-t border-slate-200 bg-slate-50 font-semibold"><td className="px-3 py-3">All models ({models.length})</td><td className="px-3 py-3">{overall.suggestions}</td><td className="px-3 py-3">{overall.approved}</td><td className="px-3 py-3">{overall.rejected}</td><td className="px-3 py-3">{overall.overridden}</td><td className="px-3 py-3">{metric(overall.precision)}</td><td className="px-3 py-3">{metric(overall.recall)}</td><td className="px-3 py-3">{metric(overall.f1)}</td></tr>}
        {models.map(model => <tr key={model.model_version} className="border-t border-slate-200"><td className="px-3 py-3 font-medium">Suggest {model.model_version}{model.reviewed === 0 && <div className="text-xs font-normal text-slate-500">Awaiting model reviews</div>}</td><td className="px-3 py-3">{model.suggestions}</td><td className="px-3 py-3">{model.approved}</td><td className="px-3 py-3">{model.rejected}</td><td className="px-3 py-3">{model.overridden}</td><td className="px-3 py-3">{metric(model.precision)}</td><td className="px-3 py-3">{metric(model.recall)}</td><td className="px-3 py-3">{metric(model.f1)}</td></tr>)}
      </tbody>
    </table></div>
  </main></div>;
}
