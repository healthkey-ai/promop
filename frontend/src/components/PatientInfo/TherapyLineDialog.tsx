import { useCallback, useEffect, useRef, useState } from 'react';
import { Search, X, Plus, Trash2 } from 'lucide-react';
import {
  searchDrugConcepts, authorTherapyLine, updateTherapyLine, THERAPY_OUTCOME_CHOICES,
  searchTherapyRegimens, getTherapyRegimenDetail,
  type DrugConcept, type EditableTherapyLine,
} from '@/api/therapyLines';
import type { TherapyRegimen } from '@/types/therapy';

interface Props {
  personId: number;
  /** Next line number, prefilled. The clinician can correct it. */
  defaultLineNumber: number;
  /** Existing line to edit. When present, the dialog PATCHes the line episode. */
  line?: EditableTherapyLine;
  /** Disease code for filtering regimens to this patient's disease. */
  diseaseCode?: string;
  onClose: () => void;
  /** Receives the re-derived record so the tab updates without a refetch. */
  onAuthored: (patientInfo: Record<string, unknown>) => void;
}

type SelectedDrug = DrugConcept & { source_value?: string | null; class_names?: string[] };

/**
 * Record a line of therapy.
 *
 * The treatment tab shows therapy fields it cannot edit, because none of them is
 * a column: a line is a set of drug exposures grouped by an Episode, and
 * `first_line_therapy`, `therapy_lines_count`, the outcomes and
 * `treatment_refractory_status` are inferred back out of that grouping. This is
 * the write that moves them.
 *
 * It asks for what a clinician knows — which line, which drugs, which dates,
 * how it went — and the server turns that into the CDM shape. Nothing here
 * carries a concept id for an episode, a type concept or a primary key.
 */
