import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus } from 'lucide-react';
import api from '@/api/axios';
import { useAuth } from '@/hooks/useAuth';
import OrgDetail from './OrgDetail';

interface Org {
  id: number;
  name: string;
  slug: string;
  is_active: boolean;
  created_at: string;
}

export default function OrgAdminPage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  const [orgs, setOrgs] = useState<Org[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newOrgName, setNewOrgName] = useState('');
  const [newOrgSlug, setNewOrgSlug] = useState('');
  const [createError, setCreateError] = useState<string | null>(null);

  const fetchOrgs = () => {
    api.get<Org[]>('/api/orgs/')
      .then(r => setOrgs(r.data))
      .catch((err) => {
        console.error('Failed to load organizations:', err);
        setError('Failed to load organizations.');
      });
  };

  useEffect(() => {
    fetchOrgs();
  }, []);

  const handleCreate = async () => {
    if (!newOrgName.trim() || !newOrgSlug.trim()) {
      setCreateError('Name and slug are required.');
      return;
    }
    try {
      await api.post('/api/orgs/', { name: newOrgName.trim(), slug: newOrgSlug.trim() });
      setCreating(false);
      setNewOrgName('');
      setNewOrgSlug('');
      setCreateError(null);
      fetchOrgs();
    } catch {
      setCreateError('Failed to create organization.');
    }
  };

  if (error) {
    return (
      <div className="p-8 text-center text-red-500">{error}</div>
    );
  }

  if (orgs === null) {
    return (
      <div className="p-8 text-center text-gray-500">Loading…</div>
    );
  }

  if (selectedSlug) {
    return (
      <OrgDetail
        slug={selectedSlug}
        isStaff={!!currentUser?.is_staff}
        onBack={() => { setSelectedSlug(null); fetchOrgs(); }}
      />
    );
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/')}
            className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800"
          >
            <ArrowLeft size={14} /> Back
          </button>
          <h1 className="text-2xl font-semibold text-gray-900">Org Admin</h1>
        </div>
        {currentUser?.is_staff && (
          <button
            onClick={() => setCreating(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            <Plus size={14} /> Create Org
          </button>
        )}
      </div>

      {creating && (
        <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-3 max-w-md">
          <h2 className="font-medium text-gray-900">New Organization</h2>
          {createError && <p className="text-sm text-red-500">{createError}</p>}
          <input
            type="text"
            placeholder="Name"
            value={newOrgName}
            onChange={e => setNewOrgName(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
          <input
            type="text"
            placeholder="Slug (e.g. my-org)"
            value={newOrgSlug}
            onChange={e => setNewOrgSlug(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
          <div className="flex gap-2">
            <button
              onClick={handleCreate}
              className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
            >
              Create
            </button>
            <button
              onClick={() => { setCreating(false); setCreateError(null); }}
              className="px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {orgs.length === 0 ? (
        <p className="text-gray-500">No organizations found.</p>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Slug</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {orgs.map(org => (
                <tr key={org.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-900">{org.name}</td>
                  <td className="px-4 py-3 text-gray-500">{org.slug}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                      org.is_active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                    }`}>
                      {org.is_active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setSelectedSlug(org.slug)}
                      className="text-sm text-blue-600 hover:underline"
                    >
                      Manage
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
