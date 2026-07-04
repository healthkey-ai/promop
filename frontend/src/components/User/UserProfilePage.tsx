import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';

const ROLE_LABELS: Record<string, string> = {
  org_admin: 'Org Admin',
  doctor: 'Doctor',
  navigator: 'Navigator',
};

export default function UserProfilePage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();

  if (!currentUser) {
    return <div className="p-8 text-center text-gray-500">Not logged in.</div>;
  }

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/')}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800"
        >
          <ArrowLeft size={14} /> Back
        </button>
        <h1 className="text-2xl font-semibold text-gray-900">My Profile</h1>
      </div>

      <div className="bg-white border border-gray-200 rounded-lg divide-y divide-gray-100">
        <div className="px-6 py-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Email</p>
          <p className="text-gray-900 font-medium">{currentUser.email}</p>
        </div>

        <div className="px-6 py-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-2">System Rights</p>
          <div className="flex gap-2 flex-wrap">
            {currentUser.is_superuser && (
              <span className="inline-flex items-center px-2.5 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
                Superuser
              </span>
            )}
            {currentUser.is_staff && (
              <span className="inline-flex items-center px-2.5 py-0.5 rounded text-xs font-medium bg-orange-100 text-orange-700">
                Staff
              </span>
            )}
            {!currentUser.is_staff && !currentUser.is_superuser && (
              <span className="inline-flex items-center px-2.5 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600">
                Standard User
              </span>
            )}
          </div>
        </div>

        <div className="px-6 py-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-3">Org Access</p>
          {(!currentUser.org_accesses || currentUser.org_accesses.length === 0) ? (
            currentUser.is_staff ? (
              <p className="text-sm text-gray-500">Staff — full access to all organizations.</p>
            ) : (
              <p className="text-sm text-gray-500">No explicit org grants. Access may be via domain trust.</p>
            )
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 uppercase tracking-wide">
                  <th className="pb-2 pr-4 font-medium">Organization</th>
                  <th className="pb-2 pr-4 font-medium">Role</th>
                  <th className="pb-2 font-medium">Expires</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {currentUser.org_accesses.map((a, i) => (
                  <tr key={i}>
                    <td className="py-2 pr-4 text-gray-900">{a.org_name}</td>
                    <td className="py-2 pr-4">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700">
                        {ROLE_LABELS[a.role] ?? a.role}
                      </span>
                    </td>
                    <td className="py-2 text-gray-500">
                      {a.expires_at
                        ? new Date(a.expires_at).toLocaleDateString()
                        : 'Never'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
