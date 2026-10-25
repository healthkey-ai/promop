import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import api from '@/api/axios';
import { destinationError } from './destinationSearch';
import EditMappingDialog from './EditMappingDialog';
import { AxisVariantFlag, AxisVariantList } from './LoincAxisChips';
import type { AxisVariant, LoincAxesPayload } from './loincAxes';
import { type CodeMappingRow, type ConceptResult, type Reference, DOMAIN_TO_TABLE, emptyReference } from './codeMappingTypes';

type Concept = {
  concept_id: number; concept_name: string; concept_code: string;
  vocabulary_id: string; domain_id: string;
  concept_class_id?: string; standard_concept?: string | null;
  fields: { field_name: string; status: string; omop_table: string }[];
  sccm_counts: { approved: number; proposed: number; rejected: number };
  /** Curator priority (1 = map first), or null when unranked (#1914). */
  priority?: number | null;
};
type Source = {
  mapping_id: number | null; source_code: string; source_code_description: string;
  source_vocabulary_id: string; occurrence_count: number; status: string;
  origin_system: string; updated_at: string; destination_concept_id: number | null;
  destination_concept_name: string; destination_standard_concept: string | null;
  evidence?: string[]; verdict?: string; note?: string;
  /** How many LOINC axis sets this code's units imply, when the server reports it. */
  axis_variant_count?: number;
  /** The variants themselves, each with its own (suggested) destination (#2012). */
  axis_variants?: AxisVariant[];
  /** The ranker's confidence (0-1) in this source → concept pairing. */
  confidence?: number | null;
  /** Saved in the full editor since the preview: no longer a bulk-review row. */
  edited?: boolean;
  /** The ranker answered "no suitable concept" for this source (#2034). */
  declined?: boolean;
};
type ConfidenceOrder = '' | '-confidence' | 'confidence';
type Page<T> = { results: T[]; page: number; page_size: number; total: number };
type Run = {
  run_id: string; state: string; error: string; done: number; total: number;
  selection: { limit: number; include_zero_seen: boolean; include_declined?: boolean; ranking_model?: string };
  activity: { concept: { concept_id: number }; candidates: Source[] }[];
};
/** How many concepts a page may show: more per page lets a curator drag a
 * concept from what would be a later page into the priority order. */
const PAGE_SIZES = [50, 100, 250, 500];
const domains = [
  ['', 'All domains'], ['measurement', 'Measurement'], ['observation', 'Observation'],
  ['condition', 'Condition'], ['drug_exposure', 'Drug'], ['procedure', 'Procedure'],
];
const button = 'rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40';
const input = 'rounded border border-slate-300 bg-white px-3 py-2 text-sm';
const sourceKey = (source: Source) => JSON.stringify([source.source_vocabulary_id, source.source_code]);
const percent = (value: number) => `${Math.round(value * 100)}%`;
// Unscored rows sort last in either direction, as the server orders them.
const byConfidence = (order: ConfidenceOrder) => (a: Source, b: Source) => {
  const left = a.confidence ?? null;
  const right = b.confidence ?? null;
  if (left === right) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return order === 'confidence' ? left - right : right - left;
};

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

type SourceMode = 'linked' | 'candidates';
/** A stored mapping's status; Existing mappings shows one group per status (#2010). */
type Status = 'approved' | 'proposed' | 'rejected';
const STATUSES: Status[] = ['approved', 'proposed', 'rejected'];
type SourcePage = Page<Source> & { zero_seen: number; status_counts?: Record<Status, number> };

