import { useCallback, useEffect, useState } from 'react';
import api from '@/api/axios';

/**
 * The axis variant walk a curator can ask for after loading patients (#2050).
 *
 * The walk rewrites every mapped code's unit evidence from the loaded patients
 * and refreshes its axis variants (#2043). It runs on the worker and takes hours
 * on a large instance, so the panel shows the last walk, how many patients were
 * loaded since it began, progress while one runs, and the axis evidence the last
 * walk counted (#2051): whether anything beyond units ever settles an axis.
 */

export type AxisEvidenceCounts = { settled: number; hint: number; varies: number; bases: Record<string, number> };
export type AxisEvidence = { codes: number; axes: Record<string, AxisEvidenceCounts> };
export type Walk = {
  id: number; outcome: 'queued' | 'running' | 'done' | 'failed'; reason: string; rebuild_evidence: boolean;
  keys: number; done: number; refreshed: number; failure_reason: string;
  created_at: string; started_at: string | null; progress_at: string | null; completed_at: string | null;
  axis_evidence: AxisEvidence | null;
};
export type WalkStatus = {
  active: Walk | null; last: Walk | null;
  /** The last completed rebuild: what the evidence table and the patient count are measured from. */
  completed: Walk | null;
  /** Not counted while a walk is active: the panel shows progress then. */
  patients_since: number | null; walked_at: string | null;
};

export const WALK_URL = '/v1/concept-to-code/axis-variant-walk/';
/** How often the panel re-reads a queued or running walk. */
export const POLL_MS = 10_000;

const AXIS_LABELS: [string, string][] = [
  ['component', 'Component'], ['property', 'Property'], ['time_aspect', 'Time'],
  ['system', 'System'], ['scale_type', 'Scale'], ['method_type', 'Method'],
];
const BASIS_LABELS: Record<string, string> = {
  curator: 'curator', source_code: 'LOINC source code', fhir_coding: 'LOINC coding',
  unit: 'unit', value_type: 'quantitative value', specimen: 'specimen',
};

const button = 'rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40';

function when(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function isStatus(data: unknown): data is WalkStatus {
  return typeof data === 'object' && data !== null && 'patients_since' in data;
}

export default function AxisVariantWalkPanel({ canApprove }: { canApprove: boolean }) {
  const [status, setStatus] = useState<WalkStatus | null>(null);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get<WalkStatus>(WALK_URL);
      if (isStatus(data)) { setStatus(data); setError(''); }
    } catch {
      setError('Could not read the source-code walk.');
    }
  }, []);

  useEffect(() => {
    (async () => { await load(); })();
  }, [load]);

  const active = status?.active ?? null;
  // Keyed on the walk's id, not the object: every poll returns a fresh one.
  const activeId = active?.id ?? null;
  useEffect(() => {
    if (activeId === null) return;
    const timer = setInterval(() => { void load(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [activeId, load]);

  const start = async () => {
    setStarting(true);
    setError('');
    try {
      const { data } = await api.post<WalkStatus & { created: boolean }>(WALK_URL);
      if (isStatus(data)) setStatus(data);
    } catch (failure) {
      const response = (failure as { response?: { status?: number; data?: { detail?: string } } })?.response;
      if (response?.status === 409 && response.data && isStatus(response.data)) setStatus(response.data);
      setError(response?.status === 409 && response.data?.detail ? response.data.detail : 'Could not start the walk.');
    } finally {
      setStarting(false);
    }
  };

  const last = status?.last ?? null;
  const completed = status?.completed ?? null;
  const evidence = completed?.axis_evidence ?? null;
  const patients = status?.patients_since ?? 0;

  return <section aria-label="Source-code walk" className="rounded border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">
    {!status && !error && <p role="status">Reading the source-code walk…</p>}
    {error && <p role="alert" className="text-rose-700">{error}</p>}
    {status && <>
      {active
        ? <p role="status" aria-live="polite">
            {active.outcome === 'queued'
              ? 'Source-code walk queued, waiting for the worker.'
              : `Walking source codes: ${active.done.toLocaleString()} of ${active.keys.toLocaleString()} codes.`}
            {' '}{active.rebuild_evidence
              ? "Each code's units are rebuilt from its patients and its axis variants refreshed."
              : 'This walk refreshes axis variants only; ask again once it finishes to rebuild the units from patients.'}
          </p>
        : <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>
              {status.walked_at
                ? <>Source codes walked {when(status.walked_at)}{completed ? ` (${completed.refreshed.toLocaleString()} refreshed)` : ''}.</>
                : 'Source codes never walked.'}
              {' '}<strong>{patients.toLocaleString()}</strong> {patients === 1 ? 'patient' : 'patients'} loaded since.
            </span>
            <button type="button" className={button} disabled={!canApprove || starting}
              title={canApprove ? 'Rebuild every code\'s units from the loaded patients and refresh its axis variants'
                : 'Approval rights are required to start a walk'}
              onClick={() => void start()}>{starting ? 'Starting…' : 'Walk source codes'}</button>
          </div>}
      {!active && last?.outcome === 'failed' && <p role="alert" className="mt-1 text-rose-700">
        The last walk failed{last.failure_reason ? `: ${last.failure_reason}` : ''}. Starting another retries it.
      </p>}
      {evidence && <details className="mt-1">
        <summary className="cursor-pointer text-slate-600">
          Axis evidence across {evidence.codes.toLocaleString()} codes{active || last?.id !== completed?.id ? ' (last completed walk)' : ''}
        </summary>
        <table aria-label="LOINC axis evidence" className="mt-1 w-full text-left">
          <thead className="text-slate-500"><tr>
            <th className="pr-2 font-medium">Axis</th><th className="pr-2 font-medium">Codes settled</th>
            <th className="pr-2 font-medium">From</th><th className="pr-2 font-medium">Hint only</th>
            <th className="font-medium">Varies across patients</th>
          </tr></thead>
          <tbody>{AXIS_LABELS.map(([axis, label]) => {
            const counts = evidence.axes[axis] ?? { settled: 0, hint: 0, varies: 0, bases: {} };
            const from = Object.entries(counts.bases).map(([basis, n]) => `${BASIS_LABELS[basis] ?? basis} ${n.toLocaleString()}`).join(', ');
            return <tr key={axis} className="border-t border-slate-200">
              <td className="pr-2 py-0.5">{label}</td>
              <td className="pr-2 py-0.5 tabular-nums">{counts.settled.toLocaleString()}</td>
              <td className="pr-2 py-0.5">{from || '—'}</td>
              <td className="pr-2 py-0.5 tabular-nums">{counts.hint.toLocaleString()}</td>
              <td className="py-0.5 tabular-nums">{counts.varies.toLocaleString()}</td>
            </tr>;
          })}</tbody>
        </table>
        <p className="mt-1 text-slate-500">Units settle the property and scale. If no other axis is ever settled or varies,
          the data holds no evidence for more than one LOINC per unit variant.</p>
      </details>}
    </>}
  </section>;
}
