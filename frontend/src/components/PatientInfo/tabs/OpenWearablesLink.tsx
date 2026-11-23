import { useEffect, useId, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

interface Connection {
  enabled: boolean;
  connected: boolean;
  devices: number;
  code_pending: boolean;
  /** Which code is pending, without being the code. */
  code_expires_at: string | null;
  /** When the newest phone joined. */
  last_connected_at: string | null;
  last_sync_at: string | null;
  providers: string[];
  host: string;
}

/** A code the server has just made. Held here only: a reload forgets it. */
interface Invitation {
  code: string;
  /**
   * When to stop waiting for it, on this browser's clock: the moment the
   * answer arrived plus the code's lifetime. Never the server's instant, which
   * a browser clock that is wrong would read as long past or far ahead.
   */
  deadline: number;
  /** The server's name for this code, to tell a newer one that replaced it. */
  codeExpiresAt: string | null;
  /** When the newest phone had joined as the code was made, to tell one joining since. */
  lastConnectedAt: string | null;
}

/** What a look at the server says about the code on screen. */
type Verdict = 'waiting' | 'connected' | 'unusable' | 'expired' | 'off';

const CONNECTION_PATH = '/v1/open-wearables/connection/';
const CODE_PATH = '/v1/open-wearables/invitation-code/';
/** A code's lifetime where the server does not say. */
const CODE_SECONDS = 15 * 60;

const NOTICES: Record<Exclude<Verdict, 'waiting' | 'off'>, string> = {
  connected: 'Your phone is connected. Its data arrives here after the app next syncs.',
  unusable: 'That code can no longer be used. If your phone did not connect, get a new code.',
  expired: 'That code has expired. Get a new one to connect a phone.',
};

/** What the app's sources are called on the phone. */
const PROVIDER_LABELS: Record<string, string> = {
  apple: 'Apple Health',
  health_connect: 'Health Connect',
};

/** The status the server sent, or null for an answer that is not one. */
function readStatus(data: unknown): Connection | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const raw = data as Record<string, unknown>;
  // An error body is an object too; a status always says whether it is offered.
  if (typeof raw.enabled !== 'boolean') return null;
  const devices = typeof raw.devices === 'number' && raw.devices > 0 ? Math.floor(raw.devices) : 0;
  return {
    enabled: raw.enabled === true,
    connected: raw.connected === true || devices > 0,
    devices,
    code_pending: raw.code_pending === true,
    code_expires_at: typeof raw.code_expires_at === 'string' ? raw.code_expires_at : null,
    last_connected_at: typeof raw.last_connected_at === 'string' ? raw.last_connected_at : null,
    last_sync_at: typeof raw.last_sync_at === 'string' ? raw.last_sync_at : null,
    providers: Array.isArray(raw.providers)
      ? raw.providers.filter((name): name is string => typeof name === 'string') : [],
    host: typeof raw.host === 'string' ? raw.host : '',
  };
}

/** The browser's IANA zone: it decides which day a late-evening reading belongs to. */
function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
  } catch {
    return undefined;
  }
}

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