export default function ConceptToCodeTab({ canApprove, onWritingChange, initialConceptId, onInitialConceptHandled }: {
  canApprove: boolean; onWritingChange?: (value: boolean) => void;
  /** Open this concept's source codes on arrival, ready to suggest more. */
  initialConceptId?: number;
  /** Called once the linked concept is opened or reported, so it is not reopened. */
  onInitialConceptHandled?: () => void;
}) {
  const [domain, setDomain] = useState('');
  const [scope, setScope] = useState('fields');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [rankedOnly, setRankedOnly] = useState(false);
  const [data, setData] = useState<(Page<Concept> & { prioritized?: number }) | null>(null);
  const [moving, setMoving] = useState(false);
  const [moveStatus, setMoveStatus] = useState('');
  const [dragged, setDragged] = useState<Concept | null>(null);
  const [dropTarget, setDropTarget] = useState<number | null>(null);
  const [selected, setSelected] = useState<Concept | null>(null);
  const selectedTrigger = useRef<HTMLButtonElement | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [writing, setWriting] = useState(false);
  // The Edit Mapping dialog sits inside this one; Escape must not close both.
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true); setError('');
      try {
        const response = await api.get<Page<Concept> & { prioritized?: number }>('/v1/concept-to-code/', {
          params: { domain, scope, search, page, page_size: pageSize, ...(rankedOnly ? { prioritized: '1' } : {}) },
          signal: controller.signal,
        });
        if (active) setData(response.data);
      } catch {
        if (active) setError('Could not load concepts. Change the search or refresh to retry.');
      } finally { if (active) setLoading(false); }
    }, 250);
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [domain, scope, search, page, pageSize, rankedOnly, revision]);

  // One concept per move: the server renumbers the global list, so a move
  // from a filtered or later page needs nothing else from it (#1914).
  const move = async (concept: Concept, position: number | null) => {
    if (moving || position === (concept.priority ?? null)) return;
    setMoving(true); setError('');
    try {
      const { data: placed } = await api.post<{ position: number | null }>('/v1/concept-to-code/priority/', {
        concept_id: concept.concept_id, position,
      });
      setMoveStatus(placed.position === null
        ? `Removed ${concept.concept_name} from the priority list.`
        : `Moved ${concept.concept_name} to priority ${placed.position}.`);
      setRevision(value => value + 1);
    } catch (failure) {
      setError(`Could not change the priority of ${concept.concept_name}. ${destinationError(failure)}`);
    } finally { setMoving(false); }
  };
  // Dropping onto a ranked concept takes its position; onto an unranked one,
  // the end of the priority list.
  const dropOn = (target: Concept) => {
    const source = dragged;
    setDragged(null); setDropTarget(null);
    if (!source || source.concept_id === target.concept_id) return;
    void move(source, target.priority ?? (data?.prioritized ?? 0) + 1);
  };
  const lastPosition = data?.prioritized ?? 0;

  useEffect(() => {
    if (!initialConceptId) return;
    let active = true;
    (async () => {
      try {
        // By id, not the list's text search: that matches the digits in names
        // and codes too, and the concept could sort past its first page.
        const { data: found } = await api.get<{ concept: Omit<Concept, 'fields' | 'sccm_counts'> }>(
          `/v1/concept-to-code/${initialConceptId}/`);
        if (!active) return;
        // The dialog shows neither; the list row carries them.
        setSelected({ ...found.concept, fields: [], sccm_counts: { approved: 0, proposed: 0, rejected: 0 } });
      } catch (failure) {
        if (!active) return;
        const notFound = (failure as { response?: { status?: number } })?.response?.status === 404;
        setError(notFound
          ? `Concept ${initialConceptId} is not a current standard concept in a clinical domain, so it has no source codes to review.`
          : `Could not open concept ${initialConceptId}. Reload the page to retry.`);
      }
      if (active) onInitialConceptHandled?.();
    })();
    return () => { active = false; };
    // The callback is a notification; a new identity must not reopen the concept.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialConceptId]);

  return <Dialog.Root open={selected !== null} onOpenChange={open => { if (!open && !writing && !editing) setSelected(null); }}>
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
      <select aria-label="Concepts per page" className={input} disabled={writing} value={pageSize}
        onChange={event => { setPageSize(Number(event.target.value)); setPage(1); }}>
        {PAGE_SIZES.map(size => <option key={size} value={size}>{size} per page</option>)}
      </select>
      <label className="flex items-center gap-2 text-sm text-slate-700">
        <input type="checkbox" checked={rankedOnly} disabled={writing}
          onChange={event => { setRankedOnly(event.target.checked); setPage(1); }} />
        Ranked only{data?.prioritized ? ` (${data.prioritized})` : ''}
      </label>
    </div>
    <p className="text-xs text-slate-500">Ranked concepts are mapped first and come first on every page. Drag a row, or type
      its priority (1 is first; clear it to unrank). To reach a concept on a later page, show more per page or search for it.</p>
    {moveStatus && <p role="status" aria-live="polite" className="text-sm text-slate-700">{moveStatus}</p>}
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {loading && <p role="status" className="text-sm text-slate-500">Loading concepts…</p>}
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table aria-label="Standard concept coverage" className="w-full text-left text-sm">
        <thead className="bg-slate-100 text-xs text-slate-600"><tr>
          <th className="p-3">Priority</th>
          <th className="p-3">Standard concept</th><th className="p-3">Patient fields</th>
          <th className="p-3 text-right">Approved</th><th className="p-3 text-right">Proposed</th><th className="p-3 text-right">Rejected</th>
        </tr></thead>
        <tbody>{data?.results.map(concept => <tr key={concept.concept_id} data-testid={`concept-row-${concept.concept_id}`}
          draggable={!writing && !moving}
          onDragStart={event => { event.dataTransfer?.setData('text/plain', String(concept.concept_id)); setDragged(concept); }}
          onDragEnd={() => { setDragged(null); setDropTarget(null); }}
          onDragOver={event => { if (dragged) { event.preventDefault(); setDropTarget(concept.concept_id); } }}
          onDragLeave={() => setDropTarget(previous => previous === concept.concept_id ? null : previous)}
          onDrop={event => { event.preventDefault(); dropOn(concept); }}
          className={`border-t border-slate-100 ${selected?.concept_id === concept.concept_id ? 'bg-sky-50' : ''} ${
            dropTarget === concept.concept_id && dragged?.concept_id !== concept.concept_id ? 'outline outline-2 outline-sky-500' : ''}`}>
          <td className="p-3 align-top"><PriorityCell concept={concept} lastPosition={lastPosition}
            disabled={writing || moving} onMove={position => void move(concept, position)} /></td>
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
      <Dialog.Content aria-modal="true" className="fixed inset-0 z-50 m-auto h-fit max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-7xl overflow-y-auto rounded-lg bg-white p-5 shadow-xl"
        onEscapeKeyDown={event => { if (writing || editing) event.preventDefault(); }}
        onInteractOutside={event => { if (writing || editing) event.preventDefault(); }}
        onCloseAutoFocus={event => { event.preventDefault(); selectedTrigger.current?.focus(); }}>
        <div className="mb-4 flex items-start justify-between gap-4 border-b border-slate-200 pb-3">
          <div>
            <Dialog.Title className="text-lg font-semibold">Source codes for {selected.concept_name}</Dialog.Title>
            <Dialog.Description className="text-sm text-slate-600">Standard {selected.vocabulary_id}:{selected.concept_code} · OMOP {selected.concept_id} · {selected.domain_id}</Dialog.Description>
          </div>
          <Dialog.Close className={button} disabled={writing}>Close</Dialog.Close>
        </div>
        <SourceCoverage key={selected.concept_id} concept={selected} canApprove={canApprove}
          onWriting={value => { setWriting(value); onWritingChange?.(value); }} onSaved={() => setRevision(value => value + 1)}
          onEditingChange={setEditing} />
      </Dialog.Content>
    </Dialog.Portal>}
  </Dialog.Root>;
}

/** Drag handle, priority number and one-step moves for one concept row. */
function PriorityCell({ concept, lastPosition, disabled, onMove }: {
  concept: Concept; lastPosition: number; disabled: boolean; onMove: (position: number | null) => void;
}) {
  const current = concept.priority ?? null;
  const [draft, setDraft] = useState(current === null ? '' : String(current));
  const [shown, setShown] = useState(current);
  // A reload renumbers rows; show the server's number unless one is being typed.
  if (shown !== current) { setShown(current); setDraft(current === null ? '' : String(current)); }
  const commit = () => {
    const text = draft.trim();
    if (!text) { if (current !== null) onMove(null); return; }
    const value = Number(text);
    if (!Number.isInteger(value) || value < 1) { setDraft(current === null ? '' : String(current)); return; }
    onMove(value);
  };
  const small = 'rounded border border-slate-300 px-1.5 text-xs hover:bg-slate-100 disabled:opacity-30';
  return <div className="flex items-center gap-1 whitespace-nowrap">
    <span aria-hidden="true" title="Drag to reorder" className={`select-none text-slate-400 ${disabled ? '' : 'cursor-grab'}`}>⠿</span>
    <input type="number" min={1} inputMode="numeric" aria-label={`Priority for ${concept.concept_name}`}
      className="w-14 rounded border border-slate-300 px-1.5 py-1 text-sm tabular-nums" placeholder="—" value={draft} disabled={disabled}
      onChange={event => setDraft(event.target.value)} onBlur={commit}
      onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); commit(); } }} />
    <button type="button" className={small} aria-label={`Move ${concept.concept_name} up`} title="Move up"
      disabled={disabled || current === 1} onClick={() => onMove(current === null ? lastPosition + 1 : current - 1)}>↑</button>
    <button type="button" className={small} aria-label={`Move ${concept.concept_name} down`} title="Move down"
      disabled={disabled || current === null || current >= lastPosition} onClick={() => current !== null && onMove(current + 1)}>↓</button>
  </div>;
}

