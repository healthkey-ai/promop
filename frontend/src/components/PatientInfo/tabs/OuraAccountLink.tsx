import { useEffect, useRef, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { pendingClause, type PendingSamples } from './wearableResult';

interface Connection {
  configured: boolean;
  connected: boolean;
  connected_at: string | null;
  last_sync_at: string | null;
  synced_through: string | null;
}

interface SyncResult extends PendingSamples {
  samples_created: number;
  replaced?: number;
  settled_rows?: number;
  duplicates_skipped?: number;
  skipped?: 'recent';
  last_sync_at?: string;
  start_date?: string;
  end_date?: string;
  /** Kinds of data Oura did not share (the patient switched a box off). */
  unavailable?: string[];
  /** The read ran out of time: part of the range was not read. */
  incomplete?: boolean;
}

/** What the callback put in the address when it sent the patient back. */
type Outcome = 'connected' | 'denied' | 'error';

const OUTCOMES: Record<Outcome, { tone: 'good' | 'bad'; text: string }> = {
  connected: { tone: 'good', text: 'Your Oura account is connected.' },
  denied: { tone: 'bad', text: 'Oura did not connect your account: access was not approved on Oura’s page.' },
  error: { tone: 'bad', text: 'Your Oura account could not be connected. Please try again.' },
};

const URL_PATH = '/v1/oura/connection/';
/** A sync older than this is repeated when the tab opens. */
const STALE_MS = 12 * 60 * 60 * 1000;

const KIND_LABELS: Record<string, string> = {
  activity: 'activity', sleep: 'sleep', spo2: 'blood oxygen', vo2: 'VO₂ max',
};

/** The `?oura=` the callback left in the address, if any. */
function readOutcome(): Outcome | null {
  const value = new URLSearchParams(window.location.search).get('oura');
  if (value === null) return null;
  // Own keys only: "toString" is in every object and is no outcome.
  return Object.prototype.hasOwnProperty.call(OUTCOMES, value) ? (value as Outcome) : 'error';
}

/** Takes `?oura=` out of the address, so a reload does not say it again. */
function clearOutcome() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has('oura')) return;
  params.delete('oura');
  const query = params.toString();
  window.history.replaceState(
    window.history.state, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
}

function day(value: string | null | undefined): string {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleDateString();
}

function stale(value: string | null): boolean {
  if (!value) return true;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) || Date.now() - parsed > STALE_MS;
}

function errorText(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { error?: string; detail?: string } } })?.response?.data;
  return data?.error || data?.detail || fallback;
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

function describe(result: SyncResult): string {
  if (result.skipped === 'recent') return 'Your Oura data is already up to date.';
  const parts = [count(result.samples_created, 'new value')];
  if ((result.replaced ?? 0) > 0) parts.push(`${result.replaced} replaced`);
  const range = result.start_date && result.end_date ? ` ${result.start_date} to ${result.end_date}` : '';
  const pending = pendingClause(result);
  const missing = (result.unavailable ?? []).map(kind => KIND_LABELS[kind] ?? kind);
  return `Synced${range}: ${parts.join(', ')}.${pending ? ` ${pending}` : ''}`
    + (missing.length > 0
      ? ` Oura did not share ${missing.join(', ')}. Disconnect and connect again, leaving every box ticked, to include it.`
      : '')
    + (result.incomplete ? ' Oura was slow, so only part of the range was read. Sync again for the rest.' : '');
}

/**
 * An Oura account linked by OAuth (#2900). The patient approves this site once
 * on Oura's own page; this site then reads their days from Oura's API: when
 * the tab opens (if the last sync is old), right after connecting, and when
 * they press Sync now. Oura pushes nothing, so nothing arrives while the tab
 * is closed. Shown only where the deployment has an Oura app registered, and
 * only to a patient on their own record: the server attaches the account to
 * the signed-in person.
 *
 * A part of the tab's Oura Ring section, above the export upload (#2889): a
 * later day from either replaces the same day from the other.
 */
