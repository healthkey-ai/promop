import { useState } from 'react';
import api from '@/api/axios';

const MIN_PASSWORD_LENGTH = 12;

function errorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const msg = (err as { response?: { data?: { error?: string } } }).response?.data?.error;
    if (msg) return msg;
  }
  return fallback;
}

interface Props {
  /** Called after the password is changed so the app can re-fetch the (now
   *  cleared) force-change flag and resume normal routing. */
  onChanged: () => void;
  onLogout: () => void;
}

/**
 * Blocking change-password screen shown when the account is flagged
 * must_change_password (PHR-S FM TI.1.1#09). The backend refuses every other
 * /api/ request until the password is changed, so this is a hard gate.
 */
export default function ChangePassword({ onChanged, onLogout }: Props) {
  const [current, setCurrent] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [message, setMessage] = useState('');
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
      await api.post('/auth/change-password/', { current_password: current, new_password: password });
      onChanged();
    } catch (err: unknown) {
      setMessage(errorMessage(err, 'Could not change your password.'));
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-6">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">Choose a new password</h1>
          <p className="mt-2 text-sm text-gray-600">
            Your account requires a password change before you can continue.
          </p>
        </div>

        <form className="space-y-4" onSubmit={handleSubmit}>
          <div>
            <label htmlFor="current" className="block text-sm font-medium text-gray-700">Current password</label>
            <input
              id="current"
              type="password"
              autoComplete="current-password"
              required
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div>
            <label htmlFor="new-password" className="block text-sm font-medium text-gray-700">New password</label>
            <input
              id="new-password"
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
            <label htmlFor="confirm" className="block text-sm font-medium text-gray-700">Confirm new password</label>
            <input
              id="confirm"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              placeholder="Re-enter your new password"
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
            {submitting ? 'Saving…' : 'Change password'}
          </button>
        </form>

        <button
          onClick={onLogout}
          className="w-full py-2 px-4 border border-gray-300 rounded text-sm text-gray-700 hover:bg-gray-50"
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
