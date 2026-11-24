import { useId, useRef, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

/** How far back one sync may read. The server allows 1 to 90 days. */
const RANGES = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

/** What the server calls the data sets Garmin may fail to return. */
const DATASET_LABELS: Record<string, string> = {
  hrv: 'HRV',
  sleep: 'sleep',
  vo2_max: 'VO₂ max',
};

interface SyncResult {
  samples_created: number;
  duplicates_skipped: number;
  replaced: number;
  start_date: string;
  end_date: string;
  datasets_failed: string[];
  unmapped_metrics?: string[];
}

interface Props {
  /** The record on the page. The server refuses a sync unless it is the caller's own. */
  personId?: number;
  /** True while a file upload is running: one write to the record at a time. */
  disabled?: boolean;
  /** Called after a sync the server accepted; `changed` when rows were written or replaced. */
  onSynced: (changed: boolean) => void;
  /** Called when a sync starts and when it ends, so the tab can hold its other writes. */
  onBusyChange?: (busy: boolean) => void;
  /** Display names for metric keys, for the ones the server could not store. */
  metricLabels?: Record<string, string>;
}

/**
 * The reader's own yesterday, as YYYY-MM-DD. Today's summary is still filling
 * in, and the server's clock may be a day ahead of or behind the reader's, so
 * the last whole day is named here and not left to the server.
 */
function localYesterday(): string {
  const now = new Date();
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

function describe(result: SyncResult): string {
  const parts = [`${count(result.samples_created, 'new value')}`];
  if (result.replaced > 0) parts.push(`${result.replaced} replaced`);
  if (result.duplicates_skipped > 0) parts.push(`${result.duplicates_skipped} already stored`);
  return `Synced ${result.start_date} to ${result.end_date}: ${parts.join(', ')}.`;
}

/**
 * Sync daily metrics from Garmin Connect with the patient's own Personal API
 * key (#2794). The key is sent with this one request and kept nowhere, here or
 * on the server: it is cleared from the field as soon as a sync succeeds.
 *
 * The server files the rows under the signed-in person's own record, so the
 * tab renders this only for a patient on their own record.
 *
 * A part of the tab's Garmin section, under the linked account (#2832): the
 * account sends each new day by itself, this reads past days when asked.
 */
export default function GarminConnectSync({
  personId, disabled = false, onSynced, onBusyChange, metricLabels = {},
}: Props) {
  const keyRef = useRef<HTMLInputElement>(null);
  const keyId = useId();
  const rangeId = useId();
  const helpId = useId();
  const [apiKey, setApiKey] = useState('');
  const [days, setDays] = useState(30);
  const [syncing, setSyncing] = useState(false);
  const [result, setResult] = useState<SyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canSync = apiKey.trim() !== '' && !syncing && !disabled;

  const sync = async () => {
    if (!canSync) return;
    setSyncing(true);
    onBusyChange?.(true);
    setResult(null);
    setError(null);
    try {
      const res = await clinicalClient().post<SyncResult>(clinicalUrl('/v1/patient-records/sync-garmin/'), {
        api_key: apiKey.trim(),
        days,
        end_date: localYesterday(),
        ...(personId != null ? { person_id: personId } : {}),
      });
      setResult(res.data);
      setApiKey('');
      onSynced(res.data.samples_created > 0 || res.data.replaced > 0);
    } catch (err: unknown) {
      const data = (err as { response?: { data?: { error?: string; detail?: string } } })?.response?.data;
      setError(data?.error || data?.detail || 'Sync failed. Please try again.');
      // Back to the field that has to be corrected.
      keyRef.current?.focus();
    } finally {
      setSyncing(false);
      onBusyChange?.(false);
    }
  };

  const failed = (result?.datasets_failed ?? []).map(name => DATASET_LABELS[name] ?? name);
  const unmapped = (result?.unmapped_metrics ?? []).map(key => metricLabels[key] ?? key);

  return (
    <div className="py-3 last:pb-1">
      <h4 className="text-sm font-medium text-portal-text-primary">Sync past days</h4>
      <p id={helpId} className="mt-1 text-xs text-gray-500">
        Bring in up to 90 past days from Garmin Connect when you ask. Create a Personal API key in
        Garmin Connect on the web (Settings, Account, Personal API Token) and paste it here. The key
        is used for this sync only and is not stored.
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <div className="w-full sm:w-auto sm:min-w-[16rem] sm:flex-1">
          <label htmlFor={keyId} className="block text-xs font-medium text-gray-700">
            Garmin Connect Personal API key
          </label>
          <input
            ref={keyRef}
            id={keyId}
            // A text field drawn as dots, not type="password": a password
            // field invites the browser to fill in the saved sign-in password
            // (which would be sent to Garmin as the key) and, once the field
            // is cleared after a sync, to save the key as this site's
            // password. Nothing here should store it.
            type="text"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            data-bwignore="true"
            data-form-type="other"
            aria-describedby={helpId}
            value={apiKey}
            onChange={e => setApiKey(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); void sync(); } }}
            // Read-only, not disabled, while it runs: a disabled field drops
            // the keyboard focus it holds.
            readOnly={syncing}
            className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm [-webkit-text-security:disc] focus:outline-none focus:ring-2 focus:ring-portal-brand read-only:opacity-50"
          />
        </div>
        <div>
          <label htmlFor={rangeId} className="block text-xs font-medium text-gray-700">
            Range
          </label>
          <select
            id={rangeId}
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            disabled={syncing}
            className="mt-1 rounded-md border border-gray-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-portal-brand disabled:opacity-50"
          >
            {RANGES.map(range => (
              <option key={range.days} value={range.days}>{range.label}</option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={() => void sync()}
          // aria-disabled for the same reason: the button keeps focus through
          // the sync. sync() itself refuses to run when it may not.
          aria-disabled={!canSync}
          className={`inline-flex items-center gap-2 rounded-md bg-portal-brand px-4 py-2 text-sm font-medium text-white shadow-sm ${canSync ? 'hover:bg-portal-brand/90' : 'cursor-not-allowed opacity-50'}`}
        >
          {syncing ? 'Syncing...' : 'Sync'}
        </button>
      </div>
      <div aria-live="polite">
        {syncing && (
          <p className="mt-2 text-sm text-gray-600">Syncing with Garmin Connect. This can take a few seconds.</p>
        )}
        {result && (
          <p className="mt-2 text-sm text-green-700">{describe(result)}</p>
        )}
        {result && failed.length > 0 && (
          <p className="mt-1 text-sm text-amber-700">
            Garmin did not return {failed.join(', ')} for this range. Sync again, or choose a shorter range.
          </p>
        )}
        {result && unmapped.length > 0 && (
          <p className="mt-1 text-sm text-amber-700">
            Not stored: {unmapped.join(', ')}. This site cannot store {unmapped.length === 1 ? 'this metric' : 'these metrics'} yet.
          </p>
        )}
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>
      )}
    </div>
  );
}