export default function TherapyLineDialog({
  personId,
  defaultLineNumber,
  line,
  diseaseCode,
  onClose,
  onAuthored,
}: Props) {
  const episodeId = typeof line?.episode_id === 'number' ? line.episode_id : null;
  const editing = episodeId !== null;
  const [lineNumber, setLineNumber] = useState(String(line?.line ?? defaultLineNumber));
  const [startDate, setStartDate] = useState(line?.start_date ?? '');
  const [endDate, setEndDate] = useState(line?.end_date ?? '');
  const [outcome, setOutcome] = useState(line?.outcome ?? '');
  const [drugs, setDrugs] = useState<SelectedDrug[]>(line?.drugs ?? []);

  const [query, setQuery] = useState('');
  const [results, setResults] = useState<DrugConcept[]>([]);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Regimen picker state
  const [regimenQuery, setRegimenQuery] = useState('');
  const [regimenResults, setRegimenResults] = useState<TherapyRegimen[]>([]);
  const [regimenSearching, setRegimenSearching] = useState(false);
  const [selectedRegimen, setSelectedRegimen] = useState<TherapyRegimen | null>(null);
  const [loadingRegimen, setLoadingRegimen] = useState(false);
  const regimenDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const doRegimenSearch = useCallback(async (q: string) => {
    if (q.trim().length < 2) {
      setRegimenResults([]);
      return;
    }
    setRegimenSearching(true);
    try {
      setRegimenResults(await searchTherapyRegimens(q, diseaseCode));
    } catch {
      setRegimenResults([]);
    } finally {
      setRegimenSearching(false);
    }
  }, [diseaseCode]);

  const selectRegimen = useCallback(async (regimen: TherapyRegimen) => {
    setRegimenQuery('');
    setRegimenResults([]);
    setSelectedRegimen(regimen);
    setLoadingRegimen(true);
    try {
      const detail = await getTherapyRegimenDetail(regimen.code);
      // Map components to DrugConcept entries for the drug list
      const componentDrugs: SelectedDrug[] = (detail.components ?? [])
        .filter((c) => c.concept_id)
        .map((c) => ({
          concept_id: c.concept_id!,
          concept_name: c.concept_name ?? c.title,
          concept_code: c.concept_code ?? c.code,
          vocabulary_id: c.vocabulary_id ?? 'HemOnc',
          class_names: (c.classes ?? []).map((cl) => cl.title),
        }));
      if (componentDrugs.length > 0) {
        setDrugs(componentDrugs);
      }
    } catch {
      // Detail fetch failed — regimen is still selected but drugs not auto-populated
    } finally {
      setLoadingRegimen(false);
    }
  }, []);

  const clearRegimen = useCallback(() => {
    setSelectedRegimen(null);
    setRegimenQuery('');
    setRegimenResults([]);
  }, []);

  const doSearch = useCallback(async (q: string) => {
    if (q.trim().length < 3) {
      setResults([]);
      return;
    }
    setSearching(true);
    try {
      setResults(await searchDrugConcepts(q));
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => { doSearch(query); }, 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [query, doSearch]);

  useEffect(() => {
    if (regimenDebounceRef.current) clearTimeout(regimenDebounceRef.current);
    regimenDebounceRef.current = setTimeout(() => { doRegimenSearch(regimenQuery); }, 300);
    return () => { if (regimenDebounceRef.current) clearTimeout(regimenDebounceRef.current); };
  }, [regimenQuery, doRegimenSearch]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  const addDrug = (drug: DrugConcept) => {
    // Adding the same ingredient twice writes one exposure either way, since the
    // server keys on (source_value, start_date) — but showing it twice would
    // suggest a dose the form never captured.
    setDrugs((prev) =>
      prev.some((d) => d.concept_id === drug.concept_id) ? prev : [...prev, drug],
    );
    setQuery('');
    setResults([]);
  };

  const submit = async () => {
    setError('');
    if (drugs.length === 0) {
      setError('Add at least one drug. A line with no drugs groups nothing, so no therapy field would follow from it.');
      return;
    }
    setSaving(true);
    try {
      const payload = {
        person: personId,
        line_number: Number(lineNumber),
        start_date: startDate || null,
        end_date: endDate || null,
        outcome: outcome || null,
        regimen_concept_id: selectedRegimen?.concept_id ?? null,
        drugs: drugs.map((d) => ({
          concept_id: d.concept_id,
          source_value: (d.source_value || d.concept_name).slice(0, 50),
        })),
      };
      const result = episodeId !== null
        ? await updateTherapyLine(episodeId, payload)
        : await authorTherapyLine(payload);
      onAuthored(result.patient_info);
      onClose();
    } catch (err: unknown) {
      // The server's refusals are specific — an unknown drug concept, a line
      // with nothing in it, a missing Treatment Regimen concept — and each says
      // what to do. Replacing them with "save failed" would throw that away.
      let detail = editing
        ? 'Could not update the therapy line.'
        : 'Could not record the therapy line.';
      if (err && typeof err === 'object' && 'response' in err) {
        const data = (err as { response?: { data?: Record<string, unknown> } })
          .response?.data;
        if (typeof data?.detail === 'string') detail = data.detail;
        else if (data) {
          const first = Object.entries(data)
            .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : String(v)}`)
            .join('; ');
          if (first) detail = first;
        }
      }
      setError(detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={editing ? 'Edit a line of therapy' : 'Record a line of therapy'}
    >
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-border bg-background p-5 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">
            {editing ? 'Edit line of therapy' : 'Record a line of therapy'}
          </h2>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X size={18} />
          </button>
        </div>

        <p className="mb-4 text-xs text-muted-foreground">
          The therapy fields on this tab are derived from the lines on record.
          {editing
            ? 'Changing one here updates the drug exposures and episode grouping, then re-derives the record.'
            : 'Adding one here writes the drug exposures and the episode that groups them, then re-derives the record.'}
        </p>

        <div className="grid grid-cols-2 gap-4">
          <label className="text-sm">
            <span className="mb-1 block font-medium">Line number</span>
            <input
              type="number" min={1} value={lineNumber}
              onChange={(e) => setLineNumber(e.target.value)}
              disabled={editing}
              className="w-full rounded-md border border-input px-2 py-1.5 text-sm"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block font-medium">Outcome</span>
            <select
              value={outcome} onChange={(e) => setOutcome(e.target.value)}
              className="w-full rounded-md border border-input px-2 py-1.5 text-sm"
            >
              <option value="">—</option>
              {THERAPY_OUTCOME_CHOICES.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block font-medium">Start date</span>
            <input
              type="date" value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              className="w-full rounded-md border border-input px-2 py-1.5 text-sm"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block font-medium">End date</span>
            <input
              type="date" value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              className="w-full rounded-md border border-input px-2 py-1.5 text-sm"
            />
          </label>
        </div>

        <div className="mt-5">
          <span className="mb-1 block text-sm font-medium">Regimen (optional)</span>
          {selectedRegimen ? (
            <div className="mb-3 flex items-center justify-between rounded border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
              <span>
                {selectedRegimen.title}
                {selectedRegimen.concept_id && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    HemOnc {selectedRegimen.concept_id}
                  </span>
                )}
              </span>
              <button
                onClick={clearRegimen}
                aria-label="Clear regimen"
                className="text-muted-foreground hover:text-foreground"
              >
                <X size={14} />
              </button>
            </div>
          ) : (
            <div className="mb-3">
              <div className="relative">
                <Search size={14} className="absolute left-2.5 top-2.5 text-muted-foreground" />
                <input
                  type="text"
                  value={regimenQuery}
                  onChange={(e) => setRegimenQuery(e.target.value)}
                  placeholder="Search regimens (e.g. R-CHOP)…"
                  aria-label="Search regimens"
                  className="w-full rounded-md border border-input py-1.5 pl-8 pr-3 text-sm"
                />
              </div>
              {regimenSearching && <p className="mt-1 text-xs text-muted-foreground">Searching…</p>}
              {loadingRegimen && <p className="mt-1 text-xs text-muted-foreground">Loading regimen components…</p>}
              {regimenResults.length > 0 && (
                <ul className="mt-1 max-h-40 overflow-y-auto rounded border border-border">
                  {regimenResults.map((r) => (
                    <li key={r.code}>
                      <button
                        onClick={() => selectRegimen(r)}
                        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted"
                      >
                        <Plus size={12} className="shrink-0 text-muted-foreground" />
                        <span>{r.title}</span>
                        {r.concept_id && (
                          <span className="ml-auto text-xs text-muted-foreground">
                            {r.concept_id}
                          </span>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="mt-5">
          <span className="mb-1 block text-sm font-medium">Drugs in this line</span>
          {drugs.length > 0 && (
            <ul className="mb-2 space-y-1">
              {drugs.map((d) => (
                <li key={d.concept_id} className="flex items-center justify-between rounded border border-border px-2 py-1 text-sm">
                  <div className="min-w-0 flex-1">
                    <span>
                      {d.concept_name}
                      <span className="ml-2 text-xs text-muted-foreground">
                        {d.vocabulary_id} {d.concept_code}
                      </span>
                    </span>
                    {d.class_names && d.class_names.length > 0 && (
                      <p className="text-xs text-muted-foreground truncate">
                        {d.class_names.join(', ')}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={() => setDrugs((prev) => prev.filter((x) => x.concept_id !== d.concept_id))}
                    aria-label={`Remove ${d.concept_name}`}
                    className="ml-2 shrink-0 text-muted-foreground hover:text-red-600"
                  >
                    <Trash2 size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-2.5 text-muted-foreground" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search drugs (3+ characters)…"
              aria-label="Search drugs"
              className="w-full rounded-md border border-input py-1.5 pl-8 pr-3 text-sm"
            />
          </div>
          {searching && <p className="mt-1 text-xs text-muted-foreground">Searching…</p>}
          {results.length > 0 && (
            <ul className="mt-1 max-h-40 overflow-y-auto rounded border border-border">
              {results.map((r) => (
                <li key={r.concept_id}>
                  <button
                    onClick={() => addDrug(r)}
                    className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted"
                  >
                    <Plus size={12} className="shrink-0 text-muted-foreground" />
                    <span>{r.concept_name}</span>
                    <span className="ml-auto text-xs text-muted-foreground">{r.concept_code}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && (
          <p className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-sm">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {saving
              ? (editing ? 'Updating…' : 'Recording…')
              : (editing ? 'Update line' : 'Record line')}
          </button>
        </div>
      </div>
    </div>
  );
}
