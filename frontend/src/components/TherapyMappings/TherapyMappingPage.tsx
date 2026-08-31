import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ChevronDown, ChevronRight, Plus, Search, Trash2, X } from 'lucide-react';
import {
  fetchRegimens, fetchRegimenDetail, createRegimen, deleteRegimen,
  addRegimenComponent, removeRegimenComponent,
  fetchComponents, createComponent,
  addComponentClass, removeComponentClass,
  fetchClasses, createClass,
  fetchDiseaseTherapyRegimens, addDiseaseTherapyRegimen, removeDiseaseTherapyRegimen,
  type TherapyRegimenItem, type TherapyComponentItem, type TherapyClassItem,
  type DiseaseTherapyRegimenItem,
} from '@/api/mappingHub';

type Tab = 'regimen-component' | 'component-class' | 'disease-regimen';

// ---------------------------------------------------------------------------
// Regimen-Component Tab
// ---------------------------------------------------------------------------

function RegimenComponentTab() {
  const [regimens, setRegimens] = useState<TherapyRegimenItem[]>([]);
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<TherapyRegimenItem | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [allComponents, setAllComponents] = useState<TherapyComponentItem[]>([]);
  const [showAddRegimen, setShowAddRegimen] = useState(false);
  const [newRegimen, setNewRegimen] = useState({ code: '', title: '', concept_id: '' });
  const [error, setError] = useState<string | null>(null);

  const loadRegimens = useCallback(async () => {
    try {
      const data = await fetchRegimens(search || undefined);
      setRegimens(data);
    } catch {
      setError('Failed to load regimens.');
    }
  }, [search]);

  useEffect(() => {
    (async () => {
      await loadRegimens();
    })();
  }, [loadRegimens]);

  const toggleExpand = useCallback(async (code: string) => {
    if (expanded === code) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(code);
    setLoadingDetail(true);
    try {
      const [d, comps] = await Promise.all([
        fetchRegimenDetail(code),
        allComponents.length ? Promise.resolve(allComponents) : fetchComponents(),
      ]);
      setDetail(d);
      if (!allComponents.length) setAllComponents(comps);
    } catch {
      setError('Failed to load regimen detail.');
    } finally {
      setLoadingDetail(false);
    }
  }, [expanded, allComponents]);

  const handleAddComponent = useCallback(async (regimenCode: string, componentCode: string) => {
    try {
      await addRegimenComponent(regimenCode, componentCode);
      const d = await fetchRegimenDetail(regimenCode);
      setDetail(d);
    } catch {
      setError('Failed to add component.');
    }
  }, []);

  const handleRemoveComponent = useCallback(async (regimenCode: string, componentCode: string) => {
    try {
      await removeRegimenComponent(regimenCode, componentCode);
      const d = await fetchRegimenDetail(regimenCode);
      setDetail(d);
    } catch {
      setError('Failed to remove component.');
    }
  }, []);

  const handleCreateRegimen = useCallback(async () => {
    if (!newRegimen.code || !newRegimen.title) return;
    try {
      await createRegimen({
        code: newRegimen.code,
        title: newRegimen.title,
        concept_id: newRegimen.concept_id ? Number(newRegimen.concept_id) : null,
      });
      setNewRegimen({ code: '', title: '', concept_id: '' });
      setShowAddRegimen(false);
      await loadRegimens();
    } catch {
      setError('Failed to create regimen.');
    }
  }, [newRegimen, loadRegimens]);

  const handleDeleteRegimen = useCallback(async (code: string) => {
    try {
      await deleteRegimen(code);
      setExpanded(null);
      setDetail(null);
      await loadRegimens();
    } catch {
      setError('Failed to delete regimen.');
    }
  }, [loadRegimens]);

  const existingCodes = new Set(detail?.components?.map(c => c.code) ?? []);
  const availableComponents = allComponents.filter(c => !existingCodes.has(c.code));

  return (
    <div>
      {error && (
        <div className="mb-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive flex items-center justify-between">
          {error}
          <button onClick={() => setError(null)}><X size={14} /></button>
        </div>
      )}

      <div className="mb-4 flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            className="h-9 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm"
            placeholder="Search regimens..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button
          onClick={() => setShowAddRegimen(!showAddRegimen)}
          className="flex items-center gap-1.5 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
        >
          <Plus size={14} /> Add Regimen
        </button>
      </div>

      {showAddRegimen && (
        <div className="mb-4 rounded-md border border-border bg-muted/30 p-4 flex items-end gap-3">
          <label className="flex flex-col gap-1 text-sm flex-1">
            Code
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newRegimen.code} onChange={e => setNewRegimen(r => ({ ...r, code: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm flex-1">
            Title
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newRegimen.title} onChange={e => setNewRegimen(r => ({ ...r, title: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm w-36">
            Concept ID
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newRegimen.concept_id} onChange={e => setNewRegimen(r => ({ ...r, concept_id: e.target.value }))} />
          </label>
          <button onClick={handleCreateRegimen} className="h-8 rounded-md bg-primary px-3 text-sm text-primary-foreground hover:bg-primary/90">Create</button>
          <button onClick={() => setShowAddRegimen(false)} className="h-8 rounded-md border border-input px-3 text-sm hover:bg-accent">Cancel</button>
        </div>
      )}

      <div className="rounded-md border border-border">
        {regimens.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground text-center">No regimens found.</div>
        ) : (
          regimens.map((reg) => (
            <div key={reg.code} className="border-b border-border last:border-b-0">
              <div
                className="flex items-center justify-between px-4 py-2.5 hover:bg-muted/50 cursor-pointer"
                onClick={() => toggleExpand(reg.code)}
              >
                <div className="flex items-center gap-2">
                  {expanded === reg.code ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  <span className="font-medium text-sm">{reg.title}</span>
                  <span className="text-xs text-muted-foreground">({reg.code})</span>
                  {reg.concept_id && <span className="text-xs text-muted-foreground">HemOnc:{reg.concept_id}</span>}
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteRegimen(reg.code); }}
                  className="text-muted-foreground hover:text-destructive p-1"
                  aria-label={`Delete ${reg.title}`}
                >
                  <Trash2 size={13} />
                </button>
              </div>

              {expanded === reg.code && (
                <div className="px-8 pb-3 pt-1">
                  {loadingDetail ? (
                    <div className="text-sm text-muted-foreground">Loading components...</div>
                  ) : (
                    <>
                      <div className="text-xs font-medium text-muted-foreground mb-2">Components</div>
                      {detail?.components?.length ? (
                        <div className="space-y-1 mb-3">
                          {detail.components.map(comp => (
                            <div key={comp.code} className="flex items-center justify-between rounded bg-muted/40 px-3 py-1.5 text-sm">
                              <div>
                                <span className="font-medium">{comp.title}</span>
                                <span className="text-xs text-muted-foreground ml-2">({comp.code})</span>
                                {comp.classes?.length ? (
                                  <span className="ml-2 text-xs text-blue-600">
                                    [{comp.classes.map(cl => cl.title).join(', ')}]
                                  </span>
                                ) : null}
                              </div>
                              <button
                                onClick={() => handleRemoveComponent(reg.code, comp.code)}
                                className="text-muted-foreground hover:text-destructive"
                                aria-label={`Remove ${comp.title}`}
                              >
                                <X size={13} />
                              </button>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm text-muted-foreground mb-3">No components assigned.</div>
                      )}

                      {availableComponents.length > 0 && (
                        <select
                          className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                          value=""
                          onChange={(e) => { if (e.target.value) handleAddComponent(reg.code, e.target.value); }}
                        >
                          <option value="">+ Add component...</option>
                          {availableComponents.map(c => (
                            <option key={c.code} value={c.code}>{c.title} ({c.code})</option>
                          ))}
                        </select>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component-Class Tab
// ---------------------------------------------------------------------------

function ComponentClassTab() {
  const [components, setComponents] = useState<TherapyComponentItem[]>([]);
  const [allClasses, setAllClasses] = useState<TherapyClassItem[]>([]);
  const [search, setSearch] = useState('');
  const [showAddComponent, setShowAddComponent] = useState(false);
  const [showAddClass, setShowAddClass] = useState(false);
  const [newComponent, setNewComponent] = useState({ code: '', title: '', concept_id: '' });
  const [newClass, setNewClass] = useState({ code: '', title: '', concept_id: '' });
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [comps, cls] = await Promise.all([
        fetchComponents(search || undefined),
        fetchClasses(),
      ]);
      setComponents(comps);
      setAllClasses(cls);
    } catch {
      setError('Failed to load data.');
    }
  }, [search]);

  useEffect(() => {
    (async () => {
      await loadData();
    })();
  }, [loadData]);

  const handleAddClass = useCallback(async (componentCode: string, classCode: string) => {
    try {
      await addComponentClass(componentCode, classCode);
      await loadData();
    } catch {
      setError('Failed to add class.');
    }
  }, [loadData]);

  const handleRemoveClass = useCallback(async (componentCode: string, classCode: string) => {
    try {
      await removeComponentClass(componentCode, classCode);
      await loadData();
    } catch {
      setError('Failed to remove class.');
    }
  }, [loadData]);

  const handleCreateComponent = useCallback(async () => {
    if (!newComponent.code || !newComponent.title) return;
    try {
      await createComponent({
        code: newComponent.code,
        title: newComponent.title,
        concept_id: newComponent.concept_id ? Number(newComponent.concept_id) : null,
      });
      setNewComponent({ code: '', title: '', concept_id: '' });
      setShowAddComponent(false);
      await loadData();
    } catch {
      setError('Failed to create component.');
    }
  }, [newComponent, loadData]);

  const handleCreateClass = useCallback(async () => {
    if (!newClass.code || !newClass.title) return;
    try {
      await createClass({
        code: newClass.code,
        title: newClass.title,
        concept_id: newClass.concept_id ? Number(newClass.concept_id) : null,
      });
      setNewClass({ code: '', title: '', concept_id: '' });
      setShowAddClass(false);
      await loadData();
    } catch {
      setError('Failed to create class.');
    }
  }, [newClass, loadData]);

  return (
    <div>
      {error && (
        <div className="mb-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive flex items-center justify-between">
          {error}
          <button onClick={() => setError(null)}><X size={14} /></button>
        </div>
      )}

      <div className="mb-4 flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            className="h-9 w-full rounded-md border border-input bg-background pl-9 pr-3 text-sm"
            placeholder="Search components..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <button
          onClick={() => setShowAddComponent(!showAddComponent)}
          className="flex items-center gap-1.5 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
        >
          <Plus size={14} /> Add Component
        </button>
        <button
          onClick={() => setShowAddClass(!showAddClass)}
          className="flex items-center gap-1.5 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
        >
          <Plus size={14} /> Add Class
        </button>
      </div>

      {showAddComponent && (
        <div className="mb-4 rounded-md border border-border bg-muted/30 p-4 flex items-end gap-3">
          <label className="flex flex-col gap-1 text-sm flex-1">
            Code
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newComponent.code} onChange={e => setNewComponent(c => ({ ...c, code: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm flex-1">
            Title
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newComponent.title} onChange={e => setNewComponent(c => ({ ...c, title: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm w-36">
            Concept ID
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newComponent.concept_id} onChange={e => setNewComponent(c => ({ ...c, concept_id: e.target.value }))} />
          </label>
          <button onClick={handleCreateComponent} className="h-8 rounded-md bg-primary px-3 text-sm text-primary-foreground hover:bg-primary/90">Create</button>
          <button onClick={() => setShowAddComponent(false)} className="h-8 rounded-md border border-input px-3 text-sm hover:bg-accent">Cancel</button>
        </div>
      )}

      {showAddClass && (
        <div className="mb-4 rounded-md border border-border bg-muted/30 p-4 flex items-end gap-3">
          <label className="flex flex-col gap-1 text-sm flex-1">
            Code
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newClass.code} onChange={e => setNewClass(c => ({ ...c, code: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm flex-1">
            Title
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newClass.title} onChange={e => setNewClass(c => ({ ...c, title: e.target.value }))} />
          </label>
          <label className="flex flex-col gap-1 text-sm w-36">
            Concept ID
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newClass.concept_id} onChange={e => setNewClass(c => ({ ...c, concept_id: e.target.value }))} />
          </label>
          <button onClick={handleCreateClass} className="h-8 rounded-md bg-primary px-3 text-sm text-primary-foreground hover:bg-primary/90">Create</button>
          <button onClick={() => setShowAddClass(false)} className="h-8 rounded-md border border-input px-3 text-sm hover:bg-accent">Cancel</button>
        </div>
      )}

      <div className="rounded-md border border-border">
        {components.length === 0 ? (
          <div className="p-4 text-sm text-muted-foreground text-center">No components found.</div>
        ) : (
          components.map((comp) => {
            const compClasses = comp.classes ?? [];
            const existingClassCodes = new Set(compClasses.map(c => c.code));
            const availableClasses = allClasses.filter(c => !existingClassCodes.has(c.code));

            return (
              <div key={comp.code} className="flex items-center justify-between px-4 py-2.5 border-b border-border last:border-b-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-medium text-sm">{comp.title}</span>
                  <span className="text-xs text-muted-foreground">({comp.code})</span>
                  {comp.concept_id && <span className="text-xs text-muted-foreground">HemOnc:{comp.concept_id}</span>}
                  {compClasses.map(cl => (
                    <span key={cl.code} className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-xs text-blue-700">
                      {cl.title}
                      <button
                        onClick={() => handleRemoveClass(comp.code, cl.code)}
                        className="hover:text-destructive"
                        aria-label={`Remove class ${cl.title}`}
                      >
                        <X size={11} />
                      </button>
                    </span>
                  ))}
                </div>
                {availableClasses.length > 0 && (
                  <select
                    className="h-7 rounded border border-input bg-background px-1.5 text-xs"
                    value=""
                    onChange={(e) => { if (e.target.value) handleAddClass(comp.code, e.target.value); }}
                  >
                    <option value="">+ Class</option>
                    {availableClasses.map(c => (
                      <option key={c.code} value={c.code}>{c.title}</option>
                    ))}
                  </select>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Disease-Round-Regimen Tab
// ---------------------------------------------------------------------------

function DiseaseRegimenTab() {
  const [items, setItems] = useState<DiseaseTherapyRegimenItem[]>([]);
  const [regimens, setRegimens] = useState<TherapyRegimenItem[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [newItem, setNewItem] = useState({ disease_concept_id: '', round_code: '', regimen_code: '' });
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [dtr, regs] = await Promise.all([
        fetchDiseaseTherapyRegimens(),
        regimens.length ? Promise.resolve(regimens) : fetchRegimens(),
      ]);
      setItems(dtr);
      if (!regimens.length) setRegimens(regs);
    } catch {
      setError('Failed to load disease-therapy data.');
    }
  }, [regimens]);

  useEffect(() => {
    (async () => {
      await loadData();
    })();
  }, [loadData]);

  const handleAdd = useCallback(async () => {
    if (!newItem.disease_concept_id || !newItem.round_code || !newItem.regimen_code) return;
    try {
      await addDiseaseTherapyRegimen({
        disease_concept_id: Number(newItem.disease_concept_id),
        round_code: newItem.round_code,
        regimen_code: newItem.regimen_code,
      });
      setNewItem({ disease_concept_id: '', round_code: '', regimen_code: '' });
      setShowAdd(false);
      await loadData();
    } catch {
      setError('Failed to add disease-therapy link.');
    }
  }, [newItem, loadData]);

  const handleRemove = useCallback(async (id: number) => {
    try {
      await removeDiseaseTherapyRegimen(id);
      await loadData();
    } catch {
      setError('Failed to remove disease-therapy link.');
    }
  }, [loadData]);

  return (
    <div>
      {error && (
        <div className="mb-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive flex items-center justify-between">
          {error}
          <button onClick={() => setError(null)}><X size={14} /></button>
        </div>
      )}

      <div className="mb-4 flex items-center gap-3">
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="flex items-center gap-1.5 rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
        >
          <Plus size={14} /> Add Association
        </button>
      </div>

      {showAdd && (
        <div className="mb-4 rounded-md border border-border bg-muted/30 p-4 flex items-end gap-3">
          <label className="flex flex-col gap-1 text-sm flex-1">
            Disease Concept ID
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newItem.disease_concept_id} onChange={e => setNewItem(n => ({ ...n, disease_concept_id: e.target.value }))} placeholder="e.g. 35918372" />
          </label>
          <label className="flex flex-col gap-1 text-sm flex-1">
            Round Code
            <input className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newItem.round_code} onChange={e => setNewItem(n => ({ ...n, round_code: e.target.value }))} placeholder="e.g. first_line_therapy" />
          </label>
          <label className="flex flex-col gap-1 text-sm flex-1">
            Regimen
            <select className="h-8 rounded-md border border-input bg-background px-2 text-sm" value={newItem.regimen_code} onChange={e => setNewItem(n => ({ ...n, regimen_code: e.target.value }))}>
              <option value="">Select regimen...</option>
              {regimens.map(r => (
                <option key={r.code} value={r.code}>{r.title} ({r.code})</option>
              ))}
            </select>
          </label>
          <button onClick={handleAdd} className="h-8 rounded-md bg-primary px-3 text-sm text-primary-foreground hover:bg-primary/90">Add</button>
          <button onClick={() => setShowAdd(false)} className="h-8 rounded-md border border-input px-3 text-sm hover:bg-accent">Cancel</button>
        </div>
      )}

      <div className="rounded-md border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/30">
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">Disease</th>
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">Round</th>
              <th className="px-4 py-2 text-left font-medium text-muted-foreground">Regimen</th>
              <th className="px-4 py-2 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr><td colSpan={4} className="px-4 py-4 text-center text-muted-foreground">No associations found.</td></tr>
            ) : (
              items.map((item) => (
                <tr key={item.id} className="border-b border-border last:border-b-0 hover:bg-muted/30">
                  <td className="px-4 py-2">
                    {item.disease_name ?? `Concept ${item.disease_concept_id}`}
                  </td>
                  <td className="px-4 py-2">
                    {item.round_title ?? item.round_code}
                  </td>
                  <td className="px-4 py-2">
                    {item.regimen_title ?? item.regimen_code}
                    {item.regimen_concept_id && (
                      <span className="text-xs text-muted-foreground ml-1">HemOnc:{item.regimen_concept_id}</span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => handleRemove(item.id)}
                      className="text-muted-foreground hover:text-destructive p-1"
                      aria-label="Remove association"
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function TherapyMappingPage() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<Tab>('regimen-component');

  const tabs: Array<{ key: Tab; label: string }> = [
    { key: 'regimen-component', label: 'Regimen \u2192 Components' },
    { key: 'component-class', label: 'Component \u2192 Classes' },
    { key: 'disease-regimen', label: 'Disease \u2192 Regimen' },
  ];

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-6 flex items-center gap-3">
        <button
          onClick={() => navigate('/mappings')}
          className="rounded p-1 text-muted-foreground hover:bg-accent"
          aria-label="Back to mappings"
        >
          <ArrowLeft size={20} />
        </button>
        <h1 className="text-2xl font-bold text-foreground">Therapy Mapping</h1>
      </div>

      <div className="mb-6 flex gap-1 border-b border-border">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground hover:border-muted-foreground/30'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'regimen-component' && <RegimenComponentTab />}
      {activeTab === 'component-class' && <ComponentClassTab />}
      {activeTab === 'disease-regimen' && <DiseaseRegimenTab />}
    </div>
  );
}
