import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { Button } from '@/components/shadcn/button';

type LegacyResult = {
  id: string;
  date: string;
  source_value: string | null;
  text: string | null;
  value_concept_name: string | null;
  state: 'recorded' | 'selection_cleared' | 'marked_in_error';
  unresolved_note: boolean;
};
type HistoryPage = { results: LegacyResult[]; next_cursor: string | null; legacy_summary?: { text: string; date: null } | null };
const stateLabels = {
  recorded: 'Recorded', selection_cleared: 'Historical selection cleared', marked_in_error: 'Marked in error',
};

export default function LegacyCytogeneticHistory({ personId }: { personId: number }) {
  const [rows, setRows] = useState<LegacyResult[]>([]);
  const [legacySummary, setLegacySummary] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string>();
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    clinicalClient().get<HistoryPage>(clinicalUrl(`/v1/patient-records/${personId}/genomics-legacy-cytogenetics/`), {
      params: { cursor },
    }).then(({ data }) => {
      if (current) {
        setRows(previous => cursor ? [...previous, ...data.results] : data.results);
        setNextCursor(data.next_cursor);
        if (!cursor) setLegacySummary(data.legacy_summary?.text ?? null);
      }
    }).catch(() => { if (current) setError(true); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [personId, cursor, retry]);

  return <details className="rounded-md border p-3" data-patient-field="cytogenetic_markers">
    <summary className="cursor-pointer font-medium">Legacy cytogenetic results</summary>
    <p className="my-3 text-sm text-muted-foreground">Compare these original results with current findings before adding another finding. A cleared selection records an editing action; it does not establish a negative test.</p>
    {legacySummary && <div className="my-3 text-sm"><p className="font-medium">Legacy summary — date unavailable</p><p className="whitespace-pre-wrap">{legacySummary}</p></div>}
    {rows.length > 0 && <table className="w-full text-left text-sm">
      <thead><tr><th className="p-2">Recorded date</th><th className="p-2">Original result</th><th className="p-2">Record status</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.id} className="border-t align-top">
        <td className="p-2 whitespace-nowrap">{row.date}</td>
        <td className="p-2 whitespace-pre-wrap break-words">{row.text || row.value_concept_name || row.source_value || 'No source text recorded.'}
          {row.unresolved_note && <p className="text-amber-700">Full source text is unavailable.</p>}
        </td>
        <td className="p-2">{stateLabels[row.state]}</td>
      </tr>)}</tbody>
    </table>}
    {loading && <p role="status">Loading legacy cytogenetic results…</p>}
    {error && <div role="alert">Could not load legacy cytogenetic results. <Button variant="outline" onClick={() => { setLoading(true); setError(false); setRetry(value => value + 1); }}>Retry legacy results</Button></div>}
    {!loading && !error && !legacySummary && rows.length === 0 && <p className="text-sm">No legacy cytogenetic results recorded.</p>}
    {!loading && !error && nextCursor && <Button variant="outline" onClick={() => { setLoading(true); setCursor(nextCursor); }}>Load earlier results</Button>}
  </details>;
}
