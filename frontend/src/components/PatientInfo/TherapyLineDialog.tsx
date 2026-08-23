import { useCallback, useEffect, useRef, useState } from 'react';
import { Search, X, Plus, Trash2 } from 'lucide-react';
import {
  searchDrugConcepts, authorTherapyLine, THERAPY_OUTCOME_CHOICES,
  type DrugConcept,
} from '@/api/therapyLines';

interface Props {
  personId: number;
  /** Next line number, prefilled. The clinician can correct it. */
  defaultLineNumber: number;
  onClose: () => void;
  /** Receives the re-derived record so the tab updates without a refetch. */
  onAuthored: (patientInfo: Record<string, unknown>) => void;
}

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
  onClose,
  onAuthored,
}: Props) {
  const [lineNumber, setLineNumber] = useState(String(defaultLineNumber));
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [outcome, setOutcome] = useState('');
  const [drugs, setDrugs] = useState<DrugConcept[]>([]);

  const [query, setQuery] = useState('');
  const [results, setResults] = useState<DrugConcept[]>([]);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
      const result = await authorTherapyLine({
        person: personId,
        line_number: Number(lineNumber),
        start_date: startDate || null,
        end_date: endDate || null,
        outcome: outcome || null,
        drugs: drugs.map((d) => ({
          concept_id: d.concept_id,
          source_value: d.concept_name.slice(0, 50),
        })),
      });
      onAuthored(result.patient_info);
      onClose();
    } catch (err: unknown) {
      // The server's refusals are specific — an unknown drug concept, a line
      // with nothing in it, a missing Treatment Regimen concept — and each says
      // what to do. Replacing them with "save failed" would throw that away.
      let detail = 'Could not record the therapy line.';
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
      aria-label="Record a line of therapy"
    >
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-border bg-background p-5 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">Record a line of therapy</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X size={18} />
          </button>
        </div>

        <p className="mb-4 text-xs text-muted-foreground">
          The therapy fields on this tab are derived from the lines on record.
          Adding one here writes the drug exposures and the episode that groups
          them, then re-derives the record.
        </p>

        <div className="grid grid-cols-2 gap-4">
          <label className="text-sm">
            <span className="mb-1 block font-medium">Line number</span>
            <input
              type="number" min={1} value={lineNumber}
              onChange={(e) => setLineNumber(e.target.value)}
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
          <span className="mb-1 block text-sm font-medium">Drugs in this line</span>
          {drugs.length > 0 && (
            <ul className="mb-2 space-y-1">
              {drugs.map((d) => (
                <li key={d.concept_id} className="flex items-center justify-between rounded border border-border px-2 py-1 text-sm">
                  <span>
                    {d.concept_name}
                    <span className="ml-2 text-xs text-muted-foreground">
                      RxNorm {d.concept_code}
                    </span>
                  </span>
                  <button
                    onClick={() => setDrugs((prev) => prev.filter((x) => x.concept_id !== d.concept_id))}
                    aria-label={`Remove ${d.concept_name}`}
                    className="text-muted-foreground hover:text-red-600"
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
            {saving ? 'Recording…' : 'Record line'}
          </button>
        </div>
      </div>
    </div>
  );
}
