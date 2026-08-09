import { useCallback, useEffect, useRef, useState } from 'react';
import Field from '../Field';
import Section from '../Section';
import api from '@/api/axios';

const ACTIVITY_TREND_OPTIONS = ['improving', 'stable', 'declining', 'insufficient_data'];

interface Props {
  formData: Record<string, unknown>;
  // onChange is accepted for API consistency with other tabs but not invoked —
  // all wearable fields are read-only (derived from OMOP, not user-editable).
  onChange?: (field: string, value: unknown) => void;
  onRefresh?: () => void;
}

const METRIC_LABELS: Record<string, string> = {
  steps: 'Steps',
  active_minutes: 'Active Minutes',
  resting_hr: 'Resting Heart Rate',
  hrv_sdnn: 'HRV SDNN',
  spo2: 'SpO\u2082',
  respiratory_rate: 'Respiratory Rate',
  sleep_duration: 'Sleep Duration',
  vo2_max: 'VO\u2082 Max',
  distance: 'Distance',
  walking_speed: 'Walking Speed',
  walking_step_length: 'Step Length',
  walking_double_support_pct: 'Double Support %',
  walking_hr_avg: 'Walking HR',
  flights_climbed: 'Flights Climbed',
  active_energy: 'Active Energy',
  basal_energy: 'Basal Energy',
  body_mass: 'Body Mass',
};

interface UploadRecord {
  id: number;
  device_type: string;
  filename: string;
  samples_created: number;
  duplicates_skipped: number;
  sample_summary: { metric: string; date: string; value: number }[];
  uploaded_at: string;
}

function formatSyncDate(raw: unknown): string {
  if (!raw) return '';
  try {
    return new Date(raw as string).toLocaleString();
  } catch {
    return String(raw);
  }
}

/** Infer device type from file extension. Returns null for unsupported extensions. */
function detectDeviceType(name: string): 'garmin' | 'apple' | null {
  const lower = name.toLowerCase();
  if (lower.endsWith('.fit')) return 'garmin';
  if (lower.endsWith('.zip')) return 'apple';
  return null;
}

function formatMetricValue(metric: string, value: number): string {
  if (metric === 'steps') return value.toLocaleString();
  if (metric === 'active_minutes') return `${value.toFixed(0)} min`;
  if (metric === 'resting_hr') return `${value.toFixed(0)} bpm`;
  if (metric === 'hrv_sdnn') return `${value.toFixed(1)} ms`;
  if (metric === 'spo2') return `${value.toFixed(1)}%`;
  if (metric === 'respiratory_rate') return `${value.toFixed(1)} breaths/min`;
  if (metric === 'sleep_duration') return `${value.toFixed(1)} hrs`;
  if (metric === 'vo2_max') return `${value.toFixed(1)} mL/kg/min`;
  if (metric === 'distance') return `${value.toFixed(2)} km`;
  if (metric === 'walking_speed') return `${value.toFixed(2)} km/hr`;
  if (metric === 'walking_step_length') return `${value.toFixed(1)} cm`;
  if (metric === 'walking_double_support_pct') return `${value.toFixed(1)}%`;
  if (metric === 'walking_hr_avg') return `${value.toFixed(0)} bpm`;
  if (metric === 'flights_climbed') return `${value.toFixed(0)}`;
  if (metric === 'active_energy') return `${value.toFixed(0)} kcal`;
  if (metric === 'basal_energy') return `${value.toFixed(0)} kcal`;
  if (metric === 'body_mass') return `${value.toFixed(1)} kg`;
  return String(value);
}

