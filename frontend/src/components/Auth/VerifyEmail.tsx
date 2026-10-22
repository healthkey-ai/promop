import PageTitle from '@/components/Branding/PageTitle';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { publicApi } from '@/api/publicAxios';

type State = 'checking' | 'success' | 'error';

/** Landing page for the link in the "confirm your email address" message. */
export default function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';
  const [state, setState] = useState<State>(token ? 'checking' : 'error');
  const [message, setMessage] = useState(token ? '' : 'This verification link is missing information.');

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await publicApi.post('/v1/auth/verify-email/', { token });
        if (cancelled) return;
        setMessage(res.data.detail ?? 'Your email address is confirmed.');
        setState('success');
      } catch (err: unknown) {
        if (cancelled) return;
        const detail = (err as { response?: { data?: { error?: string } } })?.response?.data?.error;
        setMessage(detail ?? 'This verification link is invalid or has expired.');
        setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8 max-w-md w-full space-y-4">
        <PageTitle className="text-2xl font-semibold text-gray-900">Confirm your email address</PageTitle>
        {state === 'checking' && <p className="text-sm text-gray-600">Checking your link…</p>}
        {state !== 'checking' && (
          <p role="status" className={`text-sm ${state === 'success' ? 'text-green-700' : 'text-red-700'}`}>
            {message}
          </p>
        )}
        {state === 'success' && (
          <a href="/" className="inline-block text-sm font-medium text-primary hover:underline">
            Continue to PROMOP
          </a>
        )}
        {state === 'error' && (
          <p className="text-sm text-gray-600">
            Sign in and use “Resend email” in the banner at the top of the page to get a new link.
          </p>
        )}
      </div>
    </div>
  );
}
