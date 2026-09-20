import { useEffect, useState } from 'react';
import api from '@/api/axios';
import { INPUT_CLASS } from '@/components/UI/MappingFormPrimitives';

export interface SourceTerm {
  vocabulary_id: string;
  code: string;
  name: string;
  definition: string;
  synonyms: string[];
  parents: string[];
  semantic_types: string[];
  status: string;
  retired: boolean;
  release_version: string;
  source_url: string;
  metadata?: {
    registry_numbers?: string[];
    pharmacological_actions?: { code: string; name: string }[];
    mapped_headings?: { code: string; name: string }[];
  };
}
interface CatalogResponse {
  available: boolean;
  term: SourceTerm | null;
  results: SourceTerm[];
  attribution?: string;
}

interface Props {
  vocabularyId: string;
  code: string;
  onSelect: (term: SourceTerm) => void;
}

export default function SourceVocabularyLookup(props: Props) {
  return <Lookup key={props.vocabularyId} {...props} />;
}

function Lookup({ vocabularyId, code, onSelect }: Props) {
  const [query, setQuery] = useState('');
  const [response, setResponse] = useState<{ key: string; data: CatalogResponse } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const requestKey = JSON.stringify([vocabularyId, code, query]);

  useEffect(() => {
    if (!vocabularyId) return;
    let current = true;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const { data } = await api.get<CatalogResponse>('/v1/code-mappings/source-catalog/', {
          params: { vocabulary_id: vocabularyId, code, ...(query.trim().length >= 2 ? { q: query.trim() } : {}) },
          signal: controller.signal,
        });
        if (current) setResponse({ key: requestKey, data });
      } catch {
        if (current) setError({ key: requestKey, message: 'Could not load source terminology.' });
      }
    }, 250);
    return () => { current = false; clearTimeout(timer); controller.abort(); };
  }, [vocabularyId, code, query, requestKey]);

  if (error?.key === requestKey) return <p role="status" className="text-sm text-slate-600">{error.message}</p>;
  if (!response?.data.available) return null;
  const loading = response.key !== requestKey;
  const results = loading ? [] : response.data.results;
  const term = response.data.term?.code === code ? response.data.term : null;
  return <div className="space-y-3 md:col-span-2">
    <label htmlFor="source-vocabulary-search" className="block text-sm font-medium">
      Search {vocabularyId} source codes
    </label>
    <input id="source-vocabulary-search" className={INPUT_CLASS} value={query}
      placeholder="Code, name or synonym" onChange={event => setQuery(event.target.value)}
      onKeyDown={event => { if (event.key === 'Enter') event.preventDefault(); }} />
    {query.trim().length >= 2 && <ul aria-label="Source vocabulary results" className="max-h-60 overflow-auto rounded border border-slate-200">
      {results.map(result => <li key={result.code}>
        <button type="button" className="w-full px-3 py-2 text-left text-sm hover:bg-sky-50"
          onClick={() => { onSelect(result); setQuery(''); }}>
          <span className="font-mono">{result.code}</span> — {result.name}
        </button>
      </li>)}
      {loading && <li className="px-3 py-2 text-sm text-slate-600">Searching source codes…</li>}
      {!loading && !results.length && <li className="px-3 py-2 text-sm text-slate-600">No matching source codes.</li>}
    </ul>}
    {term && <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-3 text-sm" aria-label="Source vocabulary metadata">
      <p className="font-semibold">{term.name} <span className="font-mono font-normal">({term.code})</span></p>
      {term.retired && <p role="status" className="font-semibold text-red-700">This source code is retired or obsolete.</p>}
      {term.definition && <p>{term.definition}</p>}
      {term.synonyms.length > 0 && <p><span className="font-medium">Synonyms: </span>{term.synonyms.join('; ')}</p>}
      {term.semantic_types.length > 0 && <p><span className="font-medium">Semantic types: </span>{term.semantic_types.join('; ')}</p>}
      {term.parents.length > 0 && <p><span className="font-medium">Parent codes: </span>{term.parents.join(', ')}</p>}
      {!!term.metadata?.registry_numbers?.length && <p><span className="font-medium">Registry identifiers: </span>{term.metadata.registry_numbers.join('; ')}</p>}
      {!!term.metadata?.pharmacological_actions?.length && <p><span className="font-medium">Pharmacological actions: </span>{term.metadata.pharmacological_actions.map(a => a.name).join('; ')}</p>}
      {!!term.metadata?.mapped_headings?.length && <p><span className="font-medium">MeSH headings: </span>{term.metadata.mapped_headings.map(h => `${h.name} (${h.code})`).join('; ')}</p>}
      <p className="text-xs text-slate-500">{term.vocabulary_id} release {term.release_version}{term.status ? ` · ${term.status}` : ''}</p>
    </div>}
    {response.data.attribution && <p className="text-xs text-slate-500">{response.data.attribution}</p>}
  </div>;
}
