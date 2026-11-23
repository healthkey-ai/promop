import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

const TOKEN_URL = '/v1/patient-records/apple-health-sync-token/';

/** What the server says about the patient's sync token. `token` is in the answer that creates it only. */
interface TokenStatus {
  active: boolean;
  expired: boolean;
  /** There is a token, not expired, that no longer works (the password was changed since). */
  stopped?: boolean;
  hint: string | null;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  endpoint: string;
  token?: string;
}

interface Props {
  /** True while a file upload is running: one change to the tab at a time. */
  disabled?: boolean;
}

function isStatus(data: unknown): data is TokenStatus {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return false;
  const status = data as TokenStatus;
  return typeof status.endpoint === 'string' && status.endpoint !== ''
    && typeof status.active === 'boolean' && typeof status.expired === 'boolean';
}

function day(raw: string | null): string {
  return raw ? new Date(raw).toLocaleDateString() : '';
}

function moment(raw: string | null): string {
  return raw ? new Date(raw).toLocaleString() : '';
}

function errorText(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { error?: string; detail?: string } } })?.response?.data;
  return data?.error || data?.detail || fallback;
}

/** A value the patient copies to their phone, with a button that copies it. */
function CopyRow({ label, value }: { label: string; value: string }) {
  const id = useId();
  const field = useRef<HTMLInputElement>(null);
  const [copied, setCopied] = useState<'no' | 'yes' | 'failed'>('no');

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied('yes');
    } catch {
      // No clipboard access (an insecure origin, a refused permission). The
      // value is shown once, so say so and select it for copying by hand.
      setCopied('failed');
      field.current?.select();
    }
  };

  return (
    <div className="mt-2">
      <label htmlFor={id} className="block text-xs font-medium text-gray-700">{label}</label>
      <div className="mt-1 flex gap-2">
        <input
          id={id}
          ref={field}
          readOnly
          value={value}
          autoComplete="off"
          spellCheck={false}
          onFocus={e => e.currentTarget.select()}
          className="w-full min-w-0 flex-1 rounded-md border border-gray-300 bg-gray-50 px-3 py-2 font-mono text-xs text-gray-800 focus:outline-none focus:ring-2 focus:ring-portal-brand"
        />
        <button
          type="button"
          onClick={() => void copy()}
          aria-label={`Copy ${label.toLowerCase()}`}
          className="rounded-md border border-gray-300 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50"
        >
          {copied === 'yes' ? 'Copied' : 'Copy'}
        </button>
      </div>
      {/* The button's name stays "Copy ...", so the result is said here. */}
      <span role="status" className="sr-only">{copied === 'yes' ? `${label} copied` : ''}</span>
      {copied === 'failed' && (
        <p role="alert" className="mt-1 text-xs text-red-600">
          Could not copy. The text is selected: copy it by hand.
        </p>
      )}
    </div>
  );
}

/**
 * Sync from Apple Health (#2800). Apple has no cloud API for Health data: it
 * is on the iPhone, so an app or Shortcut there sends it. This panel is where
 * the patient makes the sync token that app sends with each day's values, and
 * replaces or removes it. The token is shown once, when it is made, and is
 * kept only in this component until the patient is done copying it.
 */
