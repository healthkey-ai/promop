import { useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { publicApi } from '@/api/publicAxios';

type State = 'ready' | 'success' | 'error';

const MIN_PASSWORD_LENGTH = 12;

function errorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const msg = (err as { response?: { data?: { error?: string } } }).response?.data?.error;
    if (msg) return msg;
  }
  return fallback;
}

export default function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const uid = params.get('uid') ?? '';
  const token = params.get('token') ?? '';

  const hasLink = Boolean(uid && token);
  const [state, setState] = useState<State>(hasLink ? 'ready' : 'error');
  const [message, setMessage] = useState(hasLink ? '' : 'This reset link is missing information.');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);

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
      const res = await publicApi.post('/v1/auth/reset-password/', { uid, token, new_password: password });
      setMessage(res.data.detail ?? 'Password has been reset.');
      setState('success');
    } catch (err: unknown) {
      setMessage(errorMessage(err, 'Could not reset your password. The link may have expired.'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-6">
        <h1 className="text-2xl font-semibold text-gray-900">Reset your password</h1>

        {state === 'ready' && (
          <form className="space-y-4" onSubmit={handleSubmit}>
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-700">New password</label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
                placeholder="At least 12 characters"
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
              {submitting ? 'Resetting…' : 'Reset password'}
            </button>
          </form>
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
