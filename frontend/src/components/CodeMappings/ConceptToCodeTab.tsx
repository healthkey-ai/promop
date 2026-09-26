import { useCallback, useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import api from '@/api/axios';
import { destinationError } from './destinationSearch';

type Concept = {
  concept_id: number; concept_name: string; concept_code: string;
  vocabulary_id: string; domain_id: string;
  fields: { field_name: string; status: string; omop_table: string }[];
  sccm_counts: { approved: number; proposed: number; rejected: number };
};
type Source = {
  mapping_id: number; source_code: string; source_code_description: string;
  source_vocabulary_id: string; occurrence_count: number; status: string;
  origin_system: string; updated_at: string; destination_concept_id: number | null;
  destination_concept_name: string; destination_standard_concept: string | null;
  evidence?: string[]; verdict?: string; note?: string;
};
type Page<T> = { results: T[]; page: number; page_size: number; total: number };
type Run = {
  run_id: string; state: string; error: string; done: number; total: number;
  selection: { limit: number; include_zero_seen: boolean };
  activity: { concept: { concept_id: number }; candidates: Source[] }[];
};
const domains = [
  ['', 'All domains'], ['measurement', 'Measurement'], ['observation', 'Observation'],
  ['condition', 'Condition'], ['drug_exposure', 'Drug'], ['procedure', 'Procedure'],
];
const button = 'rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40';
const input = 'rounded border border-slate-300 bg-white px-3 py-2 text-sm';

function Pagination({ data, change, disabled = false }: {
  data: Page<unknown>; change: (page: number) => void; disabled?: boolean;
}) {
  if (data.total <= data.page_size) return null;
  return <nav aria-label="Result pages" className="flex items-center justify-end gap-3 py-3 text-sm">
    <button className={button} disabled={disabled || data.page === 1} onClick={() => change(data.page - 1)}>Previous</button>
    <span>Page {data.page} of {Math.ceil(data.total / data.page_size)}</span>
    <button className={button} disabled={disabled || data.page * data.page_size >= data.total} onClick={() => change(data.page + 1)}>Next</button>
  </nav>;
}

export default function ConceptToCodeTab({ canApprove, onWritingChange }: { canApprove: boolean; onWritingChange?: (value: boolean) => void }) {
  const [domain, setDomain] = useState('');
  const [scope, setScope] = useState('fields');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page<Concept> | null>(null);
  const [selected, setSelected] = useState<Concept | null>(null);
  const selectedTrigger = useRef<HTMLButtonElement | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [writing, setWriting] = useState(false);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true); setError('');
      try {
        const response = await api.get<Page<Concept>>('/v1/concept-to-code/', {
          params: { domain, scope, search, page }, signal: controller.signal,
        });
        if (active) setData(response.data);
      } catch {
        if (active) setError('Could not load concepts. Change the search or refresh to retry.');
      } finally { if (active) setLoading(false); }
    }, 250);
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [domain, scope, search, page, revision]);

  return <Dialog.Root open={selected !== null} onOpenChange={open => { if (!open && !writing) setSelected(null); }}>
    <section aria-label="Concept to source code" className="space-y-5">
    <div>
      <h2 className="text-lg font-semibold text-slate-950">Source-code coverage</h2>
      <p className="mt-1 text-sm text-slate-600">Choose a standard concept, then review the source codes that should map to it.</p>
    </div>
    <div className="flex flex-wrap gap-3">
      <input className={`${input} min-w-64 flex-1`} aria-label="Search standard concepts" placeholder="Concept name, code, OMOP ID or patient field"
        value={search} disabled={writing} onChange={event => { setSearch(event.target.value); setPage(1); }} />
      <select aria-label="Concept scope" className={input} disabled={writing} value={scope}
        onChange={event => { setScope(event.target.value); setPage(1); }}>
        <option value="fields">Patient field concepts</option><option value="all">All standard concepts</option>
      </select>
      <select aria-label="Concept domain" className={input} disabled={writing} value={domain}
        onChange={event => { setDomain(event.target.value); setPage(1); }}>
        {domains.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
    </div>
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {loading && <p role="status" className="text-sm text-slate-500">Loading concepts…</p>}
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table aria-label="Standard concept coverage" className="w-full text-left text-sm">
        <thead className="bg-slate-100 text-xs text-slate-600"><tr>
          <th className="p-3">Standard concept</th><th className="p-3">Patient fields</th>
          <th className="p-3 text-right">Approved</th><th className="p-3 text-right">Proposed</th><th className="p-3 text-right">Rejected</th>
        </tr></thead>
        <tbody>{data?.results.map(concept => <tr key={concept.concept_id}
          className={`border-t border-slate-100 ${selected?.concept_id === concept.concept_id ? 'bg-sky-50' : ''}`}>
          <td className="p-3"><button className="text-left font-medium text-slate-950 underline underline-offset-2" disabled={writing}
            aria-haspopup="dialog" aria-expanded={selected?.concept_id === concept.concept_id}
            onClick={event => { selectedTrigger.current = event.currentTarget; setSelected(concept); }}>{concept.concept_name}</button>
            <div className="mt-1 text-xs text-slate-500">{concept.vocabulary_id}:{concept.concept_code} · OMOP {concept.concept_id} · {concept.domain_id}</div></td>
          <td className="p-3 text-xs text-slate-600">{concept.fields.length
            ? concept.fields.map(field => <div key={field.field_name}>{field.field_name} <span className="text-slate-400">({field.status})</span></div>) : '—'}</td>
          {(['approved', 'proposed', 'rejected'] as const).map(status => <td key={status} className="p-3 text-right tabular-nums">{concept.sccm_counts[status]}</td>)}
        </tr>)}</tbody>
      </table>
      {data && !data.results.length && <p className="p-5 text-sm text-slate-500">No matching standard concepts. Try another search or All standard concepts.</p>}
    </div>
    <p className="text-xs text-slate-500">Coverage counts include all linked source codes, including Seen = 0.</p>
    {data && <Pagination data={data} disabled={loading || writing} change={setPage} />}
    </section>
    {selected && <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-slate-950/50" />
      <Dialog.Content aria-modal="true" className="fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-7xl -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg bg-white p-5 shadow-xl"
        onEscapeKeyDown={event => { if (writing) event.preventDefault(); }}
        onInteractOutside={event => { if (writing) event.preventDefault(); }}
        onCloseAutoFocus={event => { event.preventDefault(); selectedTrigger.current?.focus(); }}>
        <div className="mb-4 flex items-start justify-between gap-4 border-b border-slate-200 pb-3">
          <div>
            <Dialog.Title className="text-lg font-semibold">Source codes for {selected.concept_name}</Dialog.Title>
            <Dialog.Description className="text-sm text-slate-600">Standard {selected.vocabulary_id}:{selected.concept_code} · OMOP {selected.concept_id} · {selected.domain_id}</Dialog.Description>
          </div>
          <Dialog.Close className={button} disabled={writing}>Close</Dialog.Close>
        </div>
        <SourceCoverage key={selected.concept_id} concept={selected} canApprove={canApprove}
          onWriting={value => { setWriting(value); onWritingChange?.(value); }} onSaved={() => setRevision(value => value + 1)} />
      </Dialog.Content>
    </Dialog.Portal>}
  </Dialog.Root>;
}

