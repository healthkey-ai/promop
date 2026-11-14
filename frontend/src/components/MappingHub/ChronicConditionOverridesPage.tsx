import PageTitle from '@/components/Branding/PageTitle';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus, Search, Trash2 } from 'lucide-react';
import {
  fetchChronicConditionOverrides,
  createChronicConditionOverride,
  updateChronicConditionOverride,
  deleteChronicConditionOverride,
  type ChronicConditionOverrideItem,
} from '@/api/mappingHub';
import { useAuth } from '@/hooks/useAuth';
import { isStaffOrOrgAdmin } from '@/utils/roles';

const detail = (e: unknown, fallback: string) =>
  (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || fallback;

export default function ChronicConditionOverridesPage() {
  const navigate = useNavigate();
  const { currentUser } = useAuth();
  // Writes are org_admin-only on the API (`_require_mapping_admin`); every
  // other signed-in professional reads.
  const canEdit = isStaffOrOrgAdmin(currentUser);
  const [items, setItems] = useState<ChronicConditionOverrideItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [code, setCode] = useState('');
  const [label, setLabel] = useState('');
  const [notes, setNotes] = useState('');
  const [isChronic, setIsChronic] = useState(true);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async (q?: string) => {
    setLoading(true);
    setError(null);
    try {
      setItems(await fetchChronicConditionOverrides(q));
    } catch {
      setError('Failed to load chronic condition overrides.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  const handleAdd = async () => {
    if (!code.trim()) return;
    setAdding(true);
    setError(null);
    try {
      await createChronicConditionOverride({ code: code.trim(), is_chronic: isChronic, label: label.trim(), notes: notes.trim() });
      setCode(''); setLabel(''); setNotes('');
      await load(search || undefined);
    } catch (e: unknown) {
      setError(detail(e, 'Failed to create override.'));
    } finally {
      setAdding(false);
    }
  };

  const handleChange = async (item: ChronicConditionOverrideItem, is_chronic: boolean) => {
    setError(null);
    try {
      const saved = await updateChronicConditionOverride(item.id, { is_chronic });
      setItems(prev => prev.map(i => i.id === item.id ? saved : i));
    } catch (e: unknown) {
      setError(detail(e, 'Failed to update override.'));
    }
  };

  const handleDelete = async (item: ChronicConditionOverrideItem) => {
    setError(null);
    try {
      await deleteChronicConditionOverride(item.id);
      setItems(prev => prev.filter(i => i.id !== item.id));
    } catch (e: unknown) {
      setError(detail(e, 'Failed to delete override.'));
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-6 flex items-center gap-3">
        <button onClick={() => navigate('/mappings')} className="rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Back to Mapping Hub">
          <ArrowLeft size={20} />
        </button>
        <div>
          <PageTitle className="text-2xl font-bold text-foreground">Chronic Condition Overrides</PageTitle>
          <p className="text-sm text-muted-foreground">
            Say whether an ICD-10-CM code is a chronic condition, over what the AHRQ Chronic Condition Indicator says.
            A code covers the codes under it (M85.8 covers M85.80); the most specific code wins.
          </p>
        </div>
      </div>

      {error && <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}

      {!canEdit && (
        <p role="status" className="mb-4 text-sm text-muted-foreground">Read-only: editing reference data needs the org admin role.</p>
      )}

      {canEdit && <div className="mb-4 flex flex-wrap items-end gap-2 rounded-lg border border-border bg-background p-4">
        <div>
          <label htmlFor="override-code" className="mb-1 block text-xs font-medium text-muted-foreground">ICD-10-CM code</label>
          <input id="override-code" className="w-32 rounded-md border border-input bg-background px-3 py-1.5 text-sm" placeholder="e.g. M85.8"
            value={code} onChange={e => setCode(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void handleAdd(); }} />
        </div>
        <div>
          <label htmlFor="override-verdict" className="mb-1 block text-xs font-medium text-muted-foreground">Counts as</label>
          <select id="override-verdict" className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            value={isChronic ? 'chronic' : 'not'} onChange={e => setIsChronic(e.target.value === 'chronic')}>
            <option value="chronic">Chronic</option>
            <option value="not">Not chronic</option>
          </select>
        </div>
        <div className="min-w-[160px] flex-1">
          <label htmlFor="override-label" className="mb-1 block text-xs font-medium text-muted-foreground">Name</label>
          <input id="override-label" className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm" placeholder="e.g. Osteopenia"
            value={label} onChange={e => setLabel(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void handleAdd(); }} />
        </div>
        <div className="min-w-[200px] flex-1">
          <label htmlFor="override-notes" className="mb-1 block text-xs font-medium text-muted-foreground">Why</label>
          <input id="override-notes" className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm" placeholder="Optional"
            value={notes} onChange={e => setNotes(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void handleAdd(); }} />
        </div>
        <button className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          onClick={() => void handleAdd()} disabled={adding || !code.trim()}>
          <Plus size={16} /> Add
        </button>
      </div>}

      <div className="mb-4 flex gap-2">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input className="w-full rounded-md border border-input bg-background py-1.5 pl-9 pr-3 text-sm" placeholder="Search codes and names..."
            value={search} onChange={e => setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void load(search || undefined); }} />
        </div>
        <button className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent" onClick={() => void load(search || undefined)}>Search</button>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Code</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Name</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Counts as</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Why</th>
                {canEdit && <th className="w-16 px-4 py-2" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.length === 0 && (
                <tr><td colSpan={canEdit ? 5 : 4} className="px-4 py-6 text-center text-muted-foreground">No overrides found.</td></tr>
              )}
              {items.map(item => (
                <tr key={item.id} className="hover:bg-muted/30">
                  <td className="px-4 py-2 font-mono">{item.code}</td>
                  <td className="px-4 py-2">{item.label}</td>
                  <td className="px-4 py-2">
                    {canEdit ? (
                      <select aria-label={`Counts as for ${item.code}`} className="rounded border border-input bg-background px-2 py-1 text-sm"
                        value={item.is_chronic ? 'chronic' : 'not'} onChange={e => void handleChange(item, e.target.value === 'chronic')}>
                        <option value="chronic">Chronic</option>
                        <option value="not">Not chronic</option>
                      </select>
                    ) : (
                      <span>{item.is_chronic ? 'Chronic' : 'Not chronic'}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-muted-foreground">{item.notes}</td>
                  {canEdit && (
                    <td className="px-4 py-2 text-center">
                      <button className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                        onClick={() => void handleDelete(item)} aria-label={`Delete ${item.code}`}>
                        <Trash2 size={16} />
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs text-muted-foreground">{items.length} override{items.length !== 1 ? 's' : ''}</p>
    </div>
  );
}
