import { useState } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import axios from 'axios';

type State = 'loading' | 'ready' | 'success' | 'error';

const publicApi = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

export default function AcceptInvite() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get('token') ?? '';

  const [state, setState] = useState<State>(token ? 'ready' : 'error');
  const [message, setMessage] = useState(token ? '' : 'No invitation token found in this link.');
  const [confirming, setConfirming] = useState(false);

  const handleAccept = async () => {
    setConfirming(true);
    try {
      const res = await publicApi.post('/orgs/confirm-invitation/', { token });
      setMessage(res.data.detail ?? 'Invitation accepted.');
      setState('success');
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
          : undefined;
      setMessage(msg ?? 'Failed to accept invitation. The link may have expired or already been used.');
      setState('error');
    } finally {
      setConfirming(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-6">
        <h1 className="text-2xl font-semibold text-gray-900">Accept Invitation</h1>

        {state === 'ready' && (
          <>
            <p className="text-sm text-gray-600">
              Click below to accept this invitation and gain access.
            </p>
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
            <button
              onClick={() => navigate('/')}
              className="w-full py-2 px-4 bg-blue-600 text-white rounded hover:bg-blue-700 text-sm font-medium"
            >
              Go to PROMOP
            </button>
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
