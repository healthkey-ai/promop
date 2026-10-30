import PageTitle from '@/components/Branding/PageTitle';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Plus, Search, Trash2 } from 'lucide-react';
import {
  fetchDiseaseCategories,
  createDiseaseCategory,
  deleteDiseaseCategory,
  fetchDiseaseCategoryMappings,
  createDiseaseCategoryMapping,
  updateDiseaseCategoryMapping,
  deleteDiseaseCategoryMapping,
  type DiseaseCategoryMappingItem,
} from '@/api/mappingHub';

interface CategoryOption { value: string; label: string }

export default function DiseaseCategoryPage() {
  const navigate = useNavigate();
  const [categories, setCategories] = useState<CategoryOption[]>([]);
  const [items, setItems] = useState<DiseaseCategoryMappingItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [newName, setNewName] = useState('');
  const [newCategory, setNewCategory] = useState<string>('');
  const [adding, setAdding] = useState(false);

  // New category form
  const [showNewCat, setShowNewCat] = useState(false);
  const [newCatCode, setNewCatCode] = useState('');
  const [newCatLabel, setNewCatLabel] = useState('');
  const [addingCat, setAddingCat] = useState(false);

  const loadCategories = useCallback(async () => {
    try {
      const cats = await fetchDiseaseCategories();
      setCategories(cats);
      setNewCategory(prev => prev || (cats.length > 0 ? cats[0].value : ''));
    } catch {
      // Fallback handled by backend
    }
  }, []);

  const loadMappings = useCallback(async (q?: string) => {
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
    queueMicrotask(() => { void loadCategories(); });
  }, [loadCategories]);

  useEffect(() => {
    queueMicrotask(() => { void loadMappings(); });
  }, [loadMappings]);

  const handleSearch = () => { void loadMappings(search || undefined); };

  const handleAdd = async () => {
    if (!newName.trim() || !newCategory) return;
    setAdding(true);
    setError(null);
    try {
      await createDiseaseCategoryMapping({ disease_name: newName.trim(), category: newCategory });
      setNewName('');
      await loadMappings(search || undefined);
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

  const handleAddCategory = async () => {
    if (!newCatCode.trim() || !newCatLabel.trim()) return;
    setAddingCat(true);
    setError(null);
    try {
      await createDiseaseCategory({ code: newCatCode.trim(), label: newCatLabel.trim() });
      setNewCatCode('');
      setNewCatLabel('');
      setShowNewCat(false);
      await loadCategories();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg || 'Failed to create category.');
    } finally {
      setAddingCat(false);
    }
  };

  const handleDeleteCategory = async (code: string) => {
    setError(null);
    try {
      await deleteDiseaseCategory(code);
      await loadCategories();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(msg || 'Failed to delete category.');
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

      {/* Categories management */}
      <div className="mb-4 rounded-lg border border-border bg-background p-4">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-medium text-foreground">Categories</h3>
          <button
            className="inline-flex items-center gap-1 rounded-md border border-input px-2 py-1 text-xs hover:bg-accent"
            onClick={() => setShowNewCat(!showNewCat)}
          >
            <Plus size={14} /> New Category
          </button>
        </div>
        {showNewCat && (
          <div className="mb-3 flex flex-wrap items-end gap-2 rounded border border-dashed border-border p-3">
            <div>
              <label htmlFor="new-cat-code" className="mb-1 block text-xs font-medium text-muted-foreground">Code</label>
              <input
                id="new-cat-code"
                className="w-28 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                placeholder="e.g. DLBCL"
                value={newCatCode}
                onChange={e => setNewCatCode(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') void handleAddCategory(); }}
              />
            </div>
            <div className="flex-1 min-w-[160px]">
              <label htmlFor="new-cat-label" className="mb-1 block text-xs font-medium text-muted-foreground">Label</label>
              <input
                id="new-cat-label"
                className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
                placeholder="e.g. Diffuse Large B-Cell Lymphoma"
                value={newCatLabel}
                onChange={e => setNewCatLabel(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') void handleAddCategory(); }}
              />
            </div>
            <button
              className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              onClick={() => void handleAddCategory()}
              disabled={addingCat || !newCatCode.trim() || !newCatLabel.trim()}
            >
              Add
            </button>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {categories.map(cat => (
            <span key={cat.value} className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted/50 px-2.5 py-1 text-xs">
              <span className="font-medium">{cat.value}</span>
              <span className="text-muted-foreground">{cat.label}</span>
              <button
                className="ml-0.5 rounded-full p-0.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                onClick={() => void handleDeleteCategory(cat.value)}
                aria-label={`Delete category ${cat.value}`}
                title="Delete category"
              >
                <Trash2 size={12} />
              </button>
            </span>
          ))}
        </div>
      </div>

      {/* Add mapping form */}
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
            {categories.map(c => <option key={c.value} value={c.value}>{c.value} — {c.label}</option>)}
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
                      {categories.map(c => <option key={c.value} value={c.value}>{c.value} — {c.label}</option>)}
                      {/* Show current value even if its category was removed */}
                      {!categories.some(c => c.value === item.category) && (
                        <option value={item.category}>{item.category}</option>
                      )}
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
