import { useState } from 'react';

/**
 * "Confirm all N provisional values" for a professional who may confirm a patient's
 * proposals (#2870). Bulk, so it asks once, inline: the first press opens a one-line
 * question, the second does it. The page never decides which values qualify: `count` is
 * the number of marked fields the server listed as confirmable, and the server is the one
 * that skips what it cannot confirm.
 */
export default function ConfirmProvisionalBanner({
  count,
  busy,
  note,
  onConfirmAll,
}: {
  count: number;
  /** A confirmation is running (a field's, or `'*'` for all). */
  busy: boolean;
  /** What the last confirmation did or why it did not, or null. */
  note: { text: string; failed: boolean } | null;
  onConfirmAll: () => void;
}) {
  const [asking, setAsking] = useState(false);
  if (count === 0 && !note) return null;
  const values = `${count} provisional ${count === 1 ? 'value' : 'values'}`;
  return (
    <div
      role="region"
      aria-label="Provisional values"
      className="mx-8 mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
    >
      {count > 0 && !asking && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>
            {count === 1 ? 'A value on this record is' : `${count} values on this record are`} provisional
            (from a code mapping that is not approved yet).
          </span>
          <button
            type="button"
            className="rounded border border-amber-400 bg-white px-2.5 py-1 text-xs font-medium hover:bg-amber-100 disabled:opacity-50"
            disabled={busy}
            onClick={() => setAsking(true)}
          >
            Confirm all {values}
          </button>
        </div>
      )}
      {count > 0 && asking && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span>Save all {values} as approved data for this patient?</span>
          <span className="flex gap-2">
            <button
              type="button"
              className="rounded border border-amber-500 bg-amber-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
              disabled={busy}
              onClick={() => { setAsking(false); onConfirmAll(); }}
            >
              Yes, confirm all {count}
            </button>
            <button
              type="button"
              className="rounded border border-amber-300 bg-white px-2.5 py-1 text-xs font-medium hover:bg-amber-100"
              onClick={() => setAsking(false)}
            >
              Cancel
            </button>
          </span>
        </div>
      )}
      {note && (
        <p role={note.failed ? 'alert' : 'status'} className={`${count > 0 ? 'mt-2 ' : ''}${note.failed ? 'text-red-700' : ''}`}>
          {note.text}
        </p>
      )}
    </div>
  );
}
