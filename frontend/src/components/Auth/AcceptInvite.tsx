import { useEffect, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { publicApi } from '@/api/publicAxios';

type State = 'loading' | 'ready' | 'success' | 'error';

const MIN_PASSWORD_LENGTH = 8;

// Module-local (not exported): the file must export only the component so
// react-refresh/only-export-components / Fast Refresh stays happy.
function redirectBrowser(url: string) {
  window.location.assign(url);
}

function apiError(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const msg = (err as { response?: { data?: { error?: string } } }).response?.data?.error;
    if (msg) return msg;
  }
  return fallback;
}

interface InviteInfo {
  email: string;
  org_name: string;
  role: string;
  needs_password: boolean;
}

export default function AcceptInvite() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token') ?? '';

  const [state, setState] = useState<State>('loading');
  const [message, setMessage] = useState('');
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [redirectUrl, setRedirectUrl] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  useEffect(() => {
    if (!token) {
      setState('error');
      setMessage('No invitation token found in this link.');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await publicApi.get('/v1/orgs/invitation-lookup/', { params: { token } });
        if (cancelled) return;
        setInfo(res.data);
        setState('ready');
      } catch (err: unknown) {
        if (cancelled) return;
        setMessage(apiError(err, 'This invitation link is invalid or has expired.'));
        setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const needsPassword = !!info?.needs_password;

  const handleAccept = async () => {
    if (needsPassword) {
      if (password.length < MIN_PASSWORD_LENGTH) {
        setMessage(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
        return;
      }
      if (password !== confirmPassword) {
        setMessage('Passwords do not match.');
        return;
      }
    }
    setMessage('');
    setConfirming(true);
    try {
      const payload: Record<string, string> = { token };
      if (needsPassword) payload.password = password;
      const res = await publicApi.post('/orgs/confirm-invitation/', payload);
      setMessage(res.data.detail ?? 'Invitation accepted.');
      setRedirectUrl(res.data.redirect_url ?? '');
      setState('success');
      if (res.data.redirect_url) {
        redirectBrowser(res.data.redirect_url);
      }
    } catch (err: unknown) {
      setMessage(apiError(err, 'Failed to accept invitation. The link may have expired or already been used.'));
      setState('error');
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-6">
        <h1 className="text-2xl font-semibold text-gray-900">Accept Invitation</h1>

        {state === 'loading' && (
          <p className="text-sm text-gray-600">Loading your invitation…</p>
        )}

        {state === 'ready' && info && (
          <>
            <p className="text-sm text-gray-600">
              You've been invited to join <span className="font-medium">{info.org_name}</span> as
              a {info.role}.
            </p>
            {needsPassword && (
              <div className="space-y-4">
                <p className="text-sm text-gray-600">Choose a password to finish setting up your account.</p>
                <div>
                  <label htmlFor="password" className="block text-sm font-medium text-gray-700">Password</label>
                  <input
                    id="password"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    placeholder="At least 8 characters"
                  />
                </div>
                <div>
                  <label htmlFor="confirm-password" className="block text-sm font-medium text-gray-700">Confirm password</label>
                  <input
                    id="confirm-password"
                    type="password"
                    autoComplete="new-password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                    placeholder="Re-enter your password"
                  />
                </div>
              </div>
            )}
            {message && (
              <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">{message}</p>
            )}
            <button
              onClick={handleAccept}
              disabled={confirming}
              className="w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
            >
              {confirming ? 'Accepting…' : 'Accept Invitation'}
            </button>
          </>
        )}

        {state === 'success' && (
          <>
            <p className="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-3">
              {message}
            </p>
            {redirectUrl ? (
              <a
                href={redirectUrl}
                className="block w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm font-medium text-center"
              >
                Continue
              </a>
            ) : (
              <button
                onClick={() => navigate('/')}
                className="w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm font-medium"
              >
                Go to PROMOP
              </button>
            )}
          </>
        )}

        {state === 'error' && (
          <>
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">
              {message}
            </p>
            <button
              onClick={() => navigate('/login')}
              className="w-full py-2 px-4 border border-gray-300 rounded text-sm text-gray-700 hover:bg-gray-50"
            >
              Back to Login
            </button>
          </>
        )}
      </div>
    </div>
  );
}
