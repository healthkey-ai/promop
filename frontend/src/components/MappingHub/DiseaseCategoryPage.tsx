import PageTitle from '@/components/Branding/PageTitle';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus, Search, Trash2 } from 'lucide-react';
import {
  fetchDiseaseCategoryMappings,
  createDiseaseCategoryMapping,
  updateDiseaseCategoryMapping,
  deleteDiseaseCategoryMapping,
  type DiseaseCategoryMappingItem,
} from '@/api/mappingHub';

const CATEGORIES = ['BC', 'MM', 'FL', 'CLL', 'MCL', 'Other'] as const;
const CATEGORY_LABELS: Record<string, string> = {
  BC: 'Breast Cancer',
  MM: 'Multiple Myeloma',
  FL: 'Follicular Lymphoma',
  CLL: 'Chronic Lymphocytic Leukemia',
  MCL: 'Mantle Cell Lymphoma',
  Other: 'Other',
};

export default function DiseaseCategoryPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<DiseaseCategoryMappingItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [newName, setNewName] = useState('');
  const [newCategory, setNewCategory] = useState<string>('MM');
  const [adding, setAdding] = useState(false);

  const load = useCallback(async (q?: string) => {
    setLoading(true);
    setError(null);
    try {
      setItems(await fetchDiseaseCategoryMappings(q));
    } catch {
      setError('Failed to load disease category mappings.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  const handleSearch = () => { void load(search || undefined); };

  const handleAdd = async () => {
    if (!newName.trim()) return;
    setAdding(true);
    setError(null);
    try {
      await createDiseaseCategoryMapping({ disease_name: newName.trim(), category: newCategory });
      setNewName('');
      await load(search || undefined);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg || 'Failed to create mapping.');
    } finally {
      setAdding(false);
    }
  };

  const handleCategoryChange = async (item: DiseaseCategoryMappingItem, category: string) => {
    try {
      await updateDiseaseCategoryMapping(item.id, { category });
      setItems(prev => prev.map(i => i.id === item.id ? { ...i, category } : i));
    } catch {
      setError('Failed to update category.');
    }
  };

  const handleDelete = async (item: DiseaseCategoryMappingItem) => {
    try {
      await deleteDiseaseCategoryMapping(item.id);
      setItems(prev => prev.filter(i => i.id !== item.id));
    } catch {
      setError('Failed to delete mapping.');
    }
  };

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="mb-6 flex items-center gap-3">
        <button onClick={() => navigate('/mappings')} className="rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Back to Mapping Hub">
          <ArrowLeft size={20} />
        </button>
        <div>
          <PageTitle className="text-2xl font-bold text-foreground">Disease Category Mappings</PageTitle>
          <p className="text-sm text-muted-foreground">Classify disease names into categories for the Concept to Code disease filter.</p>
        </div>
      </div>

      {error && <div className="mb-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}

      {/* Add form */}
      <div className="mb-4 flex flex-wrap items-end gap-2 rounded-lg border border-border bg-background p-4">
        <div className="flex-1 min-w-[200px]">
          <label htmlFor="new-disease-name" className="mb-1 block text-xs font-medium text-muted-foreground">Disease Name</label>
          <input
            id="new-disease-name"
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            placeholder="e.g. Smoldering myeloma"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') void handleAdd(); }}
          />
        </div>
        <div>
          <label htmlFor="new-category" className="mb-1 block text-xs font-medium text-muted-foreground">Category</label>
          <select
            id="new-category"
            className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            value={newCategory}
            onChange={e => setNewCategory(e.target.value)}
          >
            {CATEGORIES.map(c => <option key={c} value={c}>{c} — {CATEGORY_LABELS[c]}</option>)}
          </select>
        </div>
        <button
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          onClick={() => void handleAdd()}
          disabled={adding || !newName.trim()}
        >
          <Plus size={16} /> Add
        </button>
      </div>

      {/* Search */}
      <div className="mb-4 flex gap-2">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            className="w-full rounded-md border border-input bg-background pl-9 pr-3 py-1.5 text-sm"
            placeholder="Search disease names..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleSearch(); }}
          />
        </div>
        <button className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent" onClick={handleSearch}>Search</button>
      </div>

      {/* Table */}
      {loading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50">
              <tr>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Disease Name</th>
                <th className="px-4 py-2 text-left font-medium text-muted-foreground">Category</th>
                <th className="w-16 px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.length === 0 && (
                <tr><td colSpan={3} className="px-4 py-6 text-center text-muted-foreground">No mappings found.</td></tr>
              )}
              {items.map(item => (
                <tr key={item.id} className="hover:bg-muted/30">
                  <td className="px-4 py-2">{item.disease_name}</td>
                  <td className="px-4 py-2">
                    <select
                      className="rounded border border-input bg-background px-2 py-1 text-sm"
                      value={item.category}
                      onChange={e => void handleCategoryChange(item, e.target.value)}
                    >
                      {CATEGORIES.map(c => <option key={c} value={c}>{c} — {CATEGORY_LABELS[c]}</option>)}
                    </select>
                  </td>
                  <td className="px-4 py-2 text-center">
                    <button
                      className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      onClick={() => void handleDelete(item)}
                      aria-label={`Delete ${item.disease_name}`}
                    >
                      <Trash2 size={16} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs text-muted-foreground">{items.length} mapping{items.length !== 1 ? 's' : ''}</p>
    </div>
  );
}
