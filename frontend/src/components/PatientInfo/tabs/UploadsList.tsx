import { useId, useState } from 'react';

/** One upload as the server lists it (`GET patient-records/wearable-uploads/`). */
export interface UploadRecord {
  id: number;
  device_type: string;
  filename: string;
  samples_created: number;
  duplicates_skipped: number;
  sample_summary: { metric: string; date: string; value: number }[];
  uploaded_at: string;
}

interface Props {
  uploads: UploadRecord[];
  /** What the upload history calls a device, for the badge on each row. */
  deviceLabel: (deviceType: string) => string;
  /** The label of a metric key, and how its value reads. */
  metricLabel: (metric: string) => string;
  formatValue: (metric: string, value: number) => string;
  expandedUpload: number | null;
  onToggleUpload: (id: number) => void;
  onDelete: (id: number) => void;
  deletingId: number | null;
  /** True while a sync is running: a delete waits for it. */
  busy: boolean;
  /** Names the section it sits in, for the test ids and the accessible name. */
  name: string;
}

/**
 * The uploads of one section, collapsed until opened (#2861): a closed
 * "Uploads (N)" row, which opens to the list; each upload opens to its
 * samples and can be deleted, as the one history list did before. Renders
 * nothing when the section has none.
 */
export default function UploadsList({
  uploads, deviceLabel, metricLabel, formatValue, expandedUpload, onToggleUpload,
  onDelete, deletingId, busy, name,
}: Props) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (uploads.length === 0) return null;

  return (
    <div className="py-2" data-testid={`uploads-${name}`}>
      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        aria-expanded={open}
        aria-controls={listId}
        className="flex w-full items-center gap-2 rounded px-1 py-1.5 text-left text-sm font-medium text-portal-text-primary hover:bg-gray-50"
      >
        <svg
          aria-hidden="true"
          className={`h-4 w-4 text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span>Uploads</span>{' '}
        <span className="text-xs font-normal text-gray-500">({uploads.length})</span>
      </button>

      {open && (
        <div id={listId} className="mt-1 divide-y divide-gray-100">
          {uploads.map(upload => (
            <div key={upload.id}>
              <div className="flex items-center">
                <button
                  type="button"
                  onClick={() => onToggleUpload(upload.id)}
                  aria-expanded={expandedUpload === upload.id}
                  className="flex min-w-0 flex-1 items-center gap-3 rounded px-2 py-2.5 text-left text-sm hover:bg-gray-50"
                >
                  <span className="inline-flex items-center rounded bg-gray-100 px-1.5 py-0.5 text-xs font-medium text-gray-600">
                    {deviceLabel(upload.device_type)}
                  </span>
                  <span className="flex-1 truncate text-gray-700">{upload.filename}</span>
                  <span className="text-xs text-gray-500">
                    {upload.samples_created} sample{upload.samples_created !== 1 ? 's' : ''}
                  </span>
                  <span className="text-xs text-gray-400">
                    {new Date(upload.uploaded_at).toLocaleDateString()}
                  </span>
                  <svg
                    aria-hidden="true"
                    className={`h-4 w-4 text-gray-400 transition-transform ${expandedUpload === upload.id ? 'rotate-180' : ''}`}
                    fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(upload.id)}
                  disabled={deletingId === upload.id || busy}
                  className="ml-1 rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-50"
                  title="Delete upload"
                  aria-label={`Delete upload ${upload.filename}`}
                >
                  {deletingId === upload.id ? (
                    <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  ) : (
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  )}
                </button>
              </div>

              {expandedUpload === upload.id && upload.sample_summary.length > 0 && (
                <div className="mb-2 ml-2 rounded border border-gray-100 bg-gray-50">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-200 text-left text-xs text-gray-500">
                        <th className="px-3 py-1.5 font-medium">Metric</th>
                        <th className="px-3 py-1.5 font-medium">Date</th>
                        <th className="px-3 py-1.5 text-right font-medium">Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {upload.sample_summary.map((sample, idx) => (
                        <tr key={idx} className="border-b border-gray-100 last:border-0">
                          <td className="px-3 py-1.5 text-gray-700">{metricLabel(sample.metric)}</td>
                          <td className="px-3 py-1.5 text-gray-500">{sample.date}</td>
                          <td className="px-3 py-1.5 text-right text-gray-700">
                            {formatValue(sample.metric, sample.value)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
