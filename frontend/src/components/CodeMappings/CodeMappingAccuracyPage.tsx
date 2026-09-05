import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "@/api/axios";

interface ModelAccuracy {
  model_version: string;
  suggestions: number;
  approved: number;
  rejected: number;
  overridden: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
}
const metric = (value: number | null) => value === null ? "—" : `${(value * 100).toFixed(1)}%`;

export default function CodeMappingAccuracyPage() {
  const [models, setModels] = useState<ModelAccuracy[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { void api.get<{ models: ModelAccuracy[] }>("/v1/code-mappings/accuracy/dashboard/")
    .then(({ data }) => setModels(data.models || [])).catch(() => setError("Failed to load suggestion accuracy.")); }, []);
  return <div className="min-h-screen bg-slate-50 p-6"><main className="mx-auto max-w-6xl">
    <Link to="/code-mappings" className="text-sm text-slate-700 underline">← Code Mapping</Link>
    <h1 className="mt-4 text-2xl font-semibold text-slate-950">Suggestion model accuracy</h1>
    <p className="mt-1 text-sm text-slate-600">Latest model first. Approved means the curator retained the suggested destination.</p>
    {error && <p className="mt-4 text-sm text-red-700">{error}</p>}
    <div className="mt-5 overflow-hidden rounded-md border border-slate-200 bg-white"><table className="w-full text-left text-sm">
      <thead className="bg-slate-100 text-xs uppercase text-slate-600"><tr>{["Model", "Suggestions", "Approved", "Rejected", "Other destination", "Precision", "Recall", "F1"].map(h => <th key={h} className="px-3 py-3">{h}</th>)}</tr></thead>
      <tbody>{models.map(model => <tr key={model.model_version} className="border-t border-slate-200"><td className="px-3 py-3 font-medium">{model.model_version}</td><td className="px-3 py-3">{model.suggestions}</td><td className="px-3 py-3">{model.approved}</td><td className="px-3 py-3">{model.rejected}</td><td className="px-3 py-3">{model.overridden}</td><td className="px-3 py-3">{metric(model.precision)}</td><td className="px-3 py-3">{metric(model.recall)}</td><td className="px-3 py-3">{metric(model.f1)}</td></tr>)}</tbody>
    </table></div>
  </main></div>;
}