const loadFailed = 'Could not load source codes.';
const statusLabel: Record<Status, string> = { approved: 'Approved mappings', proposed: 'Proposed mappings', rejected: 'Rejected mappings' };

type EditTarget = { mode: 'new' | 'edit'; row: CodeMappingRow; source: Source };
/** How many sources Suggest could find, by the same retrieval as a run.
 * ``approximate`` is set when the exact count ran out of time: ``earlier`` is
 * the last exact count for these settings, ``sample`` a scaled-up sample. */
type Estimate = {
  total: number | null; umls?: number; lexical?: number; capped?: boolean; partial?: boolean;
  approximate?: 'earlier' | 'sample' | null; timed_out: boolean;
  /** Sources this estimate skips because a ranker already declined them (#2034). */
  declined_skipped?: number;
};

/** "12 a ranker already declined are skipped." when earlier runs declined some of the pool. */
function skippedText(estimate: Estimate) {
  const skipped = estimate.declined_skipped ?? 0;
  return skipped > 0 ? ` ${skipped.toLocaleString()} a ranker already declined ${skipped === 1 ? 'is' : 'are'} skipped.` : '';
}

function estimateText(estimate: Estimate, limit: number) {
  if (estimate.total === null) return 'Could not estimate how many source codes Suggest could find: the search took too long.';
  const sampled = estimate.approximate === 'sample';
  if (estimate.total === 0) {
    return (sampled
      ? 'A sample found no candidate source codes; the full count took too long, so there may be a few.'
      : 'Suggest source codes would find no candidate source codes with these settings.') + skippedText(estimate);
  }
  const plus = estimate.capped ? '+' : '';
  const evidence = [estimate.lexical ? `${estimate.lexical.toLocaleString()}${plus} by name` : '',
    estimate.umls ? `${estimate.umls.toLocaleString()}${plus} by UMLS` : ''].filter(Boolean).join(', ');
  const runs = Number.isInteger(limit) && limit > 0 ? Math.ceil(estimate.total / limit) : null;
  return `${sampled ? 'Roughly' : 'About'} ${estimate.total.toLocaleString()}${plus} candidate source codes${evidence ? ` (${evidence})` : ''}`
    + (runs ? ` · about ${runs.toLocaleString()}${plus} ${runs === 1 && !plus ? 'run' : 'runs'} of ${limit}` : '') + '.'
    + skippedText(estimate)
    + (sampled ? ' Estimated from a sample: the full count took too long.' : '')
    + (estimate.approximate === 'earlier' ? ' From an earlier count: this one took too long, so codes handled since may still be counted.' : '')
    + (estimate.partial ? ` Vocabulary codes never seen were not ${sampled ? '' : 'all '}counted${sampled ? '' : ' in time'}.` : '');
}
type RankingModel = 'anthropic' | 'jev' | 'both';

