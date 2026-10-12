import { useState } from 'react';
import api from '@/api/axios';

/**
 * Shown to a signed-in user whose address is not confirmed. Nothing is blocked:
 * the account works. Only access that depends on the address — an organization
 * that trusts the email domain — waits for the link to be followed.
 */
export default function VerifyEmailBanner({ email }: { email?: string }) {
  const [status, setStatus] = useState<'idle' | 'sending' | 'sent' | 'failed'>('idle');

  const resend = async () => {
    setStatus('sending');
    try {
      await api.post('/v1/auth/verify-email/resend/');
      setStatus('sent');
    } catch {
      setStatus('failed');
    }
  };

  return (
    <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
      <span>
        Confirm your email address{email ? ` (${email})` : ''}. Use the emailed link to confirm it. Access that depends on
        your email address stays off until you do.
      </span>
      <button
        type="button"
        onClick={resend}
        disabled={status === 'sending' || status === 'sent'}
        className="font-medium underline disabled:no-underline disabled:opacity-60"
      >
        {status === 'sent' ? 'Email sent' : status === 'sending' ? 'Sending…' : 'Resend email'}
      </button>
      {status === 'failed' && <span className="text-red-700">Could not send. Try again shortly.</span>}
    </div>
  );
}
