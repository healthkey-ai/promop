import { useCallback, useRef, useState } from 'react';
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

const SOURCES = [
  { key: 'garmin', label: 'Garmin', accept: '.fit', enabled: true, multiple: true },
  { key: 'apple', label: 'Apple Health', accept: '.zip', enabled: true, multiple: false },
  { key: 'oura', label: 'Oura', accept: '', enabled: false, multiple: false },
  { key: 'fitbit', label: 'Fitbit', accept: '', enabled: false, multiple: false },
] as const;

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

export default function WearableTab({ formData, onRefresh }: Props) {
  const noData = !formData?.wearable_last_sync_at;
  const [showPicker, setShowPicker] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

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
  }, [onRefresh]);

  const handleSourceSelect = (sourceKey: string) => {
    const source = SOURCES.find(s => s.key === sourceKey);
    if (!source || !source.enabled) return;
    setSelectedSource(sourceKey);
    setShowPicker(false);
    if (fileInputRef.current) {
      fileInputRef.current.accept = source.accept;
      fileInputRef.current.multiple = source.multiple;
      fileInputRef.current.click();
    }
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0 || !selectedSource) return;
    await uploadFiles(Array.from(files), selectedSource);
    setSelectedSource(null);
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
    if (files.length === 0) return;

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
        <div className="relative">
          <button
            type="button"
            onClick={() => setShowPicker(!showPicker)}
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

          {showPicker && (
            <div className="absolute left-0 top-full z-10 mt-1 w-56 rounded-md border border-gray-200 bg-white py-1 shadow-lg">
              {SOURCES.map(source => (
                <button
                  key={source.key}
                  type="button"
                  onClick={() => handleSourceSelect(source.key)}
                  disabled={!source.enabled}
                  className={`flex w-full items-center justify-between px-4 py-2 text-left text-sm ${
                    source.enabled
                      ? 'text-gray-700 hover:bg-gray-50'
                      : 'cursor-not-allowed text-gray-400'
                  }`}
                >
                  <span>{source.label}</span>
                  {!source.enabled && (
                    <span className="text-xs text-gray-400">Coming soon</span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        <span className="text-xs text-gray-400">or drag &amp; drop files here</span>

        {uploadResult && (
          <span className="text-sm text-green-600">{uploadResult}</span>
        )}
        {uploadError && (
          <span className="text-sm text-red-600">{uploadError}</span>
        )}
      </div>

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        className="hidden"
        onChange={handleFileChange}
      />

      {noData && (
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
    </div>
  );
}