export default function OuraAccountLink({
  personId, disabled = false, onSynced, onBusyChange, leave = (url) => window.location.assign(url),
}: {
  /** The record on the page. The server refuses a sync unless it is the caller's own. */
  personId?: number;
  /** True while another write to the record is running. */
  disabled?: boolean;
  /** Called after a sync the server accepted; `changed` when rows were written or replaced. */
  onSynced?: (changed: boolean) => void;
  /** Called when a sync starts and when it ends, so the tab can hold its other writes. */
  onBusyChange?: (busy: boolean) => void;
  /** Sends the browser to Oura's consent page. */
  leave?: (url: string) => void;
}) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(readOutcome);
  const [busy, setBusy] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnect, setReconnect] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  // The sync the tab starts by itself runs once, however often the tab renders.
  const autoRan = useRef(false);
  const mounted = useRef(true);
  // The outcome the page opened with, kept for the first read: it is cleared
  // from the address on mount.
  const openedWith = useRef<Outcome | null>(outcome);

  useEffect(() => { clearOutcome(); }, []);

  // Back from Oura's page with the browser's Back button, the page can be
  // restored as it was left: mid-"Opening Oura...", with the button inert.
  useEffect(() => {
    const restored = (event: PageTransitionEvent) => { if (event.persisted) setBusy(false); };
    window.addEventListener('pageshow', restored);
    return () => window.removeEventListener('pageshow', restored);
  }, []);

  const sync = async () => {
    setSyncing(true);
    onBusyChange?.(true);
    setError(null);
    setReconnect(false);
    setResult(null);
    setNotice(null);
    try {
      const res = await clinicalClient().post<SyncResult>(
        clinicalUrl('/v1/oura/sync/'), personId != null ? { person_id: personId } : {});
      if (!mounted.current) return;
      setResult(describe(res.data));
      if (res.data.skipped !== 'recent') {
        setConnection(current => current && {
          ...current, last_sync_at: new Date().toISOString(),
          synced_through: res.data.incomplete ? current.synced_through : (res.data.end_date ?? current.synced_through),
        });
      }
      onSynced?.((res.data.samples_created ?? 0) > 0 || (res.data.replaced ?? 0) > 0
        || (res.data.settled_rows ?? 0) > 0);
    } catch (err: unknown) {
      if (!mounted.current) return;
      setError(errorText(err, 'Sync failed. Please try again.'));
      setReconnect((err as { response?: { data?: { reconnect?: boolean } } })?.response?.data?.reconnect === true);
    } finally {
      if (mounted.current) setSyncing(false);
      onBusyChange?.(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    (async () => {
      let status: Connection | null = null;
      try {
        const res = await clinicalClient().get<Connection>(clinicalUrl(URL_PATH));
        if (res?.data && typeof res.data === 'object' && !Array.isArray(res.data)) status = res.data;
      } catch {
        // Not offered when its state cannot be read; the export upload still works.
      }
      if (!mounted.current || !status) return;
      setConnection(status);
      // Right after connecting, and when the last sync is old: read the days.
      if (status.connected && !autoRan.current && (openedWith.current === 'connected' || stale(status.last_sync_at))) {
        autoRan.current = true;
        await sync();
      }
    })();
    return () => { mounted.current = false; };
    // The first read only: later syncs are the patient's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Nothing to offer where no Oura app is set up, unless an account is already
  // connected: that one can still be disconnected.
  if (!connection || (!connection.configured && !connection.connected)) return null;

  const connect = async () => {
    if (busy || syncing || disabled) return;
    setBusy(true);
    setError(null);
    setReconnect(false);
    setOutcome(null);
    setNotice(null);
    setResult(null);
    try {
      const res = await clinicalClient().post<{ authorize_url: string }>(clinicalUrl('/v1/oura/oauth/start/'));
      const url = res?.data?.authorize_url;
      if (typeof url !== 'string' || !url.startsWith('https://')) throw new Error('no consent page');
      // Oura's own page; it sends the patient back here when they answer.
      leave(url);
    } catch (err: unknown) {
      setError(errorText(err, 'Could not start connecting to Oura. Please try again.'));
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (busy || syncing || disabled) return;
    if (!window.confirm('Disconnect your Oura account? Data already received stays in your record.')) return;
    setBusy(true);
    setError(null);
    setReconnect(false);
    setOutcome(null);
    setNotice(null);
    setResult(null);
    try {
      const res = await clinicalClient().delete<{ oura_notified: boolean }>(clinicalUrl(URL_PATH));
      setConnection({ ...connection, connected: false, connected_at: null, last_sync_at: null, synced_through: null });
      setNotice(res.data?.oura_notified
        ? 'Your Oura account is disconnected and Oura has been told to end this access.'
        : 'Your Oura account is disconnected here, but Oura could not be told. To end the access there, '
          + 'remove this app in your Oura account (Membership Hub, Connected apps).');
    } catch (err: unknown) {
      setError(errorText(err, 'Could not disconnect. Please try again.'));
    } finally {
      setBusy(false);
    }
  };

  const since = day(connection.connected_at);
  const last = day(connection.last_sync_at);
  const idle = busy || syncing || disabled;
  const button = `rounded-md px-4 py-2 text-sm font-medium shadow-sm ${idle ? 'cursor-not-allowed opacity-50' : ''}`;
  const primary = `bg-portal-brand text-white ${idle ? '' : 'hover:bg-portal-brand/90'} ${button}`;
  const secondary = `border border-gray-300 bg-white text-gray-700 ${idle ? '' : 'hover:bg-gray-50'} ${button}`;

  return (
    <div className="py-3 last:pb-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Connect your Oura account</h4>
      {connection.connected ? (
        <>
          <p className="mt-1 text-xs text-gray-500">
            Connected{since ? ` since ${since}` : ''}. This site reads your days from Oura when you open this
            tab and when you press Sync now. {last ? `Last synced ${last}.` : 'Not synced yet.'}
          </p>
          <div className="mt-3 flex flex-wrap gap-3">
            <button type="button" onClick={() => void sync()} aria-disabled={idle} disabled={idle} className={primary}>
              {syncing ? 'Syncing...' : 'Sync now'}
            </button>
            <button type="button" onClick={() => void disconnect()} aria-disabled={idle} className={secondary}>
              {busy ? 'Disconnecting...' : 'Disconnect Oura'}
            </button>
            {reconnect && (
              <button type="button" onClick={() => void connect()} aria-disabled={idle} className={primary}>
                Connect again
              </button>
            )}
          </div>
        </>
      ) : (
        <>
          <p className="mt-1 text-xs text-gray-500">
            Connect your Oura account once and this site reads your steps, active minutes, sleep, heart rate
            variability, resting heart rate, breathing rate, blood oxygen and VO&#8322; max, starting with
            the last 90 days. You approve it on Oura&rsquo;s own page and can disconnect at any time.
          </p>
          <button type="button" onClick={() => void connect()} aria-disabled={idle} className={`mt-3 ${primary}`}>
            {busy ? 'Opening Oura...' : 'Connect Oura'}
          </button>
        </>
      )}
      <div aria-live="polite">
        {outcome && (
          <p className={`mt-2 text-sm ${OUTCOMES[outcome].tone === 'good' ? 'text-green-700' : 'text-red-600'}`}>
            {OUTCOMES[outcome].text}
          </p>
        )}
        {result && <p className="mt-2 text-sm text-green-700">{result}</p>}
        {notice && <p className="mt-2 text-sm text-gray-700">{notice}</p>}
      </div>
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  );
}