export default function AppleHealthSync({ disabled = false }: Props) {
  const [status, setStatus] = useState<TokenStatus | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The status could not be read for a reason other than "not yours".
  const [unread, setUnread] = useState(false);
  // Each read, create and turn-off takes a number; a read whose number is no
  // longer the latest is an answer about a token that has since changed.
  const latest = useRef(0);
  const panel = useRef<HTMLDivElement>(null);
  const tokenBox = useRef<HTMLDivElement>(null);
  const shown = useRef(false);

  // The buttons that were pressed leave the page when the token appears and
  // when it is put away, taking keyboard focus with them: move it to what
  // replaced them, which also has a screen reader say the token is here.
  useEffect(() => {
    if (token) {
      shown.current = true;
      tokenBox.current?.focus();
    } else if (shown.current) {
      shown.current = false;
      panel.current?.focus();
    }
  }, [token]);

  const read = useCallback(async (live: () => boolean = () => true) => {
    const mine = ++latest.current;
    const current = () => live() && mine === latest.current;
    try {
      const res = await clinicalClient().get(clinicalUrl(TOKEN_URL));
      if (!current()) return;
      if (isStatus(res.data)) {
        setStatus(res.data);
        setUnread(false);
      } else {
        // A 200 that is not a status (a proxy's page, say) is not "no token".
        setUnread(true);
      }
    } catch (err: unknown) {
      if (!current()) return;
      // A 403 is the server saying this is not the reader's own record: there
      // is no token to manage and the panel stays hidden. Anything else (a
      // rate limit, a server error, no connection) must not hide a token the
      // patient may want to turn off.
      const code = (err as { response?: { status?: number } })?.response?.status;
      setUnread(code !== 403);
    }
  }, []);

  useEffect(() => {
    let live = true;
    (async () => {
      await read(() => live);
    })();
    return () => { live = false; };
  }, [read]);

  if (!status) {
    if (!unread) return null;
    return (
      <div className="mb-4 rounded-md border border-gray-200 px-4 py-3">
        <h3 className="text-sm font-medium text-portal-text-primary">Sync from Apple Health</h3>
        <p role="alert" className="mt-2 text-sm text-red-600">
          The sync token could not be loaded.
        </p>
        <button
          type="button"
          onClick={() => void read()}
          className="mt-2 inline-flex items-center rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          Try again
        </button>
      </div>
    );
  }

  const exists = status.active || status.expired || Boolean(status.stopped);
  // Something is in flight, here or elsewhere on the tab.
  const blocked = busy || disabled;

  const create = async () => {
    if (blocked) return;
    if (status.active && !confirm(
      'Make a new sync token? The one on your iPhone stops working until you replace it there.',
    )) return;
    latest.current += 1;
    setBusy(true);
    setError(null);
    try {
      const res = await clinicalClient().post(clinicalUrl(TOKEN_URL));
      if (isStatus(res.data) && res.data.token) {
        const { token: issued, ...rest } = res.data;
        setStatus(rest);
        setToken(issued);
      } else {
        // The server may already have replaced the old token: say so, and
        // show what it now holds.
        setError('A sync token was made but could not be shown. Make another one.');
        await read();
      }
    } catch (err: unknown) {
      setError(errorText(err, 'The sync token could not be created. Please try again.'));
    } finally {
      setBusy(false);
    }
  };

  const turnOff = async () => {
    if (blocked) return;
    if (!confirm('Turn off Apple Health sync? Your iPhone can no longer send data until you make a new token.')) return;
    latest.current += 1;
    setBusy(true);
    setError(null);
    try {
      const res = await clinicalClient().delete(clinicalUrl(TOKEN_URL));
      if (isStatus(res.data)) setStatus(res.data);
      setToken(null);
    } catch (err: unknown) {
      setError(errorText(err, 'The sync token could not be turned off. Please try again.'));
    } finally {
      setBusy(false);
    }
  };

  const primary = 'inline-flex items-center rounded-md bg-portal-brand px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-portal-brand/90 disabled:opacity-50';
  const secondary = 'inline-flex items-center rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50';

  return (
    <div
      ref={panel}
      tabIndex={-1}
      className="mb-4 rounded-md border border-gray-200 px-4 py-3 focus:outline-none"
    >
      <h3 className="text-sm font-medium text-portal-text-primary">Sync from Apple Health</h3>
      <p className="mt-1 text-xs text-gray-500">
        Apple keeps Health data on your iPhone, so it is sent from the phone. Make a sync token
        here, then give the address and the token to an app on your iPhone that can send Apple
        Health data to a web address, such as Health Auto Export or an iOS Shortcut. Each day it
        sends replaces what was stored for that day.
      </p>

      {token && (
        <div
          ref={tokenBox}
          tabIndex={-1}
          role="group"
          aria-label="Your new sync token"
          className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-portal-brand"
        >
          <p className="text-sm font-medium text-amber-900">
            Copy the token now. It is not shown again.
          </p>
          <CopyRow label="Address" value={status.endpoint} />
          <CopyRow label="Sync token" value={token} />
          <p className="mt-2 text-xs text-amber-900">
            Send the token as the header <span className="font-mono">Authorization</span> with
            the value <span className="font-mono">Bearer</span>, a space, then the token. In
            Health Auto Export: a REST API automation, format JSON, data summarised by day. The
            token can add wearable data to your record and cannot read it.
          </p>
          <button
            type="button"
            onClick={() => { setToken(null); void read(); }}
            className={`${secondary} mt-2`}
          >
            Done
          </button>
        </div>
      )}

      {!token && status.active && (
        <p className="mt-3 text-sm text-gray-700">
          A sync token ending in <span className="font-mono">{status.hint}</span> is on. It
          expires {day(status.expires_at)}.{' '}
          {status.last_used_at
            ? `Last received from your iPhone: ${moment(status.last_used_at)}.`
            : 'Nothing has been received from your iPhone yet.'}
        </p>
      )}
      {!token && status.expired && (
        <p className="mt-3 text-sm text-amber-700">
          Your sync token expired {day(status.expires_at)}. Make a new one to keep syncing.
        </p>
      )}
      {!token && status.stopped && (
        <p className="mt-3 text-sm text-amber-700">
          Your sync token ending in <span className="font-mono">{status.hint}</span> has stopped
          working. A token stops when your password is changed. Make a new one to keep
          syncing.
        </p>
      )}

      {!token && (
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" onClick={() => void create()} disabled={blocked} className={primary}>
            {busy ? 'Working...' : status.active ? 'Replace token' : 'Create sync token'}
          </button>
          {exists && (
            <button type="button" onClick={() => void turnOff()} disabled={blocked} className={secondary}>
              Turn off
            </button>
          )}
        </div>
      )}

      {error && <p role="alert" className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  );
}