function SourceCoverage({ concept, canApprove, onSaved, onWriting }: {
  concept: Concept; canApprove: boolean; onSaved: () => void; onWriting: (value: boolean) => void;
}) {
  const [mode, setMode] = useState<'linked' | 'available' | 'candidates'>('linked');
  const [search, setSearch] = useState('');
  const [seenOnly, setSeenOnly] = useState(true);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<(Page<Source> & { zero_seen: number }) | null>(null);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [run, setRun] = useState<Run | null>(null);
  const [starting, setStarting] = useState(false);
  const [ranker, setRanker] = useState('none');
  const [limit, setLimit] = useState(25);
  const [strategies, setStrategies] = useState({ umls: true, lexical: true });
  const [writing, setWriting] = useState(false);
  const [progress, setProgress] = useState('');
  const writingRef = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const running = starting || run?.state === 'queued' || run?.state === 'running';

  useEffect(() => {
    if (mode === 'candidates') return;
    let current = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const response = await api.get<Page<Source> & { zero_seen: number }>(`/v1/concept-to-code/${concept.concept_id}/`, {
          params: { mode, search, seen_only: seenOnly ? '1' : '0', page }, signal: controller.signal,
        });
        if (current) { setData(response.data); setSelected(new Set()); }
      } catch {
        if (current) setError('Could not load source codes. Refresh to retry.');
      } finally { if (current) setLoading(false); }
    }, 250);
    return () => { current = false; controller.abort(); window.clearTimeout(timer); };
  }, [concept.concept_id, mode, search, seenOnly, page, revision]);

  useEffect(() => {
    if (!run || !['queued', 'running'].includes(run.state)) return;
    let current = true;
    const timer = window.setTimeout(async () => {
      try {
        const { data: next } = await api.get<Run>(`/v1/concept-to-code/suggest-runs/${run.run_id}/`);
        if (current) setRun(next);
      } catch {
        if (current) { setError('Could not read search progress. Run the search again to retry.'); setRun(previous => previous ? { ...previous, state: 'failure' } : null); }
      }
    }, 1500);
    return () => { current = false; window.clearTimeout(timer); };
  }, [run]);

  const changeMode = (next: typeof mode) => { setMode(next); setSelected(new Set()); setPage(1); setSearch(''); setData(null); setError(''); };
  const start = async () => {
    setStarting(true); setError(''); setProgress(''); setSelected(new Set()); setRun(null); setMode('candidates');
    try {
      const { data: next } = await api.post<Run>('/v1/concept-to-code/suggest/', {
        concept_ids: [concept.concept_id], strategies: Object.keys(strategies).filter(key => strategies[key as keyof typeof strategies]),
        limit, include_zero_seen: !seenOnly, ranking_model: ranker,
      });
      if (active.current) setRun(next);
    } catch (failure) { if (active.current) setError(destinationError(failure)); }
    finally { if (active.current) setStarting(false); }
  };
  const candidates = run?.activity.find(event => event.concept.concept_id === concept.concept_id)?.candidates ?? [];
  const rows = mode === 'candidates' ? candidates.filter(row => !seenOnly || row.occurrence_count > 0) : data?.results ?? [];
  const editable = rows.filter(row => row.status === 'proposed' && row.origin_system !== 'athena');
  const pending = editable.filter(row => selected.has(row.mapping_id));

  const save = async (approve: boolean) => {
    if (writingRef.current || !pending.length || (approve && !canApprove)) return;
    const batch = [...pending];
    writingRef.current = true; setWriting(true); onWriting(true); setError('');
    const failures: string[] = [];
    let saved = 0;
    try {
      for (const [index, row] of batch.entries()) {
        if (!active.current) break;
        setProgress(`Reviewing ${index + 1} of ${batch.length}…`);
        try {
          const { data: updated } = await api.post<Source>(`/v1/concept-to-code/${concept.concept_id}/mappings/${row.mapping_id}/`, {
            status: approve ? 'approved' : 'proposed', expected_updated_at: row.updated_at,
          });
          saved += 1;
          if (active.current) {
            setData(previous => previous ? { ...previous, results: previous.results.map(item => item.mapping_id === row.mapping_id ? updated : item) } : null);
            setRun(previous => previous ? { ...previous, activity: previous.activity.map(event => ({ ...event,
              candidates: event.candidates.map(item => item.mapping_id === row.mapping_id ? { ...item, ...updated } : item),
            })) } : null);
          }
        } catch (failure) { failures.push(`${row.source_code}: ${destinationError(failure)}`); }
      }
      if (active.current) {
        setProgress(`${approve ? 'Approved' : 'Proposed'} ${saved} of ${batch.length} selected mappings.`);
        setSelected(new Set());
        if (failures.length) setError(failures.join(' '));
        if (saved) { onSaved(); if (mode !== 'candidates') setRevision(value => value + 1); }
      }
    } finally { writingRef.current = false; if (active.current) setWriting(false); onWriting(false); }
  };

  const busy = writing || running;
  const refresh = useCallback(() => { setData(null); setSelected(new Set()); setError(''); setRevision(value => value + 1); }, []);
  return <section aria-label={`Source codes for ${concept.concept_name}`} className="space-y-4">
    <div role="group" aria-label="Source code views" className="flex flex-wrap gap-2">
      {([['linked', 'Existing mappings'], ['available', 'Find source codes'], ['candidates', 'Suggested source codes']] as const).map(([key, label]) =>
        <button key={key} className={`${button} ${mode === key ? 'bg-slate-100 font-semibold' : ''}`} disabled={busy} aria-pressed={mode === key}
          onClick={() => changeMode(key)}>{label}</button>)}
    </div>
    <div className="flex flex-wrap items-center gap-3">
      {mode !== 'candidates' && <input className={`${input} flex-1`} aria-label="Search source codes" placeholder="Source code, description or vocabulary"
        value={search} disabled={busy} onChange={event => { setSearch(event.target.value); setPage(1); setSelected(new Set()); setData(null); }} />}
      {(['umls', 'lexical'] as const).map(strategy => <label key={strategy} className="flex items-center gap-1 text-sm">
        <input type="checkbox" checked={strategies[strategy]} disabled={busy} onChange={event => setStrategies(previous => ({ ...previous, [strategy]: event.target.checked }))} />
        {strategy === 'umls' ? 'UMLS' : 'Names and synonyms'}</label>)}
      <select aria-label="Source suggestion ranker" className={input} value={ranker} disabled={busy} onChange={event => setRanker(event.target.value)}>
        <option value="none">Curator review</option><option value="anthropic">Anthropic assistance</option>
        <option value="jev">Jev assistance</option><option value="both">Both rankers</option>
      </select>
      <input type="number" aria-label="Maximum source candidates" className={`${input} w-20`} value={limit} min={1} max={100} disabled={busy} onChange={event => setLimit(Number(event.target.value))} />
      <button className={button} disabled={busy || (!strategies.umls && !strategies.lexical) || !Number.isInteger(limit) || limit < 1 || limit > 100}
        onClick={() => void start()}>{running ? 'Searching…' : 'Suggest source codes'}</button>
      {mode !== 'candidates' && <button className={button} disabled={busy} onClick={refresh}>Refresh</button>}
    </div>
    <p className="text-xs text-slate-500">Review clinical meaning before selecting codes. Suggestions do not change mappings; approval updates the mapping and matching stored clinical rows.</p>
    {run && mode === 'candidates' && <p role="status" className="text-sm text-slate-600">
      {running ? 'Searching local vocabulary evidence…' : `${candidates.length} candidates retrieved (limit ${run.selection.limit}).`}
      {run.error && ` ${run.error}`}
    </p>}
    {run && mode === 'candidates' && run.selection.include_zero_seen === seenOnly && <p className="text-xs text-amber-800">Run Suggest source codes again to search with the changed Seen filter.</p>}
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {progress && <p role="status" className="text-sm font-medium text-slate-800">{progress}</p>}
    {loading && <p role="status" className="text-sm text-slate-500">Loading source codes…</p>}
    <div className="flex flex-wrap items-center gap-3">
      <button className={button} disabled={busy || !pending.length} onClick={() => void save(false)}>Propose selected ({pending.length})</button>
      {canApprove && <button className="rounded bg-slate-950 px-3 py-1.5 text-sm text-white disabled:opacity-40" disabled={busy || !pending.length}
        onClick={() => void save(true)}>Approve selected ({pending.length})</button>}
      {mode !== 'candidates' && data && <span className="text-xs text-slate-500">{data.total} matching sources{seenOnly ? ` · ${data.zero_seen} zero-Seen codes excluded` : ''}</span>}
    </div>
    <div className="overflow-x-auto"><table aria-label="Source codes for selected concept" className="w-full text-left text-sm">
      <thead className="border-y bg-slate-50 text-xs text-slate-600"><tr>
        <th className="p-2"><input type="checkbox" aria-label="Select displayed proposed sources" disabled={busy || !editable.length}
          checked={editable.length > 0 && editable.every(row => selected.has(row.mapping_id))}
          onChange={event => setSelected(new Set(event.target.checked ? editable.map(row => row.mapping_id) : []))} /></th>
        <th className="p-2">Source code</th><th className="p-2">Description</th>
        <th className="p-2" aria-sort="descending"><label className="mb-1 flex whitespace-nowrap items-center gap-1 font-normal">
          <input type="checkbox" aria-label="Only source codes with Seen greater than zero" checked={seenOnly} disabled={busy}
            onChange={event => { setSeenOnly(event.target.checked); setPage(1); setData(null); setSelected(new Set()); }} />&gt; 0 only</label>Seen ↓</th>
        <th className="p-2">Current destination</th><th className="p-2">Status / evidence</th>
      </tr></thead>
      <tbody>{rows.map(row => <tr key={row.mapping_id} className="border-b border-slate-100 align-top">
        <td className="p-2"><input type="checkbox" aria-label={`Select ${row.source_vocabulary_id || 'Uncoded'} ${row.source_code}`}
          disabled={busy || row.status !== 'proposed' || row.origin_system === 'athena'} checked={selected.has(row.mapping_id)}
          onChange={event => setSelected(previous => { const next = new Set(previous); if (event.target.checked) next.add(row.mapping_id); else next.delete(row.mapping_id); return next; })} /></td>
        <td className="p-2 font-mono text-xs">{row.source_vocabulary_id || 'Uncoded'}:{row.source_code}</td>
        <td className="p-2">{row.source_code_description || '—'}</td><td className="p-2 text-right tabular-nums">{row.occurrence_count}</td>
        <td className="p-2 text-xs">{row.destination_concept_name || 'None'}{row.destination_concept_id && <div className="mt-1 text-slate-500">
          OMOP {row.destination_concept_id} · {row.destination_standard_concept === 'S' ? 'Standard' : 'Nonstandard'}</div>}</td>
        <td className="p-2 text-xs"><span className={row.status === 'approved' ? 'font-medium text-green-800' : 'text-slate-700'}>{row.status}</span>
          {!!row.evidence?.length && <div className="mt-1 text-slate-500">{row.evidence.join(' + ')}</div>}
          {row.verdict === 'supported' && <div className="mt-1 text-sky-800">Model-supported; curator review required</div>}
          {row.note && <p className="mt-1 max-w-xs text-slate-500">{row.note}</p>}</td>
      </tr>)}</tbody>
    </table></div>
    {!loading && !rows.length && <p className="text-sm text-slate-500">No source codes shown. {seenOnly ? 'Uncheck > 0 only to include zero-Seen codes. ' : ''}
      {mode === 'candidates' ? 'Run Suggest source codes to search.' : mode === 'linked' ? 'Use Find source codes or Suggest source codes to extend coverage.' : 'Try a different source search.'}</p>}
    {mode !== 'candidates' && data && <Pagination data={data} disabled={busy || loading} change={next => { setPage(next); setData(null); setSelected(new Set()); }} />}
  </section>;
}
