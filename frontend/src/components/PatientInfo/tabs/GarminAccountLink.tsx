import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

interface Connection {
  configured: boolean;
  connected: boolean;
  connected_at: string | null;
  last_push_at: string | null;
  permissions: string[];
}

/** What the callback put in the address when it sent the patient back. */
type Outcome = 'connected' | 'denied' | 'in_use' | 'error';

const OUTCOMES: Record<Outcome, { tone: 'good' | 'bad'; text: string }> = {
  connected: {
    tone: 'good',
    text: 'Your Garmin account is connected. New data arrives here after your watch next syncs with Garmin Connect.',
  },
  denied: { tone: 'bad', text: 'Garmin did not connect your account: access was not approved on Garmin’s page.' },
  in_use: { tone: 'bad', text: 'That Garmin account is already connected to another record here.' },
  error: { tone: 'bad', text: 'Your Garmin account could not be connected. Please try again.' },
};

const URL_PATH = '/v1/garmin/connection/';

/** The `?garmin=` the callback left in the address, if any. */
function readOutcome(): Outcome | null {
  const value = new URLSearchParams(window.location.search).get('garmin');
  if (value === null) return null;
  return value in OUTCOMES ? (value as Outcome) : 'error';
}

/** Takes `?garmin=` out of the address, so a reload does not say it again. */
function clearOutcome() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has('garmin')) return;
  params.delete('garmin');
  const query = params.toString();
  window.history.replaceState(
    window.history.state, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
}

function day(value: string | null): string {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : parsed.toLocaleDateString();
}

function errorText(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { error?: string; detail?: string } } })?.response?.data;
  return data?.error || data?.detail || fallback;
}

/**
 * A Garmin account linked by OAuth (#2802). The patient approves this site
 * once on Garmin's own page; Garmin then sends their daily data here as their
 * watch syncs, with nothing to paste or upload. Shown only where the
 * deployment has a Garmin app registered, and only to a patient on their own
 * record: the server attaches the account to the signed-in person.
 *
 * Disconnecting stops it here at once and tells Garmin to stop sending.
 */
export default function GarminAccountLink({ disabled = false, leave = (url) => window.location.assign(url) }: {
  /** True while another write to the record is running. */
  disabled?: boolean;
  /** Sends the browser to Garmin's consent page. */
  leave?: (url: string) => void;
}) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(readOutcome);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => { clearOutcome(); }, []);

  useEffect(() => {
    let current = true;
    (async () => {
      try {
        const res = await clinicalClient().get<Connection>(clinicalUrl(URL_PATH));
        if (current && res?.data && typeof res.data === 'object' && !Array.isArray(res.data)) setConnection(res.data);
      } catch {
        // Not offered when its state cannot be read; the other ways in still work.
      }
    })();
    return () => { current = false; };
  }, []);

  if (!connection?.configured) return null;

  const connect = async () => {
    if (busy || disabled) return;
    setBusy(true);
    setError(null);
    setOutcome(null);
    setNotice(null);
    try {
      const res = await clinicalClient().post<{ authorize_url: string }>(clinicalUrl('/v1/garmin/oauth/start/'));
      // Garmin's own page; it sends the patient back here when they answer.
      leave(res.data.authorize_url);
    } catch (err: unknown) {
      setError(errorText(err, 'Could not start connecting to Garmin. Please try again.'));
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (busy || disabled) return;
    if (!window.confirm('Disconnect your Garmin account? Data already received stays in your record.')) return;
    setBusy(true);
    setError(null);
    setOutcome(null);
    setNotice(null);
    try {
      const res = await clinicalClient().delete<{ garmin_notified: boolean }>(clinicalUrl(URL_PATH));
      setConnection({ ...connection, connected: false, connected_at: null, last_push_at: null, permissions: [] });
      setNotice(res.data?.garmin_notified
        ? 'Your Garmin account is disconnected. Garmin has been told to stop sending data.'
        : 'Your Garmin account is disconnected here, but Garmin could not be told. To stop it sending, '
          + 'remove this app in Garmin Connect (Settings, Account Information, Connected Apps).');
    } catch (err: unknown) {
      setError(errorText(err, 'Could not disconnect. Please try again.'));
    } finally {
      setBusy(false);
    }
  };

  const since = day(connection.connected_at);
  const last = day(connection.last_push_at);
  // Garmin sends health data only when the patient left that box ticked.
  const noHealth = connection.connected && !connection.permissions.includes('HEALTH_EXPORT');
  const idle = busy || disabled;
  const button = `rounded-md px-4 py-2 text-sm font-medium shadow-sm ${idle ? 'cursor-not-allowed opacity-50' : ''}`;

  return (
    <div className="mb-4 rounded-md border border-gray-200 px-4 py-3">
      <h3 className="text-sm font-medium text-portal-text-primary">Garmin account</h3>
      {connection.connected ? (
        <>
          <p className="mt-1 text-xs text-gray-500">
            Connected{since ? ` since ${since}` : ''}. Garmin sends your daily data here after your watch syncs
            with Garmin Connect. {last ? `Last received ${last}.` : 'Nothing has been received yet.'}
          </p>
          {noHealth && (
            <p className="mt-1 text-xs text-amber-700">
              Sharing health data was not allowed on Garmin&rsquo;s page, so Garmin sends none. Disconnect and
              connect again to change it.
            </p>
          )}
          <button type="button" onClick={() => void disconnect()} aria-disabled={idle}
            className={`mt-3 border border-gray-300 bg-white text-gray-700 ${idle ? '' : 'hover:bg-gray-50'} ${button}`}>
            {busy ? 'Disconnecting...' : 'Disconnect Garmin'}
          </button>
        </>
      ) : (
        <>
          <p className="mt-1 text-xs text-gray-500">
            Connect once and Garmin sends your steps, heart rate, sleep and other daily data here by itself.
            You approve it on Garmin&rsquo;s own page and can disconnect at any time.
          </p>
          <button type="button" onClick={() => void connect()} aria-disabled={idle}
            className={`mt-3 bg-portal-brand text-white ${idle ? '' : 'hover:bg-portal-brand/90'} ${button}`}>
            {busy ? 'Opening Garmin...' : 'Connect Garmin'}
          </button>
        </>
      )}
      <div aria-live="polite">
        {outcome && (
          <p className={`mt-2 text-sm ${OUTCOMES[outcome].tone === 'good' ? 'text-green-700' : 'text-red-600'}`}>
            {OUTCOMES[outcome].text}
          </p>
        )}
        {notice && <p className="mt-2 text-sm text-gray-700">{notice}</p>}
      </div>
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  );
}