function dateTime(value: string | null): string {
  const parsed = timestamp(value);
  return parsed === null ? '' : new Date(parsed).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function clock(value: number): string {
  return new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function sources(providers: string[]): string {
  const names = providers.map(name => (
    Object.prototype.hasOwnProperty.call(PROVIDER_LABELS, name) ? PROVIDER_LABELS[name] : name));
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** The status in an answer, or null for a refusal or anything that is not one. */
function readResponse(res: { status?: number; data?: unknown } | undefined): Connection | null {
  if (typeof res?.status === 'number' && (res.status < 200 || res.status >= 300)) return null;
  return readStatus(res?.data);
}

/** Where the connection stands now, or null when that cannot be read. */
async function fetchStatus(): Promise<Connection | null> {
  try {
    return readResponse(await clinicalClient().get(clinicalUrl(CONNECTION_PATH)));
  } catch {
    return null;
  }
}

/** Whether two instants the server sent differ; false when either is missing. */
function differs(a: string | null, b: string | null): boolean {
  if (a === null || b === null) return false;
  const [first, second] = [timestamp(a), timestamp(b)];
  return first !== null && second !== null ? first !== second : a !== b;
}

/** Whether a phone has joined since `before`, by when the newest one did. */
function joinedSince(now: string | null, before: string | null): boolean {
  if (now === null) return false;
  if (before === null) return true;
  const [first, second] = [timestamp(now), timestamp(before)];
  return first !== null && second !== null ? first > second : now !== before;
}

/**
 * What `status` says about the code on screen. `last` is the one look taken
 * once the code's time is up: whatever it does not settle, the time has.
 */
function judge(invitation: Invitation, status: Connection | null, last: boolean): Verdict {
  if (!status) return last ? 'expired' : 'waiting';
  // Switched off meanwhile: the code cannot be used, and the panel says so.
  if (!status.enabled) return 'off';
  // Not the count of phones: a new one that displaces the oldest leaves it as it was.
  if (joinedSince(status.last_connected_at, invitation.lastConnectedAt)) return 'connected';
  // A newer code, made on another page or by a request whose answer was lost.
  if (status.code_pending && differs(status.code_expires_at, invitation.codeExpiresAt)) return 'unusable';
  if (last) return 'expired';
  return status.code_pending ? 'waiting' : 'unusable';
}

/** The server's wording, except for a limit: that arrives as seconds to wait. */
function errorText(err: unknown, fallback: string, limited: string): string {
  const response = (err as { response?: { status?: number; data?: { error?: string; detail?: string } } })?.response;
  if (response?.status === 429) return limited;
  const text = response?.data?.error || response?.data?.detail;
  return typeof text === 'string' && text ? text : fallback;
}

/**
 * A phone app built on the Open Wearables SDK, connected to the patient's own
 * record (#2810). The patient gets a short one-time code here, types this
 * server's address and the code into the app, and the app then sends what it
 * reads from Apple Health or Google Health Connect by itself. Shown only where
 * the deployment offers it, and only to a patient on their own record: the
 * server files the connection under the signed-in person. Where it has been
 * switched off with a phone still connected, the panel stays for that phone:
 * it can be disconnected, and no code is offered.
 *
 * The code is shown once and kept in this component only. While it is shown
 * the panel asks the server now and then whether a phone has used it, and
 * once more when the code's time is up.
 *
 * Disconnecting signs every phone out. Values already stored stay; they are
 * removed from the upload history like any other upload.
 */
export default function OpenWearablesLink({ disabled = false, pollMs = 10_000 }: {
  /** True while another write to the record is running. */
  disabled?: boolean;
  /** How often to ask whether the code on screen has been used. */
  pollMs?: number;
}) {
  const headingId = useId();
  const [connection, setConnection] = useState<Connection | null>(null);
  const [invitation, setInvitation] = useState<Invitation | null>(null);
  const [busy, setBusy] = useState<'code' | 'disconnect' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    (async () => {
      // Not offered when its state cannot be read; the other ways in still work.
      const next = await fetchStatus();
      if (current && next) setConnection(next);
    })();
    return () => { current = false; };
  }, []);

  // While a code is on screen, watch for the phone that uses it: the patient
  // is looking at their phone, and nothing else would tell this page. A change
  // of code, or none, ends the watch, and an answer still on its way is dropped.
  useEffect(() => {
    if (!invitation) return undefined;
    let current = true;
    let asking = false;
    const timer = window.setInterval(() => {
      // One look at a time: a slow answer is not asked for again over itself.
      if (asking) return;
      const last = Date.now() >= invitation.deadline;
      if (last) window.clearInterval(timer);
      asking = true;
      (async () => {
        const next = await fetchStatus();
        asking = false;
        if (!current) return;
        if (next) setConnection(next);
        const verdict = judge(invitation, next, last);
        if (verdict === 'waiting') return;
        setInvitation(null);
        if (verdict !== 'off') setNotice(NOTICES[verdict]);
      })();
    }, pollMs);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [invitation, pollMs]);

  // Nothing to offer where connecting a phone is not set up, unless a phone
  // is already connected: that one can still be disconnected.
  if (!connection || (!connection.enabled && !connection.connected)) return null;

  const idle = busy !== null || disabled;
  const phones = connection.connected ? Math.max(connection.devices, 1) : 0;

  const getCode = async () => {
    if (idle || !connection.enabled) return;
    setBusy('code');
    setError(null);
    setNotice(null);
    try {
      const res = await clinicalClient().post(clinicalUrl(CODE_PATH), { time_zone: browserTimeZone() });
      const received = Date.now();
      const next = readResponse(res);
      const { code, code_expires_in: seconds } = (res?.data ?? {}) as { code?: unknown; code_expires_in?: unknown };
      if (!next || typeof code !== 'string' || !code) throw new Error('no code');
      const lifetime = typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0 ? seconds : CODE_SECONDS;
      setConnection(next);
      setInvitation({
        code,
        deadline: received + lifetime * 1000,
        codeExpiresAt: next.code_expires_at,
        lastConnectedAt: next.last_connected_at,
      });
      // Whatever a look at the code before this one concluded is about that code.
      setNotice(null);
    } catch (err: unknown) {
      setError(errorText(err, 'Could not get a connection code. Please try again.',
        'Too many codes requested. Try again in a few minutes.'));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    if (idle) return;
    const which = phones === 1 ? 'your phone' : `all ${phones} phones`;
    if (!window.confirm(`Disconnect ${which}? Values already stored stay in your record. `
      + 'You can remove them from the upload history.')) return;
    setBusy('disconnect');
    setError(null);
    setNotice(null);
    try {
      const res = await clinicalClient().delete(clinicalUrl(CONNECTION_PATH));
      setConnection(readResponse(res) ?? {
        ...connection, connected: false, devices: 0, code_pending: false, code_expires_at: null,
        last_connected_at: null, last_sync_at: null, providers: [],
      });
      // A code made before this no longer works either.
      setInvitation(null);
      setNotice(`${phones === 1 ? 'Your phone is' : 'Your phones are'} disconnected. Values already stored `
        + 'stay in your record; you can remove them from the upload history.');
    } catch (err: unknown) {
      setError(errorText(err, 'Could not disconnect. Please try again.',
        'Too many requests. Try again in a few minutes.'));
    } finally {
      setBusy(null);
    }
  };

  const host = connection.host || window.location.origin;
  const from = sources(connection.providers);
  const last = dateTime(connection.last_sync_at);
  const button = `rounded-md px-4 py-2 text-sm font-medium shadow-sm ${idle ? 'cursor-not-allowed opacity-50' : ''}`;
  let codeLabel = 'Get a connection code';
  if (busy === 'code') codeLabel = 'Getting a code...';
  else if (invitation) codeLabel = 'Get a new code';
  else if (connection.connected) codeLabel = 'Connect another phone';

  return (
    <section aria-labelledby={headingId} className="mb-4 rounded-md border border-gray-200 px-4 py-3">
      <h3 id={headingId} className="text-sm font-medium text-portal-text-primary">Phone health app</h3>
      <p className="mt-1 text-xs text-gray-500">
        Connect an app built on Open Wearables that reads Apple Health or Google Health Connect, and it
        sends your daily data here by itself. You can disconnect at any time.
      </p>
      {connection.connected && (
        <p className="mt-2 text-sm text-gray-700">
          {phones} {phones === 1 ? 'phone' : 'phones'} connected.{from ? ` Synced from ${from}.` : ''}{' '}
          {last ? `Last sync ${last}.` : 'No data received yet.'}
        </p>
      )}
      {!connection.enabled && (
        <p className="mt-1 text-xs text-amber-700">
          New phones cannot be connected on this server right now.
        </p>
      )}
      <div aria-live="polite">
        {invitation && connection.enabled && (
          <div className="mt-3 rounded-md border border-gray-200 bg-gray-50 px-4 py-3">
            <p className="text-xs font-medium text-gray-500">Connection code</p>
            <p className="mt-1 font-mono text-3xl font-semibold tracking-widest text-portal-text-primary">
              <span aria-hidden="true" className="select-all">{invitation.code}</span>
              {/* Read out a character at a time, not as one word. */}
              <span className="sr-only select-none">{invitation.code.split('').join(' ')}</span>
            </p>
            <p className="mt-3 text-xs font-medium text-gray-500">Server address</p>
            <p className="mt-1 select-all break-all font-mono text-sm text-portal-text-primary">{host}</p>
            <p className="mt-3 text-xs text-gray-500">
              In the app, enter the server address and then the code.{' '}
              This code works once and expires at {clock(invitation.deadline)}.
            </p>
          </div>
        )}
        {notice && <p className="mt-2 text-sm text-gray-700">{notice}</p>}
      </div>
      <div className="mt-3 flex flex-wrap gap-3">
        {connection.enabled && (
          <button type="button" onClick={() => void getCode()} aria-disabled={idle}
            className={invitation
              ? `border border-gray-300 bg-white text-gray-700 ${idle ? '' : 'hover:bg-gray-50'} ${button}`
              : `bg-portal-brand text-white ${idle ? '' : 'hover:bg-portal-brand/90'} ${button}`}>
            {codeLabel}
          </button>
        )}
        {connection.connected && (
          <button type="button" onClick={() => void disconnect()} aria-disabled={idle}
            className={`border border-gray-300 bg-white text-gray-700 ${idle ? '' : 'hover:bg-gray-50'} ${button}`}>
            {busy === 'disconnect' ? 'Disconnecting...' : 'Disconnect'}
          </button>
        )}
      </div>
      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  );
}
