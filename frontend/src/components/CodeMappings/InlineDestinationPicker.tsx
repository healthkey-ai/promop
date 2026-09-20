import { useEffect, useId, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import api from '@/api/axios';
import ConceptInputDetails from '@/components/UI/ConceptInputDetails';
import { destinationError, saveMappingDestination, searchDestinationConcepts, type DestinationConcept, type SavedMapping } from './destinationSearch';

type Props = {
  mappingId: number;
  sourceLabel: string;
  vocabularies?: { vocabulary_id: string; vocabulary_name: string }[];
  initialVocabulary?: string;
  canApprove?: boolean;
  onSaved: (mapping: SavedMapping, concept: DestinationConcept) => void;
  onCancel: () => void;
};

export default function InlineDestinationPicker({ mappingId, sourceLabel, vocabularies = [], initialVocabulary = '', canApprove = false, onSaved, onCancel }: Props) {
  const listId = useId();
  const [query, setQuery] = useState('');
  const [vocabulary, setVocabulary] = useState(initialVocabulary);
  const [results, setResults] = useState<DestinationConcept[]>([]);
  const [selected, setSelected] = useState<DestinationConcept | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [current, setCurrent] = useState<SavedMapping | null>(null);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const writing = useRef(false);

  useEffect(() => {
    let active = true;
    api.get<SavedMapping>(`/v1/code-mappings/${mappingId}/`)
      .then(({ data }) => { if (active) setCurrent(data); })
      .catch(() => { if (active) setError('Could not load the current mapping. Close and try again.'); });
    return () => { active = false; };
  }, [mappingId]);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setResults([]);
    setActiveIndex(-1);
    if (query.trim().length < 3 || selected) {
      setSearching(false);
      return () => { active = false; controller.abort(); };
    }
    setSearching(true);
    const timer = window.setTimeout(async () => {
      try {
        const matches = await searchDestinationConcepts(query, vocabulary, controller.signal);
        if (active) setResults(matches);
      } catch {
        if (active) setError('Could not search destinations. Change the search to try again.');
      } finally {
        if (active) setSearching(false);
      }
    }, 250);
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [query, vocabulary, selected]);

  const choose = (concept: DestinationConcept) => {
    setSelected(concept);
    setResults([]);
    setError('');
  };
  const save = async (approve: boolean) => {
    if (!selected || !current || writing.current) return;
    writing.current = true;
    setSaving(true);
    setError('');
    try {
      const saved = await saveMappingDestination(mappingId, selected.concept_id, approve, current.destination_concept_id);
      onSaved(saved, selected);
    } catch (failure) {
      setError(destinationError(failure));
    } finally {
      writing.current = false;
      setSaving(false);
    }
  };
  const readOnly = current?.status === 'approved' || current?.mapping_origin === 'athena';

  return <section aria-label={`Choose destination for ${sourceLabel}`} className="space-y-3 rounded-md border border-slate-300 bg-slate-50 p-4 text-left text-sm">
    <div className="flex items-start justify-between gap-3">
      <div>
        <h4 className="font-semibold text-slate-950">Choose a destination</h4>
        <p className="mt-0.5 text-xs text-slate-600">{sourceLabel}</p>
      </div>
      <button type="button" aria-label="Close destination picker" onClick={onCancel} disabled={saving} className="rounded p-1 text-slate-500 hover:bg-slate-200 disabled:opacity-50"><X size={16} /></button>
    </div>
    {error && <p role="alert" className="text-rose-700">{error}</p>}
    {readOnly ? <p role="status">This mapping is already approved. Open the full editor to review it.</p> : <>
      <div className="flex flex-wrap gap-2">
        <label className="relative min-w-60 flex-1">
          <span className="sr-only">Search destination concepts inline</span>
          <Search size={15} className="pointer-events-none absolute left-3 top-3 text-slate-400" />
          <input autoFocus role="combobox" aria-autocomplete="list" aria-expanded={results.length > 0} aria-controls={listId}
            aria-activedescendant={activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
            value={query} disabled={saving} placeholder="Search by name, code or OMOP ID"
            className="h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-3 outline-none focus:border-slate-700"
            onChange={event => { setQuery(event.target.value); setSelected(null); setError(''); }}
            onKeyDown={event => {
              if (event.key === 'ArrowDown' && results.length) { event.preventDefault(); setActiveIndex(index => (index + 1) % results.length); }
              if (event.key === 'ArrowUp' && results.length) { event.preventDefault(); setActiveIndex(index => (index <= 0 ? results.length : index) - 1); }
              if (event.key === 'Enter') { event.preventDefault(); if (activeIndex >= 0 && results[activeIndex]) choose(results[activeIndex]); }
              if (event.key === 'Escape' && !saving) { event.preventDefault(); onCancel(); }
            }} />
        </label>
        {vocabularies.length > 0 && <select aria-label="Inline search vocabulary" value={vocabulary} disabled={saving}
          onChange={event => { setVocabulary(event.target.value); setSelected(null); setError(''); }}
          className="h-10 max-w-64 rounded-md border border-slate-300 bg-white px-3">
          <option value="">All vocabularies</option>
          {vocabularies.map(item => <option key={item.vocabulary_id} value={item.vocabulary_id}>{item.vocabulary_name || item.vocabulary_id}</option>)}
        </select>}
      </div>
      <p className="text-xs text-slate-500">Active, standard concepts and HealthKey concepts. Type at least 3 characters.</p>
      {searching && <p role="status" className="text-slate-600">Searching destinations…</p>}
      {!searching && query.trim().length >= 3 && !selected && !results.length && !error && <p role="status">No matching destinations. Try another name or code.</p>}
      {results.length > 0 && <ul id={listId} role="listbox" aria-label="Destination search results" className="max-h-64 overflow-y-auto rounded border border-slate-200 bg-white">
        {results.map((concept, index) => <li key={concept.concept_id} role="none">
          <button type="button" role="option" id={`${listId}-${index}`} aria-selected={activeIndex === index}
            onClick={() => choose(concept)} className={`block w-full border-b border-slate-100 px-3 py-2 text-left hover:bg-slate-100 ${activeIndex === index ? 'bg-slate-100' : ''}`}>
            <span className="font-medium text-slate-950">{concept.concept_name}</span>
            <span className="mt-0.5 block text-xs text-slate-500">{concept.vocabulary_id}:{concept.concept_code} · OMOP {concept.concept_id}</span>
            {(concept.measurement_type || concept.suggested_unit) && <ConceptInputDetails domain_id={concept.domain_id} measurement_type={concept.measurement_type} suggested_unit={concept.suggested_unit} />}
          </button>
        </li>)}
      </ul>}
      {selected && <div className="rounded border border-sky-200 bg-sky-50 px-3 py-2" role="status">
        <p className="font-medium text-slate-950">Selected: {selected.concept_name}</p>
        <p className="text-xs text-slate-600">{selected.vocabulary_id}:{selected.concept_code} · OMOP {selected.concept_id}</p>
      </div>}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" disabled={!selected || !current || saving} onClick={() => void save(false)} className="rounded-md bg-slate-950 px-3 py-2 font-medium text-white hover:bg-slate-800 disabled:opacity-40">{saving ? 'Saving…' : 'Save choice'}</button>
        {canApprove && <button type="button" disabled={!selected || !current || saving} onClick={() => void save(true)} className="rounded-md border border-slate-300 bg-white px-3 py-2 font-medium hover:bg-slate-100 disabled:opacity-40">Save & approve</button>}
        <button type="button" disabled={saving} onClick={onCancel} className="rounded-md px-3 py-2 text-slate-600 hover:bg-slate-200 disabled:opacity-40">Cancel</button>
        <span className="text-xs text-slate-500">Save choice keeps the mapping proposed for review.</span>
      </div>
    </>}
  </section>;
}