/** A suggested source with no mapping row yet, shaped for the New Mapping form. */
function newMappingRow(source: Source, concept: Concept): CodeMappingRow {
  return {
    mapping_id: null, domain_id: concept.domain_id,
    source_vocabulary_id: source.source_vocabulary_id, source_code: source.source_code,
    source_code_description: source.source_code_description,
    destination_concept_id: null, destination_concept_name: '', destination_concept_code: '',
    destination_vocabulary_id: '', destination_concept_class_id: '',
    destination_omop_table: DOMAIN_TO_TABLE[concept.domain_id] ?? '',
    status: 'proposed', notes: '', origin: '', origin_system: '', suggest_strategy: '', umls_cui: '',
    created_by: '', occurrence_count: source.occurrence_count, destination_count: 0, has_mapping: false,
  };
}

/** One status group of a concept's stored mappings, fetched while it is open. */
function useSourceGroup(conceptId: number, status: Status, enabled: boolean, query: {
  search: string; seenOnly: boolean; order: ConfidenceOrder; revision: number;
}) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SourcePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const { search, seenOnly, order, revision } = query;
  useEffect(() => {
    if (!enabled) return;
    let current = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setLoading(true); setFailed(false);
      try {
        const response = await api.get<SourcePage>(`/v1/concept-to-code/${conceptId}/`, {
          params: { mode: 'linked', status, search, seen_only: seenOnly ? '1' : '0', page, ...(order ? { order } : {}) },
          signal: controller.signal,
        });
        if (current) setData(response.data);
      } catch {
        if (current) setFailed(true);
      } finally { if (current) setLoading(false); }
    }, 250);
    return () => { current = false; controller.abort(); window.clearTimeout(timer); };
  }, [conceptId, status, enabled, search, seenOnly, order, page, revision]);
  // Stale rows must not stay selectable while a changed query loads, and a
  // folded group's failure must not outlive the group.
  const reset = useCallback(() => { setData(null); setPage(1); setFailed(false); }, []);
  return { data, setData, page, setPage, loading, failed, reset };
}

