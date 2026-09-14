import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '@/api/axios';
import { useAuth } from '@/hooks/useAuth';

interface Token {
  id: number; label: string; suffix: string; created_at: string;
  expires_at: string | null; last_used_at: string | null; revoked_at: string | null;
}
interface Application {
  id: number; name: string; service_id: string; description: string;
  owner_contact: string; scopes: string; is_active: boolean; tokens: Token[];
}
type Draft = Omit<Application, 'id' | 'tokens'>;
const emptyDraft: Draft = { name: '', service_id: '', description: '', owner_contact: '', scopes: 'patient/*.read', is_active: true };
const scopeOptions = [
  ['patient/*.read', 'Read patient data'], ['patient/*.write', 'Write and delete patient data'],
  ['system/etl.write', 'ETL imports (no deletes)'], ['system/*.read', 'Read reference data'],
  ['user/*.read', 'Read user data'], ['user/*.write', 'Write user data'],
];
const endpoint = '/v1/service-applications/';
const inputClass = 'w-full rounded border border-gray-300 p-2 text-sm';
const buttonClass = 'rounded border border-gray-300 px-3 py-2 text-sm disabled:opacity-50';
const formatDate = (value: string | null) => value ? new Date(value).toLocaleString() : 'Never';

export default function ServiceApplicationsPage() {
  const { currentUser } = useAuth();
  const [apps, setApps] = useState<Application[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [tokenLabel, setTokenLabel] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [issued, setIssued] = useState<{ token: string; appName: string } | null>(null);
  const [revokeId, setRevokeId] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const selected = apps.find(app => app.id === selectedId);

  async function load() {
    const response = await api.get<Application[]>(endpoint);
    setApps(response.data);
  }
  useEffect(() => {
    if (!currentUser?.is_staff) return;
    let active = true;
    api.get<Application[]>(endpoint).then(response => { if (active) setApps(response.data); })
      .catch(() => { if (active) setError('Could not load service applications.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [currentUser?.is_staff]);

  function select(app: Application) {
    setSelectedId(app.id); setDraft({ ...app }); setIssued(null); setCopied(false);
    setRevokeId(null); setError(''); setTokenLabel(''); setExpiresAt('');
  }
  async function save() {
    if (!draft) return;
    setBusy(true); setError('');
    const { name, service_id, owner_contact, description, scopes, is_active } = draft;
    try {
      const payload = { name, service_id, owner_contact, description, scopes, is_active };
      const response = selectedId
        ? await api.patch<Application>(`${endpoint}${selectedId}/`, payload)
        : await api.post<Application>(endpoint, payload);
      await load(); select(response.data);
    } catch { setError('Could not save the application. Check the required fields and unique service ID.'); }
    finally { setBusy(false); }
  }
  async function createToken() {
    if (!selected) return;
    setBusy(true); setError(''); setIssued(null); setCopied(false);
    try {
      const response = await api.post<{ token: string }>(`${endpoint}${selected.id}/tokens/`, {
        label: tokenLabel, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      });
      // Kept only in component memory; never local/session storage or logs.
      setIssued({ token: response.data.token, appName: selected.name }); setTokenLabel('');
      await load();
    } catch { setError('Could not create the token. Check the label, expiry, and application status.'); }
    finally { setBusy(false); }
  }
  async function revoke() {
    if (!selected || revokeId === null) return;
    setBusy(true); setError('');
    try {
      await api.post(`${endpoint}${selected.id}/tokens/${revokeId}/revoke/`);
      setRevokeId(null); setIssued(null); await load();
    } catch { setError('Could not revoke the token.'); }
    finally { setBusy(false); }
  }
  if (!currentUser?.is_staff) return <p className="p-6">Staff access is required.</p>;

  return <main className="mx-auto max-w-6xl space-y-5 p-6">
    <Link to="/org-admin" className="text-sm text-blue-700">← Org Admin</Link>
    <div className="flex items-center justify-between gap-3">
      <div><h1 className="text-2xl font-semibold">Service applications</h1>
        <p className="text-sm text-gray-600">Manage application owners, access, and API tokens.</p></div>
      <button className={buttonClass} disabled={busy} onClick={() => {
        setSelectedId(null); setDraft({ ...emptyDraft }); setIssued(null); setRevokeId(null); setError('');
      }}>New application</button>
    </div>
    {error && <p role="alert" className="rounded bg-red-50 p-3 text-red-800">{error}</p>}
    {loading ? <p>Loading applications…</p> : <div className="grid gap-6 md:grid-cols-[240px_1fr]">
      <nav aria-label="Service applications" className="space-y-2">
        {apps.map(app => <button key={app.id} disabled={busy} onClick={() => select(app)}
          className={`block w-full rounded border p-3 text-left ${selectedId === app.id ? 'border-blue-600 bg-blue-50' : 'border-gray-200'}`}>
          <span className="block font-medium">{app.name}</span>
          <span className="block text-xs text-gray-600">{app.owner_contact || 'No owner assigned'} · {app.is_active ? 'Enabled' : 'Disabled'}</span>
        </button>)}
        {apps.length === 0 && <p className="text-sm text-gray-600">No applications yet.</p>}
      </nav>
      {draft ? <div className="space-y-6">
        <section className="space-y-3 rounded border border-gray-200 p-4">
          <h2 className="text-lg font-medium">{selected ? 'Application details' : 'New application'}</h2>
          <label className="block text-sm">Application name<input className={inputClass} value={draft.name}
            onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
          <label className="block text-sm">Service ID<input className={inputClass} value={draft.service_id}
            disabled={!!selected} onChange={e => setDraft({ ...draft, service_id: e.target.value })} /></label>
          <label className="block text-sm">Owner / contact<input className={inputClass} value={draft.owner_contact}
            onChange={e => setDraft({ ...draft, owner_contact: e.target.value })} /></label>
          <label className="block text-sm">Description<textarea className={inputClass} value={draft.description}
            onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
          <fieldset><legend className="text-sm font-medium">Access for all tokens in this application</legend>
            <p className="mb-2 text-xs text-gray-600">Service grants apply across patients. Changes affect existing tokens immediately.</p>
            {scopeOptions.map(([scope, label]) => <label key={scope} className="block text-sm">
              <input type="checkbox" checked={draft.scopes.split(' ').includes(scope)} onChange={e => {
                const values = new Set(draft.scopes.split(' ').filter(Boolean));
                if (e.target.checked) values.add(scope); else values.delete(scope);
                setDraft({ ...draft, scopes: [...values].join(' ') });
              }} /> {label}
            </label>)}
          </fieldset>
          <label className="block text-sm"><input type="checkbox" checked={draft.is_active}
            onChange={e => setDraft({ ...draft, is_active: e.target.checked })} /> Application enabled</label>
          {!draft.is_active && <p className="text-sm text-amber-800">Saving disables every token for this application.</p>}
          <button className={buttonClass} disabled={busy || !draft.name.trim() || !draft.service_id.trim()} onClick={save}>Save application</button>
        </section>
        {selected && <section className="space-y-3 rounded border border-gray-200 p-4">
          <h2 className="text-lg font-medium">Tokens</h2>
          <p className="text-sm text-gray-600">To rotate, create and distribute a replacement, then revoke the old token.</p>
          <label className="block text-sm">Token label<input className={inputClass} value={tokenLabel}
            onChange={e => setTokenLabel(e.target.value)} placeholder="e.g. Production integration" /></label>
          <label className="block text-sm">Expires at (optional)<input type="datetime-local" className={inputClass}
            value={expiresAt} onChange={e => setExpiresAt(e.target.value)} /></label>
          <button className={buttonClass} disabled={busy || !selected.is_active || !tokenLabel.trim()} onClick={createToken}>Create token</button>
          {issued && <div role="status" className="space-y-2 rounded border border-amber-300 bg-amber-50 p-3">
            <p className="font-medium">Copy the token for {issued.appName} now. It will not be shown again.</p>
            <input aria-label="New service token" readOnly value={issued.token} className={`${inputClass} font-mono`} />
            <p className="text-sm">Share through your secret manager. Do not paste it into issues or ordinary email.</p>
            <button className={buttonClass} onClick={async () => {
              try { await navigator.clipboard.writeText(issued.token); setCopied(true); }
              catch { setError('Clipboard unavailable. Select and copy the token above.'); }
            }}>{copied ? 'Copied' : 'Copy token'}</button>
            <button className={`${buttonClass} ml-2`} onClick={() => setIssued(null)}>Dismiss token</button>
          </div>}
          <ul className="divide-y divide-gray-200">
            {selected.tokens.map(token => <li key={token.id} className="flex items-start justify-between gap-3 py-3">
              <div><p className="font-medium">{token.label} <span className="font-mono text-sm">…{token.suffix}</span></p>
                <p className="text-xs text-gray-600">Created {formatDate(token.created_at)} · Last used {formatDate(token.last_used_at)}</p>
                <p className="text-xs text-gray-600">Expires: {token.expires_at ? formatDate(token.expires_at) : 'No expiry'} · {token.revoked_at ? 'Revoked' : token.expires_at && new Date(token.expires_at) <= new Date() ? 'Expired' : selected.is_active ? 'Active' : 'Disabled'}</p>
              </div>
              {!token.revoked_at && <button className={buttonClass} disabled={busy} onClick={() => setRevokeId(token.id)}>Revoke {token.label}</button>}
            </li>)}
          </ul>
          {revokeId !== null && <div role="alertdialog" aria-label="Confirm token revocation" className="rounded bg-red-50 p-3">
            <p>This token will stop working immediately. Existing replacements remain valid.</p>
            <button className={buttonClass} disabled={busy} onClick={revoke}>Confirm revocation</button>
            <button className={`${buttonClass} ml-2`} disabled={busy} onClick={() => setRevokeId(null)}>Cancel</button>
          </div>}
        </section>}
      </div> : <p className="text-gray-600">Choose an application to manage its access and tokens.</p>}
    </div>}
  </main>;
}
