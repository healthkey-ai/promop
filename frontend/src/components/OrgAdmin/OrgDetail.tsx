import { useEffect, useState } from 'react';
import { ArrowLeft, Trash2 } from 'lucide-react';
import api from '@/api/axios';

interface OrgDetailProps {
  slug: string;
  isStaff: boolean;
  onBack: () => void;
}

interface Org {
  id: number;
  name: string;
  slug: string;
  is_active: boolean;
  created_at: string;
}

interface Trust {
  id: number;
  granting_org_slug: string;
  trusted_org_slug: string | null;   // read-only, from serializer
  trusted_domain: string;
  created_at: string;
}

interface Invitation {
  id: number;
  org_slug: string;
  email: string;
  role: string;
  status: string;
  expires_at: string;
  created_at: string;
}

interface AccessGrant {
  id: number;
  email: string;
  org_slug: string;
  group_name: string | null;
  role: string;
  expires_at: string | null;
  granted_at: string;
}

type Section = 'settings' | 'trusts' | 'admins' | 'invitations';

export default function OrgDetail({ slug, isStaff, onBack }: OrgDetailProps) {
  const [org, setOrg] = useState<Org | null>(null);
  const [trusts, setTrusts] = useState<Trust[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [accessGrants, setAccessGrants] = useState<AccessGrant[]>([]);
  const [activeSection, setActiveSection] = useState<Section>('settings');
  const [error, setError] = useState<string | null>(null);

  // Settings form state
  const [orgName, setOrgName] = useState('');
  const [isActive, setIsActive] = useState(true);
  const [settingsSaved, setSettingsSaved] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  // Trust form state
  const [trustInput, setTrustInput] = useState('');
  const [trustType, setTrustType] = useState<'domain' | 'org_id'>('domain');
  const [trustError, setTrustError] = useState<string | null>(null);

  // Invite form state
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('doctor');
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteSuccess, setInviteSuccess] = useState<string | null>(null);

  const base = `/orgs/${slug}`;

  const fetchAll = async () => {
    try {
      const [orgRes, trustRes, invRes, accessRes] = await Promise.all([
        api.get<Org>(`${base}/`),
        api.get<Trust[]>(`${base}/trusts/`),
        api.get<Invitation[]>(`${base}/invitations/`),
        api.get<AccessGrant[]>(`${base}/access/`),
      ]);
      setOrg(orgRes.data);
      setOrgName(orgRes.data.name);
      setIsActive(orgRes.data.is_active);
      setTrusts(trustRes.data);
      setInvitations(invRes.data);
      setAccessGrants(accessRes.data);
    } catch {
      setError('Failed to load org details.');
    }
  };

  useEffect(() => { fetchAll(); }, [slug]);

  const handleSaveSettings = async () => {
    try {
      setSettingsError(null);
      const payload: Record<string, unknown> = { name: orgName };
      if (isStaff) payload.is_active = isActive;
      await api.patch(`${base}/`, payload);
      setSettingsSaved(true);
      setTimeout(() => setSettingsSaved(false), 2000);
    } catch {
      setSettingsError('Failed to save settings.');
    }
  };

  const handleAddTrust = async () => {
    try {
      setTrustError(null);
      const payload = trustType === 'domain'
        ? { trusted_domain: trustInput.trim() }
        : { trusted_org: parseInt(trustInput, 10) };
      await api.post(`${base}/trusts/`, payload);
      setTrustInput('');
      fetchAll();
    } catch {
      setTrustError('Failed to add trust. Check the value and try again.');
    }
  };

  const handleRemoveTrust = async (trustId: number) => {
    if (!window.confirm('Remove this trust? Users who access this org only via this trust will lose access immediately.')) return;
    try {
      await api.delete(`${base}/trusts/${trustId}/`);
      fetchAll();
    } catch (err) {
      console.error('Failed to remove trust:', err);
      setTrustError('Failed to remove trust. Please try again.');
    }
  };

  const handleInvite = async () => {
    try {
      setInviteError(null);
      setInviteSuccess(null);
      await api.post(`${base}/invite/`, { email: inviteEmail, role: inviteRole });
      setInviteSuccess(`Invitation sent to ${inviteEmail}.`);
      setInviteEmail('');
      fetchAll();
    } catch {
      setInviteError('Failed to send invitation.');
    }
  };

  const [cancelError, setCancelError] = useState<string | null>(null);

  const handleCancelInvitation = async (invId: number) => {
    if (!window.confirm('Cancel this invitation?')) return;
    try {
      await api.delete(`${base}/invitations/${invId}/`);
      fetchAll();
    } catch (err) {
      console.error('Failed to cancel invitation:', err);
      setCancelError('Failed to cancel invitation. Please try again.');
    }
  };

  const [accessError, setAccessError] = useState<string | null>(null);

  const handleRevokeAccess = async (accessId: number) => {
    if (!window.confirm('Revoke this access grant? The user will immediately lose access.')) return;
    try {
      await api.delete(`${base}/access/${accessId}/`);
      fetchAll();
    } catch (err) {
      console.error('Failed to revoke access:', err);
      setAccessError('Failed to revoke access. Please try again.');
    }
  };

  if (error) return <div className="p-8 text-red-500">{error}</div>;
  if (!org) return <div className="p-8 text-gray-500">Loading…</div>;

  const sections: { key: Section; label: string }[] = [
    { key: 'settings', label: 'Settings' },
    { key: 'trusts', label: 'Access Rules' },
    { key: 'admins', label: 'Admins' },
    { key: 'invitations', label: 'Invitations' },
  ];

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-800">
          <ArrowLeft size={14} /> Back
        </button>
        <h1 className="text-xl font-semibold text-gray-900">{org.name}</h1>
        <span className={`text-xs px-2 py-0.5 rounded font-medium ${org.is_active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
          {org.is_active ? 'Active' : 'Inactive'}
        </span>
      </div>

      {/* Section tabs */}
      <div className="flex gap-1 border-b border-gray-200">
        {sections.map(s => (
          <button
            key={s.key}
            onClick={() => setActiveSection(s.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
              activeSection === s.key
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {/* Settings */}
      {activeSection === 'settings' && (
        <div className="max-w-sm space-y-3">
          {settingsError && <p className="text-sm text-red-500">{settingsError}</p>}
          {settingsSaved && <p className="text-sm text-green-600">Saved.</p>}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Name</label>
            <input
              value={orgName}
              onChange={e => setOrgName(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
            />
          </div>
          {isStaff && (
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="is_active"
                checked={isActive}
                onChange={e => setIsActive(e.target.checked)}
                className="h-4 w-4"
              />
              <label htmlFor="is_active" className="text-sm text-gray-700">Active</label>
            </div>
          )}
          <button
            onClick={handleSaveSettings}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Save
          </button>
        </div>
      )}

      {/* Access Rules (Trusts) */}
      {activeSection === 'trusts' && (
        <div className="space-y-4">
          <h2 className="font-medium text-gray-900">Trusted Domains & Orgs</h2>
          {trusts.length === 0 ? (
            <p className="text-sm text-gray-500">No trusts configured.</p>
          ) : (
            <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg overflow-hidden">
              {trusts.map(t => (
                <li key={t.id} className="flex items-center justify-between px-4 py-2">
                  <span className="text-sm">
                    {t.trusted_domain
                      ? <span><span className="text-gray-500">Domain: </span>{t.trusted_domain}</span>
                      : <span><span className="text-gray-500">Org: </span>{t.trusted_org_slug}</span>}
                  </span>
                  <button
                    onClick={() => handleRemoveTrust(t.id)}
                    className="text-red-400 hover:text-red-600"
                    title="Remove trust"
                  >
                    <Trash2 size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Add trust form */}
          <div className="space-y-2 max-w-sm">
            {trustError && <p className="text-sm text-red-500">{trustError}</p>}
            <div className="flex gap-2">
              <select
                value={trustType}
                onChange={e => setTrustType(e.target.value as 'domain' | 'org_id')}
                className="border border-gray-300 rounded px-2 py-1.5 text-sm"
              >
                <option value="domain">Domain</option>
                <option value="org_id">Org ID</option>
              </select>
              <input
                type="text"
                placeholder={trustType === 'domain' ? 'e.g. hospital.org' : 'Org ID'}
                value={trustInput}
                onChange={e => setTrustInput(e.target.value)}
                className="flex-1 border border-gray-300 rounded px-3 py-1.5 text-sm"
              />
              <button
                onClick={handleAddTrust}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
              >
                Add
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Admins (org_admin access grants) */}
      {activeSection === 'admins' && (
        <div className="space-y-4">
          <h2 className="font-medium text-gray-900">Access Grants</h2>
          {accessError && <p className="text-sm text-red-500">{accessError}</p>}
          {accessGrants.length === 0 ? (
            <p className="text-sm text-gray-500">No access grants.</p>
          ) : (
            <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg overflow-hidden">
              {accessGrants.map(g => (
                <li key={g.id} className="flex items-center justify-between px-4 py-2">
                  <span className="text-sm">
                    <span className="font-medium">{g.email}</span>
                    <span className="text-gray-400 ml-2">({g.role})</span>
                  </span>
                  <button
                    onClick={() => handleRevokeAccess(g.id)}
                    className="text-red-400 hover:text-red-600"
                    title="Revoke access"
                  >
                    <Trash2 size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Invite form */}
          <div className="space-y-2 max-w-sm">
            <h3 className="text-sm font-medium text-gray-700">Invite user</h3>
            {inviteError && <p className="text-sm text-red-500">{inviteError}</p>}
            {inviteSuccess && <p className="text-sm text-green-600">{inviteSuccess}</p>}
            <input
              type="email"
              placeholder="Email address"
              value={inviteEmail}
              onChange={e => setInviteEmail(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
            />
            <div className="flex gap-2">
              <select
                value={inviteRole}
                onChange={e => setInviteRole(e.target.value)}
                className="border border-gray-300 rounded px-2 py-1.5 text-sm"
              >
                <option value="org_admin">Org Admin</option>
                <option value="doctor">Doctor</option>
                <option value="navigator">Navigator</option>
              </select>
              <button
                onClick={handleInvite}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
              >
                Send Invite
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Invitations */}
      {activeSection === 'invitations' && (
        <div className="space-y-3">
          <h2 className="font-medium text-gray-900">Invitations</h2>
          {cancelError && <p className="text-sm text-red-500">{cancelError}</p>}
          {invitations.length === 0 ? (
            <p className="text-sm text-gray-500">No invitations.</p>
          ) : (
            <ul className="divide-y divide-gray-100 border border-gray-200 rounded-lg overflow-hidden">
              {invitations.map(inv => (
                <li key={inv.id} className="flex items-center justify-between px-4 py-2">
                  <span className="text-sm">
                    <span className="font-medium">{inv.email}</span>
                    <span className="text-gray-400 ml-2">({inv.role})</span>
                    <span className={`ml-2 text-xs px-1.5 py-0.5 rounded font-medium ${
                      inv.status === 'pending' ? 'bg-yellow-100 text-yellow-700'
                        : inv.status === 'confirmed' ? 'bg-green-100 text-green-700'
                        : 'bg-gray-100 text-gray-500'
                    }`}>
                      {inv.status}
                    </span>
                  </span>
                  {inv.status === 'pending' && (
                    <button
                      onClick={() => handleCancelInvitation(inv.id)}
                      className="text-red-400 hover:text-red-600"
                      title="Cancel invitation"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
