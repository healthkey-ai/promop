import { useEffect, useState } from 'react';
import api from '@/api/axios';

interface DiseaseCount {
  disease_slug: string;
  label: string;
  count: number;
}

interface OrgStats {
  org_slug: string;
  org_name: string;
  total: number;
  owned_count: number;
  accessible_count: number;
  disease_counts: DiseaseCount[];
}

export default function StatsPage() {
  const [data, setData] = useState<OrgStats[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    api.get<OrgStats[]>('/stats/org-disease/')
      .then(r => setData(r.data))
      .catch(() => setError(true));
  }, []);

  if (error) {
    return (
      <div className="p-8 text-center text-red-500">Failed to load stats. Please try again.</div>
    );
  }

  if (data === null) {
    return (
      <div className="p-8 text-center text-gray-500">Loading…</div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="p-8 text-center text-gray-500">
        No patient data available for your account.
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-semibold text-gray-900">Patient Summary by Organization</h1>
      {data.map(org => (
        <div key={org.org_slug} className="bg-white rounded-lg border border-gray-200 shadow-sm">
          <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
            <h2 className="text-lg font-medium text-gray-900">{org.org_name}</h2>
            <span className="text-sm text-gray-500">
              {org.accessible_count} accessible
              {org.accessible_count !== org.owned_count && (
                <span className="ml-1 text-gray-400">({org.owned_count} owned)</span>
              )}
            </span>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-6 py-2 font-medium text-gray-600">Disease</th>
                <th className="text-right px-6 py-2 font-medium text-gray-600">Patients</th>
              </tr>
            </thead>
            <tbody>
              {org.disease_counts.map(dc => (
                <tr key={dc.disease_slug} className="border-t border-gray-100">
                  <td className="px-6 py-2 text-gray-800">{dc.label}</td>
                  <td className="px-6 py-2 text-right text-gray-800">{dc.count}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-gray-200 bg-gray-50">
                <td className="px-6 py-2 font-medium text-gray-700">Owned</td>
                <td className="px-6 py-2 text-right font-medium text-gray-700">{org.owned_count}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      ))}
    </div>
  );
}