function SourceCoverage({ concept, canApprove, onSaved, onWriting, onEditingChange }: {
  concept: Concept; canApprove: boolean; onSaved: () => void; onWriting: (value: boolean) => void;
  onEditingChange?: (value: boolean) => void;
}) {
  // Suggested source codes is the view a curator comes for, so it opens first (#2010).
  const [mode, setMode] = useState<SourceMode>('candidates');
  const [search, setSearch] = useState('');
  const [seenOnly, setSeenOnly] = useState(true);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  // Approved mappings are done and rejected ones are decided; both start folded
  // so the proposals waiting for review are what the curator sees first.
  const [open, setOpen] = useState<Record<Status, boolean>>({ approved: false, proposed: true, rejected: false });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [run, setRun] = useState<Run | null>(null);
  // Rows saved or approved while a run is still streaming, by source key. The
  // poll replaces the run wholesale, so edits live beside it and are laid over
  // its candidates on every render (#2028).
  const [patched, setPatched] = useState<Record<string, Partial<Source>>>({});
  const [starting, setStarting] = useState(false);
  const [ranker, setRanker] = useState('jev');
  const [limit, setLimit] = useState(25);
  // Retry the sources a ranker declined in earlier runs, instead of skipping them (#2034).
  const [retryDeclined, setRetryDeclined] = useState(false);
  const [strategies, setStrategies] = useState({ umls: true, lexical: true });
  const [writing, setWriting] = useState(false);
  const [progress, setProgress] = useState('');
  const [order, setOrder] = useState<ConfidenceOrder>('');
  const [threshold, setThreshold] = useState(80);
  const groupQuery = { search, seenOnly, order, revision };
  const approvedGroup = useSourceGroup(concept.concept_id, 'approved', mode === 'linked' && open.approved, groupQuery);
  const proposedGroup = useSourceGroup(concept.concept_id, 'proposed', mode === 'linked' && open.proposed, groupQuery);
  const rejectedGroup = useSourceGroup(concept.concept_id, 'rejected', mode === 'linked' && open.rejected, groupQuery);
  const groups = { approved: approvedGroup, proposed: proposedGroup, rejected: rejectedGroup };
  // A folded group's effect never runs again, so only the groups being shown
  // may contribute loading, failure or counts.
  const shownGroups = STATUSES.filter(status => mode === 'linked' && open[status]);
  const resetView = () => { setSelected(new Set()); STATUSES.forEach(status => groups[status].reset()); };
  const [edit, setEdit] = useState<EditTarget | null>(null);
  const [opening, setOpening] = useState(false);
  const [reference, setReference] = useState<Reference | null>(null);
  const [dialogStrategies, setDialogStrategies] = useState({ umls: true, lexical: true, vectors: true });
  const [dialogRanking, setDialogRanking] = useState<RankingModel>('jev');
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const destination = useMemo<ConceptResult>(() => ({
    concept_id: concept.concept_id, concept_name: concept.concept_name, concept_code: concept.concept_code,
    vocabulary_id: concept.vocabulary_id, domain_id: concept.domain_id,
    concept_class_id: concept.concept_class_id ?? '', standard_concept: concept.standard_concept ?? 'S',
  }), [concept]);
  const writingRef = useRef(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const running = starting || run?.state === 'queued' || run?.state === 'running';
  const finishedRun = run && !running ? run.run_id : null;

  // Re-estimated after every write: a proposed source leaves the pool, so the
  // next run reaches codes the last one did not show.
  const strategyKey = (['umls', 'lexical'] as const).filter(key => strategies[key]).join(',');
  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setEstimate(null);
      if (!strategyKey) return;
      try {
        const response = await api.get<Estimate>(`/v1/concept-to-code/${concept.concept_id}/estimate/`, {
          params: { strategies: strategyKey, include_zero_seen: seenOnly ? '0' : '1', include_declined: retryDeclined ? '1' : '0' },
          signal: controller.signal,
        });
        if (current) setEstimate(response.data);
      } catch { /* The estimate is a guide; Suggest works without it. */ }
    }, 250);
    return () => { current = false; controller.abort(); window.clearTimeout(timer); };
    // A finished run recorded declines, which the estimate now skips.
  }, [concept.concept_id, strategyKey, seenOnly, retryDeclined, revision, finishedRun]);

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

  const changeMode = (next: typeof mode) => { setMode(next); setSearch(''); resetView(); setError(''); };
  const toggleGroup = (status: Status) => {
    setOpen(previous => ({ ...previous, [status]: !previous[status] }));
    // Folding drops the rows, so reopening fetches them fresh instead of
    // showing what was there before later writes.
    groups[status].reset();
    setSelected(new Set());
  };
  const start = async () => {
    setStarting(true); setError(''); setProgress(''); setSelected(new Set()); setRun(null); setPatched({}); setMode('candidates');
    try {
      const { data: next } = await api.post<Run>('/v1/concept-to-code/suggest/', {
        concept_ids: [concept.concept_id], strategies: Object.keys(strategies).filter(key => strategies[key as keyof typeof strategies]),
        limit, include_zero_seen: !seenOnly, ranking_model: ranker, include_declined: retryDeclined,
      });
      if (active.current) setRun(next);
    } catch (failure) { if (active.current) setError(destinationError(failure)); }
    finally { if (active.current) setStarting(false); }
  };
  const streamed = run?.activity.find(event => event.concept.concept_id === concept.concept_id)?.candidates ?? [];
  const candidates = Object.keys(patched).length
    ? streamed.map(item => { const edit = patched[sourceKey(item)]; return edit ? { ...item, ...edit } : item; })
    : streamed;
  // A ranked run scores every source it considers a possible match; one it left
  // unscored is not a suggestion. An unranked run scores nothing, so it shows all.
  const ranked = Boolean(run?.selection.ranking_model && run.selection.ranking_model !== 'none');
  const scored = ranked ? candidates.filter(row => row.confidence != null) : candidates;
  const unscored = ranked ? candidates.filter(row => row.confidence == null) : [];
  // A decline is the ranker's answer; a candidate it could not rank (the
  // ranker was unavailable) is hidden too, but nothing was decided (#2034).
  const declinedHidden = unscored.filter(row => row.declined).length;
  const unrankedHidden = unscored.length - declinedHidden;
  const candidateRows = scored.filter(row => !seenOnly || row.occurrence_count > 0);
  const linkedRows = STATUSES.flatMap(status => open[status] ? groups[status].data?.results ?? [] : []);
  const rows = mode === 'candidates' ? (order ? [...candidateRows].sort(byConfidence(order)) : candidateRows) : linkedRows;
  const editable = rows.filter(row => !row.edited && (row.status === 'proposed' || (row.status === 'unmapped' && row.mapping_id === null)) && row.origin_system !== 'athena');
  const pending = editable.filter(row => selected.has(sourceKey(row)));
  // Every open group answers with the counts of all three, under the same filters.
  const statusCounts = shownGroups.map(status => groups[status].data?.status_counts).find(Boolean) ?? null;
  const zeroSeen = shownGroups.map(status => groups[status].data?.zero_seen).find(value => value !== undefined);
  const loading = shownGroups.some(status => groups[status].loading);
  const loadError = shownGroups.some(status => groups[status].failed);
  const validThreshold = Number.isFinite(threshold) && threshold >= 0 && threshold <= 100;
  // Compare the percentage the column shows: 0.55 * 100 is 55.00000000000001.
  const confident = validThreshold
    ? editable.filter(row => row.confidence != null && Math.round(row.confidence * 100) > threshold)
    : [];
  const selectConfident = () => setSelected(new Set(confident.map(sourceKey)));
  const changeOrder = () => {
    setOrder(previous => previous === '-confidence' ? 'confidence' : '-confidence');
    resetView();
  };
  // What a write changed on a row, laid over its copies wherever the dialog
  // shows it. Only the fields the write touched, so a running search's later
  // updates to the same candidate (its verdict, note, other variants) still
  // show; a full server row from the batch approve replaces the stored copy.
  const patchRow = (row: Source, updated: Partial<Source>, { replace = false } = {}) => {
    if (!active.current) return;
    STATUSES.forEach(status => groups[status].setData(previous => previous
      ? { ...previous, results: previous.results.map(item => sourceKey(item) === sourceKey(row)
        ? (replace ? updated as Source : { ...item, ...updated }) : item) } : null));
    setPatched(previous => ({ ...previous, [sourceKey(row)]: { ...previous[sourceKey(row)], ...updated } }));
  };

  // One click approves: a suggested source with no mapping yet is proposed from
  // its saved preview first, then approved on the revision that returned (#2010).
  const approve = async () => {
    if (writingRef.current || !pending.length || !canApprove) return;
    const batch = [...pending];
    writingRef.current = true; setWriting(true); onWriting(true); setError('');
    const failures: string[] = [];
    let saved = 0;
    // Rows written, approved or only proposed: each changes the counts and the estimate.
    let written = 0;
    try {
      for (const [index, row] of batch.entries()) {
        if (!active.current) break;
        setProgress(`Reviewing ${index + 1} of ${batch.length}…`);
        try {
          let current = row;
          if (current.mapping_id === null) {
            ({ data: current } = await api.post<Source>(`/v1/concept-to-code/${concept.concept_id}/mappings/`, {
              run_id: run?.run_id, source_vocabulary_id: row.source_vocabulary_id, source_code: row.source_code, status: 'proposed',
            }));
            written += 1;
            patchRow(row, current, { replace: true });
          }
          const { data: updated } = await api.post<Source>(`/v1/concept-to-code/${concept.concept_id}/mappings/${current.mapping_id}/`, {
            status: 'approved', expected_updated_at: current.updated_at,
          });
          saved += 1; written += 1;
          patchRow(row, updated, { replace: true });
        } catch (failure) { failures.push(`${row.source_code}: ${destinationError(failure)}`); }
      }
      if (active.current) {
        setProgress(`Approved ${saved} of ${batch.length} selected mappings.`);
        setSelected(new Set());
        if (failures.length) setError(failures.join(' '));
        if (written) { onSaved(); setRevision(value => value + 1); }
      }
    } finally { writingRef.current = false; if (active.current) setWriting(false); onWriting(false); }
  };

  // Approve one axis variant from the row: its stored rows in those units are
  // re-pointed, and the row shows the server's view of every variant (#2012).
  const approveVariant = async (row: Source, variant: AxisVariant) => {
    if (writingRef.current || opening || row.mapping_id === null || !variant.target_concept) return;
    // A write like any other here: one at a time, and the dialog cannot close under it.
    writingRef.current = true; setWriting(true); onWriting(true); setError('');
    try {
      const { data } = await api.post<LoincAxesPayload>(`/v1/code-mappings/${row.mapping_id}/axis-variants/`, {
        axis_key: variant.axis_key, target_concept_id: variant.target_concept.concept_id, approve: true,
      });
      patchRow(row, { axis_variants: data.variants });
      setProgress(`Approved the ${variant.units?.join(', ') || variant.axis_key} variant of ${row.source_code}.`);
    } catch (failure) {
      if (active.current) setError(`Could not approve the variant of ${row.source_code}. ${destinationError(failure)}`);
    } finally { writingRef.current = false; if (active.current) setWriting(false); onWriting(false); }
  };

  const openEditor = async (row: Source) => {
    if (writingRef.current || opening) return;
    setOpening(true); setError('');
    try {
      let current = reference;
      if (!current) {
        const { data: loaded } = await api.get<Reference>('/v1/code-mappings/reference/');
        current = { ...emptyReference, ...(loaded || {}) };
        if (active.current) setReference(current);
      }
      const target: EditTarget = row.mapping_id === null
        ? { mode: 'new', row: newMappingRow(row, concept), source: row }
        : { mode: 'edit', row: (await api.get<CodeMappingRow>(`/v1/code-mappings/${row.mapping_id}/`)).data, source: row };
      if (!active.current) return;
      if (ranker !== 'none') setDialogRanking(ranker as RankingModel);
      setEdit(target); onEditingChange?.(true);
    } catch {
      if (active.current) setError(`Could not open ${row.source_code} for editing. Click it again to retry.`);
    } finally { if (active.current) setOpening(false); }
  };
  const closeEditor = () => { setEdit(null); onEditingChange?.(false); };
  const editorSaved = (saved: CodeMappingRow) => {
    onSaved();
    setRevision(value => value + 1);
    // Stored rows reload with new revisions; a selection made over the old ones must not be approved.
    if (mode !== 'candidates') { setSelected(new Set()); return; }
    const key = edit ? sourceKey(edit.source) : '';
    setSelected(previous => { const next = new Set(previous); next.delete(key); return next; });
    if (edit) {
      patchRow(edit.source, {
        edited: true, mapping_id: saved.mapping_id, status: saved.status,
        destination_concept_id: saved.destination_concept_id, destination_concept_name: saved.destination_concept_name,
        destination_standard_concept: saved.standard_concept ?? null,
      });
    }
  };

  const editorDeleted = () => {
    onSaved(); setSelected(new Set()); setRevision(value => value + 1);
    if (edit) {
      patchRow(edit.source, {
        edited: false, mapping_id: null, status: 'unmapped',
        destination_concept_id: null, destination_concept_name: '', destination_standard_concept: null,
      });
    }
  };

  // The run's controls and the batch selection wait for a run to finish; a
  // write in progress blocks everything. Opening a code to edit it waits only
  // for a write, so a curator works through the list while Suggest searches (#2028).
  const busy = writing || running;
  const locked = writing || opening;
  const retry = () => { resetView(); setError(''); setRevision(value => value + 1); };
  return <section aria-label={`Source codes for ${concept.concept_name}`} className="space-y-4">
    <div role="group" aria-label="Source code views" className="flex flex-wrap gap-2">
      {([['linked', 'Existing mappings'], ['candidates', 'Suggested source codes']] as const).map(([key, label]) =>
        <button key={key} className={`${button} ${mode === key ? 'bg-slate-100 font-semibold' : ''}`} disabled={writing} aria-pressed={mode === key}
          onClick={() => changeMode(key)}>{label}</button>)}
    </div>
    {mode !== 'candidates' && <input className={`${input} w-full`} aria-label="Search source codes" placeholder="Source code, description or vocabulary"
      value={search} disabled={writing} onChange={event => { setSearch(event.target.value); resetView(); }} />}
    <div className="space-y-1">
      {/* The ranker, the limit and the button stay on one line: the button
          is read with the settings it runs (#2034). */}
      <div role="group" aria-label="Suggest settings" className="flex flex-wrap items-center gap-3">
        {(['umls', 'lexical'] as const).map(strategy => <label key={strategy} className="flex items-center gap-1 text-sm">
          <input type="checkbox" checked={strategies[strategy]} disabled={busy} onChange={event => setStrategies(previous => ({ ...previous, [strategy]: event.target.checked }))} />
          {strategy === 'umls' ? 'UMLS' : 'Names and synonyms'}</label>)}
        <label className="flex items-center gap-1 text-sm" title="Search the sources a ranker declined in earlier runs again, instead of skipping them">
          <input type="checkbox" checked={retryDeclined} disabled={busy} onChange={event => setRetryDeclined(event.target.checked)} />
          Retry declined</label>
        <span className="flex flex-nowrap items-center gap-3">
          <select aria-label="Source suggestion ranker" className={input} value={ranker} disabled={busy} onChange={event => setRanker(event.target.value)}>
            <option value="jev">Jev assistance</option><option value="anthropic">Anthropic assistance</option>
            <option value="both">Both rankers</option><option value="none">Curator review</option>
          </select>
          <input type="number" aria-label="Maximum source candidates" className={`${input} w-20`} value={limit} min={1} max={100} disabled={busy} onChange={event => setLimit(Number(event.target.value))} />
          <button className={`${button} whitespace-nowrap`} disabled={busy || (!strategies.umls && !strategies.lexical) || !Number.isInteger(limit) || limit < 1 || limit > 100}
            onClick={() => void start()}>{running ? 'Searching…' : 'Suggest source codes'}</button>
        </span>
      </div>
      {estimate && <p data-testid="source-estimate" className="text-xs text-slate-600">{estimateText(estimate, limit)}</p>}
    </div>
    <p className="text-xs text-slate-500">Review clinical meaning before selecting codes. Suggestions do not change mappings; approval updates the mapping and matching stored clinical rows.
      {' '}New vocabulary codes have Seen = 0; approving one proposes it from this preview first.</p>
    {run && mode === 'candidates' && <p role="status" className="text-sm text-slate-600">
      {running ? 'Searching local vocabulary evidence…' : `${candidates.length} candidates retrieved (limit ${run.selection.limit}).`}
      {!running && declinedHidden > 0 && ` ${declinedHidden} declined by the ranker and hidden${run.selection.include_declined ? '' : '; the next run moves on to other codes'}.`}
      {!running && unrankedHidden > 0 && ` ${unrankedHidden} could not be ranked (the ranker was unavailable) and ${unrankedHidden === 1 ? 'is' : 'are'} hidden.`}
      {run.error && ` ${run.error}`}
    </p>}
    {run && mode === 'candidates' && run.selection.include_zero_seen === seenOnly && <p className="text-xs text-amber-800">Run Suggest source codes again to search with the changed Seen filter.</p>}
    {(error || loadError) && <p role="alert" className="text-sm text-rose-700">{error || loadFailed}
      {loadError && <button type="button" className="ml-2 underline" onClick={retry}>Retry</button>}</p>}
    {progress && <p role="status" className="text-sm font-medium text-slate-800">{progress}</p>}
    {loading && <p role="status" className="text-sm text-slate-500">Loading source codes…</p>}
    {(canApprove || (mode === 'linked' && statusCounts)) && <div className="flex flex-wrap items-center gap-3">
      {canApprove && <button className="rounded bg-slate-950 px-3 py-1.5 text-sm text-white disabled:opacity-40" disabled={busy || !pending.length}
        onClick={() => void approve()}>Approve selected ({pending.length})</button>}
      {mode === 'linked' && statusCounts && <span className="text-xs text-slate-500">
        {STATUSES.reduce((sum, status) => sum + statusCounts[status], 0)} matching sources{seenOnly && zeroSeen !== undefined ? ` · ${zeroSeen} zero-Seen codes excluded` : ''}</span>}
    </div>}
    <div className="overflow-x-auto"><table aria-label="Source codes for selected concept" className="w-full text-left text-sm">
      <thead className="border-y bg-slate-50 text-xs text-slate-600"><tr>
        <th className="p-2"><input type="checkbox" aria-label="Select displayed proposed sources" disabled={busy || !editable.length}
          checked={editable.length > 0 && editable.every(row => selected.has(sourceKey(row)))}
          onChange={event => setSelected(new Set(event.target.checked ? editable.map(sourceKey) : []))} /></th>
        <th className="p-2"><div className="flex flex-wrap items-center gap-2">Source code
          <span className="flex items-center gap-1 whitespace-nowrap font-normal">
            <button type="button" className="rounded border border-slate-300 bg-white px-2 py-0.5 hover:bg-slate-100 disabled:opacity-40"
              title={confident.length ? `Select ${confident.length} source(s) above ${threshold}% confidence` : `No suggested source is above ${validThreshold ? threshold : '—'}% confidence`}
              disabled={busy || !confident.length} onClick={selectConfident}>Select</button>
            &gt;<input type="number" aria-label="Select sources with confidence above, percent" className="w-14 rounded border border-slate-300 bg-white px-1 py-0.5"
              min={0} max={100} value={Number.isNaN(threshold) ? '' : threshold} disabled={busy}
              onChange={event => setThreshold(event.target.valueAsNumber)} />% confidence</span></div></th>
        <th className="p-2">Description</th>
        <th className="p-2" aria-sort={order ? 'none' : 'descending'}><label className="mb-1 flex whitespace-nowrap items-center gap-1 font-normal">
          <input type="checkbox" aria-label="Only source codes with Seen greater than zero" checked={seenOnly} disabled={busy}
            onChange={event => { setSeenOnly(event.target.checked); resetView(); }} />&gt; 0 only</label>
          {order ? <button type="button" className="font-semibold hover:text-slate-950 disabled:opacity-40" disabled={busy} title="Order by Seen"
            onClick={() => { setOrder(''); resetView(); }}>Seen ↕</button> : 'Seen ↓'}</th>
        <th className="p-2">Current destination</th>
        <th className="p-2" aria-sort={order === '-confidence' ? 'descending' : order === 'confidence' ? 'ascending' : 'none'}>
          <button type="button" className="flex items-center gap-1 font-semibold hover:text-slate-950 disabled:opacity-40" disabled={busy}
            title="Order by confidence" onClick={changeOrder}>Status / evidence · Confidence
            <span aria-hidden="true">{order === '-confidence' ? '↓' : order === 'confidence' ? '↑' : '↕'}</span></button></th>
      </tr></thead>
      {mode === 'linked' ? STATUSES.map(status => {
        const group = groups[status];
        const count = statusCounts?.[status];
        return <tbody key={status} data-testid={`mapping-group-${status}`}>
          <tr className="border-b border-slate-100 bg-slate-50">
            <td colSpan={6} className="p-2">
              <button type="button" className="flex items-center gap-2 text-sm font-semibold text-slate-800 disabled:opacity-40"
                aria-expanded={open[status]} disabled={writing} onClick={() => toggleGroup(status)}>
                <span aria-hidden="true">{open[status] ? '▾' : '▸'}</span>
                {statusLabel[status]}{count !== undefined ? ` (${count})` : ''}
              </button>
            </td>
          </tr>
          {open[status] && group.data?.results.map(renderRow)}
          {open[status] && group.data && !group.data.results.length && <tr><td colSpan={6} className="p-2 text-sm text-slate-500">
            No {status} mappings{search ? ' match this search' : ''}{seenOnly ? '; uncheck > 0 only to include zero-Seen codes' : ''}.</td></tr>}
          {open[status] && group.data && <tr><td colSpan={6} className="p-0">
            <Pagination data={group.data} disabled={writing || group.loading} change={next => { group.setPage(next); group.setData(null); setSelected(new Set()); }} /></td></tr>}
        </tbody>;
      }) : <tbody>{rows.map(renderRow)}</tbody>}
    </table></div>
    {mode === 'candidates' && !loading && !rows.length && <p className="text-sm text-slate-500">No source codes shown. {seenOnly ? 'Uncheck > 0 only to include zero-Seen codes. ' : ''}
      Run Suggest source codes to search.</p>}
    <EditMappingDialog open={edit !== null} onClose={closeEditor} mode={edit?.mode ?? 'edit'} row={edit?.row ?? null}
      reference={reference ?? emptyReference} canApprove={canApprove}
      strategies={dialogStrategies} rankingModel={dialogRanking}
      onStrategiesChange={setDialogStrategies} onRankingModelChange={setDialogRanking}
      onSaved={editorSaved} onDeleted={editorDeleted}
      defaultDestination={destination} sourceExamples />
  </section>;

  // A plain render function, not a nested component: a component declared here
  // would be a new type every render and remount each row.
  function renderRow(row: Source) {
    return <tr key={sourceKey(row)} className="cursor-pointer border-b border-slate-100 align-top hover:bg-slate-50"
        onClick={event => {
          // The checkbox and the code button act on their own.
          if ((event.target as HTMLElement).closest('input, button, a, select, label')) return;
          void openEditor(row);
        }}>
        <td className="p-2"><input type="checkbox" aria-label={`Select ${row.source_vocabulary_id || 'Uncoded'} ${row.source_code}`}
          disabled={busy || !editable.includes(row)} checked={selected.has(sourceKey(row))}
          onChange={event => setSelected(previous => { const next = new Set(previous); if (event.target.checked) next.add(sourceKey(row)); else next.delete(sourceKey(row)); return next; })} /></td>
        <td className="p-2 font-mono text-xs"><button type="button" className="text-left underline underline-offset-2 hover:text-slate-950 disabled:opacity-40"
          disabled={locked} aria-haspopup="dialog" title={`Open ${row.source_code} in Edit Mapping`}
          onClick={() => void openEditor(row)}>{row.source_vocabulary_id || 'Uncoded'}:{row.source_code}</button><AxisVariantFlag count={row.axis_variant_count} />
          <AxisVariantList variants={row.axis_variants} canApprove={canApprove} busy={locked} onApprove={variant => void approveVariant(row, variant)} /></td>
        <td className="p-2">{row.source_code_description || '—'}</td><td className="p-2 text-right tabular-nums">{row.occurrence_count}</td>
        <td className="p-2 text-xs">{row.destination_concept_name || 'None'}{row.destination_concept_id && <div className="mt-1 text-slate-500">
          OMOP {row.destination_concept_id} · {row.destination_standard_concept === 'S' ? 'Standard' : 'Nonstandard'}</div>}</td>
        <td className="p-2 text-xs"><span className={row.status === 'approved' ? 'font-medium text-green-800' : 'text-slate-700'}>{row.status === 'unmapped' ? 'Not yet mapped' : row.status}</span>
          {!!row.evidence?.length && <div className="mt-1 text-slate-500">{row.evidence.join(' + ')}</div>}
          {(row.status === 'proposed' || mode === 'candidates') && <div className="mt-1 text-slate-700">
            Confidence: {row.confidence != null ? <span className="font-medium tabular-nums">{percent(row.confidence)}</span> : <span className="text-slate-500">not scored</span>}</div>}
          {row.verdict === 'supported' && <div className="mt-1 text-sky-800">Model-supported; curator review required</div>}
          {row.note && <p className="mt-1 max-w-xs text-slate-500">{row.note}</p>}
          {row.edited && <div className="mt-1 text-slate-700">Saved in the editor</div>}</td>
      </tr>;
  }
}
