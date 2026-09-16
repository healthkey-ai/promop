import PageTitle from '@/components/Branding/PageTitle';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useAuth, type EffectiveRole } from '@/hooks/useAuth';

const ROLE_LABELS: Record<string, string> = {
  staff: 'Staff', org_admin: 'Org Admin', doctor: 'Doctor',
  analyst: 'Analyst', patient: 'Patient',
};
const SOURCE_LABELS: Record<EffectiveRole['source'], string> = {
  staff_flag: 'Staff account', patient_link: 'Linked patient account',
  org_grant: 'Organization grant', group_grant: 'Group grant',
  organization_trust: 'Organization trust', domain_trust: 'Email-domain trust',
};

function scopeLabel(role: EffectiveRole) {
  if (role.role === 'patient' && role.scope !== 'patient') {
    return `Patient membership in ${role.org_name} (own record only)`;
  }
  switch (role.scope) {
    case 'platform': return 'All organizations';
    case 'patient': return `Own patient record #${role.person_id}`;
    case 'group': return `${role.org_name} — ${role.group_name}`;
    case 'organization': return role.org_name;
  }
}

export default function UserProfilePage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  if (!currentUser) return <div className="p-8 text-center text-gray-500">Not logged in.</div>;

  const roles = currentUser.effective_roles ?? [];
  const delegations = currentUser.patient_delegations ?? [];
  const notices = (currentUser.org_accesses ?? []).filter(a => !a.role);

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <button onClick={() => navigate('/')} className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800">
          <ArrowLeft size={14} /> Back
        </button>
        <PageTitle className="text-2xl font-semibold text-gray-900">My Profile</PageTitle>
      </div>
      <div className="bg-white border border-gray-200 rounded-lg divide-y divide-gray-100">
        <div className="px-6 py-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Email</p>
          <p className="text-gray-900 font-medium">{currentUser.email}</p>
        </div>
        <section className="px-6 py-4" aria-labelledby="active-roles">
          <h2 id="active-roles" className="text-sm font-semibold mb-3">Active roles</h2>
          {roles.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead><tr>
                  {['Role', 'Scope', 'Granted through', 'Expires'].map(label => (
                    <th key={label} className="pb-2 pr-4 font-medium">{label}</th>
                  ))}
                </tr></thead>
                <tbody>{roles.map((role, index) => (
                  <tr key={index} className="border-t border-gray-100">
                    <td className="py-2 pr-4">{ROLE_LABELS[role.role]}</td>
                    <td className="py-2 pr-4">{scopeLabel(role)}</td>
                    <td className="py-2 pr-4">{SOURCE_LABELS[role.source]}
                      {role.source_org_name ? ` from ${role.source_org_name}` : ''}
                      {role.source_domain ? ` for @${role.source_domain}` : ''}
                    </td>
                    <td className="py-2">{role.expires_at ? new Date(role.expires_at).toLocaleDateString() : 'Never'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <p className="text-sm text-gray-500">No active application roles.</p>}
        </section>
        {delegations.length > 0 && (
          <section className="px-6 py-4" aria-labelledby="delegated-access">
            <h2 id="delegated-access" className="text-sm font-semibold mb-3">Delegated patient access</h2>
            <ul className="space-y-2 text-sm">{delegations.map(access => (
              <li key={access.person_id}>Patient record #{access.person_id} — verified {access.relationship}</li>
            ))}</ul>
          </section>
        )}
        {notices.length > 0 && (
          <section className="px-6 py-4" aria-labelledby="organization-notices">
            <h2 id="organization-notices" className="text-sm font-semibold mb-3">Pending invitations</h2>
            <ul className="space-y-2 text-sm">{notices.map((access, index) => (
              <li key={index}>
                <span className="font-medium">{access.org_name}</span>{' — '}
                {access.access_via?.includes('invitation_pending')
                  ? `Invitation pending${access.pending_role ? ` (${ROLE_LABELS[access.pending_role] ?? access.pending_role})` : ''}; no role granted yet.`
                  : 'Trusted domain; no application role granted.'}
              </li>
            ))}</ul>
          </section>
        )}
      </div>
    </div>
  );
}
