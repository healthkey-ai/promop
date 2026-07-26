import { useEffect, useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import axios from 'axios';

type State = 'loading' | 'ready' | 'success' | 'error';

const publicApi = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  headers: { 'Content-Type': 'application/json' },
});

const MIN_PASSWORD_LENGTH = 8;

function errorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const msg = (err as { response?: { data?: { error?: string } } }).response?.data?.error;
    if (msg) return msg;
  }
  return fallback;
}

export default function AcceptPatientInvite() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token') ?? '';

  // Derive the no-token error state at init (matches AcceptInvite.tsx) so the
  // effect never calls setState synchronously on mount.
  const [state, setState] = useState<State>(token ? 'loading' : 'error');
  const [message, setMessage] = useState(token ? '' : 'No invitation token found in this link.');
  const [email, setEmail] = useState('');
  const [patientName, setPatientName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    // No synchronous setState in the effect body — the no-token branch lives in
    // the async IIFE (like the catch path), which also re-derives the error
    // state if `token` later becomes absent while mounted.
    (async () => {
      if (!token) {
        setState('error');
        setMessage('No invitation token found in this link.');
        return;
      }
      try {
        const res = await publicApi.get('/v1/patient-invitations/lookup/', { params: { token } });
        setEmail(res.data.email ?? '');
        setPatientName(res.data.patient_name ?? '');
        setState('ready');
      } catch (err: unknown) {
        setMessage(errorMessage(err, 'This invitation link is invalid or has expired.'));
        setState('error');
      }
    })();
  }, [token]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) {
      setMessage(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirm) {
      setMessage('Passwords do not match.');
      return;
    }
    setMessage('');
    setSubmitting(true);
    try {
      const res = await publicApi.post('/v1/patient-invitations/accept/', { token, password });
      setMessage(res.data.detail ?? 'Account created. You can now sign in.');
      setState('success');
    } catch (err: unknown) {
      setMessage(errorMessage(err, 'Could not create your account. The link may have expired.'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-6">
        <h1 className="text-2xl font-semibold text-gray-900">Set up your account</h1>

        {state === 'loading' && (
          <p className="text-sm text-gray-600">Checking your invitation…</p>
        )}

        {state === 'ready' && (
          <>
            <p className="text-sm text-gray-600">
              {patientName ? `Welcome, ${patientName}. ` : ''}
              Create a password to access your health record.
            </p>
            <form className="space-y-4" onSubmit={handleSubmit}>
              <div>
                <label htmlFor="email" className="block text-sm font-medium text-gray-700">Email</label>
                <input
                  id="email"
                  type="email"
                  value={email}
                  readOnly
                  className="mt-1 block w-full rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600"
                />
              </div>
              <div>
                <label htmlFor="password" className="block text-sm font-medium text-gray-700">Password</label>
                <input
                  id="password"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  placeholder="At least 8 characters"
                />
              </div>
              <div>
                <label htmlFor="confirm" className="block text-sm font-medium text-gray-700">Confirm password</label>
                <input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  placeholder="Re-enter your password"
                />
              </div>
              {message && (
                <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">{message}</p>
              )}
              <button
                type="submit"
                disabled={submitting}
                className="w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
              >
                {submitting ? 'Creating account…' : 'Create account'}
              </button>
            </form>
          </>
        )}

        {state === 'success' && (
          <>
            <p className="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-3">{message}</p>
            <button
              onClick={() => navigate('/login')}
              className="w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm font-medium"
            >
              Go to sign in
            </button>
          </>
        )}

        {state === 'error' && (
          <>
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-3">{message}</p>
            <button
              onClick={() => navigate('/login')}
              className="w-full py-2 px-4 border border-gray-300 rounded text-sm text-gray-700 hover:bg-gray-50"
            >
              Back to sign in
            </button>
          </>
        )}
      </div>
    </div>
  );
}