export default function WearableTab({ formData, onRefresh }: Props) {
  const noData = !formData?.wearable_last_sync_at;
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

  // Upload history
  const [uploads, setUploads] = useState<UploadRecord[]>([]);
  const [uploadsLoading, setUploadsLoading] = useState(true);
  const [expandedUpload, setExpandedUpload] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const fetchUploads = useCallback(async () => {
    try {
      const res = await api.get('/v1/patient-records/wearable-uploads/');
      setUploads(res.data);
    } catch {
      // silently fail — upload history is non-critical
    } finally {
      setUploadsLoading(false);
    }
  }, []);

  // Load history on mount. The fetch is scoped to the effect and ignores a
  // late response after unmount — calling fetchUploads() from the effect body
  // reads to the linter as a synchronous setState, and left a real window
  // where a slow request could resolve against a gone component.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get('/v1/patient-records/wearable-uploads/');
        if (!cancelled) setUploads(res.data);
      } catch {
        // silently fail — upload history is non-critical
      } finally {
        if (!cancelled) setUploadsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleDelete = useCallback(async (uploadId: number) => {
    if (!confirm('Delete this upload and its associated measurements?')) return;
    setDeletingId(uploadId);
    try {
      await api.delete(`/v1/patient-records/wearable-uploads/${uploadId}/`);
      setUploads(prev => prev.filter(u => u.id !== uploadId));
      if (expandedUpload === uploadId) setExpandedUpload(null);
      if (onRefresh) onRefresh();
    } catch {
      setUploadError('Failed to delete upload. Please try again.');
    } finally {
      setDeletingId(null);
    }
  }, [expandedUpload, onRefresh]);

  /** Upload a list of files with a given device type. Shared by file-picker and drag-and-drop. */
  const uploadFiles = useCallback(async (files: File[], deviceType: string) => {
    setUploading(true);
    setUploadResult(null);
    setUploadError(null);

    let totalCreated = 0;
    let totalDuplicates = 0;

    try {
      for (const file of files) {
        const payload = new FormData();
        payload.append('file', file);
        payload.append('device_type', deviceType);

        const res = await api.post('/v1/patient-records/upload-wearable/', payload, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });

        totalCreated += res.data.samples_created || 0;
        totalDuplicates += res.data.duplicates_skipped || 0;
      }

      setUploadResult(
        `Uploaded ${totalCreated} sample${totalCreated !== 1 ? 's' : ''}` +
        (totalDuplicates > 0 ? ` (${totalDuplicates} duplicate${totalDuplicates !== 1 ? 's' : ''} skipped)` : '')
      );

      // Refresh upload history and patient record
      fetchUploads();
      if (totalCreated > 0 && onRefresh) {
        onRefresh();
      }
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { error?: string } } })?.response?.data?.error
        || 'Upload failed. Please try again.';
      setUploadError(msg);
    } finally {
      setUploading(false);
    }
  }, [onRefresh, fetchUploads]);

  const handleUploadClick = () => {
    if (fileInputRef.current) {
      fileInputRef.current.click();
    }
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const fileList = Array.from(files);

    // Auto-detect device type from file extensions
    const types = new Set(fileList.map(f => detectDeviceType(f.name)));
    if (types.has(null)) {
      setUploadError('Unsupported file type. Please select .fit (Garmin) or .zip (Apple Health) files.');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }
    if (types.size > 1) {
      setUploadError('Please upload one type at a time (.fit or .zip).');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }

    const deviceType = [...types][0]!;
    await uploadFiles(fileList, deviceType);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  // ── Drag-and-drop handlers ──────────────────────────────────────────────

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current += 1;
    if (e.dataTransfer.types.includes('Files')) {
      setDragOver(true);
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current -= 1;
    if (dragCounter.current === 0) {
      setDragOver(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setDragOver(false);

    if (uploading) return;

    const files = Array.from(e.dataTransfer.files);

    if (files.length === 0) {
      // MTP file managers (MacDroid, OpenMTP) provide URI/HTML references
      // instead of File objects — the browser can't access MTP files directly.
      const uriList = e.dataTransfer.getData('text/uri-list');
      const html = e.dataTransfer.getData('text/html');

      if (uriList || html) {
        setUploadError(
          'Files dragged from the Garmin MTP device cannot be read directly by the browser. ' +
          'Please copy the files to a local folder first (e.g. Desktop), then drag them from there.'
        );
      }
      return;
    }

    // Detect device type from extensions — all files must be the same type
    const types = new Set(files.map(f => detectDeviceType(f.name)));

    if (types.has(null)) {
      setUploadError('Unsupported file type. Please drop .fit (Garmin) or .zip (Apple Health) files.');
      return;
    }

    if (types.size > 1) {
      setUploadError('Please drop files of one type at a time (.fit or .zip, not both).');
      return;
    }

    const deviceType = types.values().next().value as string;
    await uploadFiles(files, deviceType);
  }, [uploading, uploadFiles]);

  return (
    <div
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      className="relative"
    >
      {/* Drop zone overlay */}
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-lg border-2 border-dashed border-portal-brand bg-portal-brand/5">
          <div className="flex flex-col items-center gap-2 text-portal-brand">
            <svg className="h-10 w-10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5-5m0 0l5 5m-5-5v12" />
            </svg>
            <span className="text-sm font-medium">Drop .fit or .zip files to upload</span>
          </div>
        </div>
      )}

      {/* Upload controls */}
      <div className="mb-4 flex items-center gap-3">
        <button
          type="button"
          onClick={handleUploadClick}
          disabled={uploading}
          className="inline-flex items-center gap-2 rounded-md bg-portal-brand px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-portal-brand/90 disabled:opacity-50"
        >
          {uploading ? (
            <>
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Uploading...
            </>
          ) : (
            <>
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2M7 10l5-5m0 0l5 5m-5-5v12" />
              </svg>
              Upload
            </>
          )}
        </button>

        <span className="text-xs text-gray-400">or drag &amp; drop files here</span>

        {uploadResult && (
          <span className="text-sm text-green-600">{uploadResult}</span>
        )}
        {uploadError && (
          <span className="text-sm text-red-600">{uploadError}</span>
        )}
      </div>

      {/* Hidden file input — accepts both .fit and .zip; device type auto-detected */}
      <input
        ref={fileInputRef}
        type="file"
        accept=".fit,.zip"
        multiple
        className="hidden"
        onChange={handleFileChange}
      />

      {/* Upload history */}
      {!uploadsLoading && uploads.length > 0 && (
        <Section title="Upload History">
          <div className="divide-y divide-gray-100">
            {uploads.map(upload => (
              <div key={upload.id}>
                <button
                  type="button"
                  onClick={() => setExpandedUpload(expandedUpload === upload.id ? null : upload.id)}
                  className="flex w-full items-center gap-3 px-2 py-2.5 text-left text-sm hover:bg-gray-50 rounded"
                >
                  <span className="inline-flex items-center rounded bg-gray-100 px-1.5 py-0.5 text-xs font-medium text-gray-600">
                    {upload.device_type === 'garmin' ? 'Garmin' : 'Apple'}
                  </span>
                  <span className="flex-1 truncate text-gray-700">{upload.filename}</span>
                  <span className="text-xs text-gray-500">
                    {upload.samples_created} sample{upload.samples_created !== 1 ? 's' : ''}
                  </span>
                  <span className="text-xs text-gray-400">
                    {new Date(upload.uploaded_at).toLocaleDateString()}
                  </span>
                  <svg
                    className={`h-4 w-4 text-gray-400 transition-transform ${expandedUpload === upload.id ? 'rotate-180' : ''}`}
                    fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); handleDelete(upload.id); }}
                  disabled={deletingId === upload.id}
                  className="ml-1 rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-50"
                  title="Delete upload"
                >
                  {deletingId === upload.id ? (
                    <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                  ) : (
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  )}
                </button>

                {expandedUpload === upload.id && upload.sample_summary.length > 0 && (
                  <div className="mb-2 ml-2 rounded border border-gray-100 bg-gray-50">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-gray-200 text-left text-xs text-gray-500">
                          <th className="px-3 py-1.5 font-medium">Metric</th>
                          <th className="px-3 py-1.5 font-medium">Date</th>
                          <th className="px-3 py-1.5 font-medium text-right">Value</th>
                        </tr>
                      </thead>
                      <tbody>
                        {upload.sample_summary.map((sample, idx) => (
                          <tr key={idx} className="border-b border-gray-100 last:border-0">
                            <td className="px-3 py-1.5 text-gray-700">
                              {METRIC_LABELS[sample.metric] || sample.metric}
                            </td>
                            <td className="px-3 py-1.5 text-gray-500">{sample.date}</td>
                            <td className="px-3 py-1.5 text-right text-gray-700">
                              {formatMetricValue(sample.metric, sample.value)}
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
        </Section>
      )}

      {noData && !uploadsLoading && uploads.length === 0 && (
        <p className="mb-4 text-sm text-gray-500 italic">
          No wearable data synced yet. Upload or drag &amp; drop wearable data files to contribute to your record.
        </p>
      )}

      <Section title="Data Coverage">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Last Sync</label>
            <p className="text-sm text-gray-700 py-1.5">
              {formatSyncDate(formData?.wearable_last_sync_at) || '\u2014'}
            </p>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">
              Coverage Ratio (30 days)
            </label>
            <p className="text-sm text-gray-700 py-1.5">
              {formData?.wearable_coverage_ratio_30d != null
                ? `${(Number(formData.wearable_coverage_ratio_30d) * 100).toFixed(0)}%`
                : '\u2014'}
            </p>
          </div>
        </div>
      </Section>

      <Section title="Activity (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Median Daily Steps"
            name="median_daily_steps_30d"
            type="number"
            value={formData?.median_daily_steps_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Active Minutes / Day"
            name="active_minutes_per_day_30d"
            type="number"
            value={formData?.active_minutes_per_day_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Activity Trend"
            name="activity_trend_30d"
            type="select"
            value={formData?.activity_trend_30d}
            options={ACTIVITY_TREND_OPTIONS}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Cardiovascular (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Resting Heart Rate (bpm)"
            name="resting_heart_rate_avg_30d"
            type="number"
            value={formData?.resting_heart_rate_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="HRV SDNN (ms)"
            name="hrv_sdnn_avg_30d"
            type="number"
            value={formData?.hrv_sdnn_avg_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Respiratory (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Min SpO&#8322; (%)"
            name="oxygen_saturation_min_30d"
            type="number"
            value={formData?.oxygen_saturation_min_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Avg SpO&#8322; (%)"
            name="oxygen_saturation_avg_30d"
            type="number"
            value={formData?.oxygen_saturation_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Respiratory Rate (breaths/min)"
            name="respiratory_rate_avg_30d"
            type="number"
            value={formData?.respiratory_rate_avg_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Sleep (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Avg Sleep Duration (hours)"
            name="sleep_duration_hours_avg_30d"
            type="number"
            value={formData?.sleep_duration_hours_avg_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Fitness (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="VO&#8322; Max (mL/kg/min)"
            name="vo2_max_avg_30d"
            type="number"
            value={formData?.vo2_max_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Distance (km/day)"
            name="distance_km_per_day_30d"
            type="number"
            value={formData?.distance_km_per_day_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Flights Climbed / Day"
            name="flights_climbed_per_day_30d"
            type="number"
            value={formData?.flights_climbed_per_day_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Gait & Mobility (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Walking Speed (km/hr)"
            name="walking_speed_avg_30d"
            type="number"
            value={formData?.walking_speed_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Step Length (cm)"
            name="walking_step_length_avg_30d"
            type="number"
            value={formData?.walking_step_length_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Double Support (%)"
            name="walking_double_support_pct_avg_30d"
            type="number"
            value={formData?.walking_double_support_pct_avg_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Walking Heart Rate (bpm)"
            name="walking_hr_avg_30d"
            type="number"
            value={formData?.walking_hr_avg_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>

      <Section title="Energy & Body (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <Field
            label="Active Energy (kcal/day)"
            name="active_energy_per_day_30d"
            type="number"
            value={formData?.active_energy_per_day_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Basal Energy (kcal/day)"
            name="basal_energy_per_day_30d"
            type="number"
            value={formData?.basal_energy_per_day_30d}
            onChange={() => {}}
            disabled
          />
          <Field
            label="Body Mass (kg)"
            name="body_mass_avg_30d"
            type="number"
            value={formData?.body_mass_avg_30d}
            onChange={() => {}}
            disabled
          />
        </div>
      </Section>
    </div>
  );
}
