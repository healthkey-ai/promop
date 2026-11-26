import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useWritableFields } from '@/hooks/useWritableFields';
import { MetricField } from './WearableMetricTools';
import { WearableMetricProvider } from './wearableMetricContext';
import type { OmopFocus } from '@/components/Patient/omopRows';
import ProvisionalBadge from '../ProvisionalBadge';
import Section from '../Section';
import GarminAccountLink from './GarminAccountLink';
import GarminConnectSync from './GarminConnectSync';
import OpenWearablesLink from './OpenWearablesLink';
import OuraAccountLink from './OuraAccountLink';
import OuraUpload from './OuraUpload';
import UploadsList, { type UploadRecord } from './UploadsList';
import { pendingClause } from './wearableResult';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';

const ACTIVITY_TREND_OPTIONS = ['improving', 'stable', 'declining', 'insufficient_data'];

interface Props {
  formData: Record<string, unknown>;
  // onChange is accepted for API consistency with other tabs but not invoked —
  // all wearable fields are read-only (derived from OMOP, not user-editable).
  onChange?: (field: string, value: unknown) => void;
  onRefresh?: () => void;
  /**
   * True when the signed-in person is the patient of this record. A Garmin
   * Connect sync is filed under whoever is signed in, so the panel is offered
   * nowhere else: not to a clinician on a patient's page, not in a read-only
   * embed.
   */
  canSyncOwnDevice?: boolean;
  /**
   * Opens the OMOP tab at a concept group. Passed only where that tab is
   * offered (staff and org admins outside patient mode); without it a 30-day
   * value has its chart and no link to rows (#2887).
   */
  onOpenOmopRows?: (focus: OmopFocus) => void;
  /**
   * True when the page is the signed-in person's own record. A file upload,
   * the upload history and its deletes all act on the caller's own record,
   * wherever they are used from, so they are offered nowhere else (#2819):
   * on somebody else's page an upload would land on the viewer's record
   * while the page reported it on the patient's.
   */
  ownRecord?: boolean;
}

const METRIC_LABELS: Record<string, string> = {
  steps: 'Steps',
  active_minutes: 'Active Minutes',
  resting_hr: 'Resting Heart Rate',
  hrv_sdnn: 'HRV SDNN',
  hrv_rmssd: 'HRV RMSSD',
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

/** What the upload history calls the device an upload came from. */
const DEVICE_LABELS: Record<string, string> = {
  garmin: 'Garmin',
  apple: 'Apple',
  android: 'Android',
  oura: 'Oura',
};

/** A device type the list does not name is shown as the server sent it. */
function deviceLabel(deviceType: string): string {
  return Object.prototype.hasOwnProperty.call(DEVICE_LABELS, deviceType) ? DEVICE_LABELS[deviceType] : deviceType;
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
  if (metric === 'hrv_rmssd') return `${value.toFixed(1)} ms`;
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

export default function WearableTab({ formData, onRefresh, canSyncOwnDevice = false, ownRecord = false, onOpenOmopRows }: Props) {
  // Ask about *this* patient: whether a field may be edited depends on who is
  // asking and whose record it is, not only on whether the field is mapped.
  const personId = (formData?.person_id ?? formData?.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const noData = !formData?.wearable_last_sync_at;
  const garminHeadingId = useId();
  const [uploading, setUploading] = useState(false);
  // A Garmin Connect sync in flight: uploads and deletes wait for it, as it
  // waits for them.
  const [syncing, setSyncing] = useState(false);
  const [uploadResult, setUploadResult] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Which section's upload the result and error above belong to, so each is
  // shown beside the button that was used.
  const [messageAt, setMessageAt] = useState<'top' | 'oura'>('top');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragCounter = useRef(0);

  // Upload history
  const [uploads, setUploads] = useState<UploadRecord[]>([]);
  // The history is the caller's own, so it is read only on their own record.
  const [uploadsLoading, setUploadsLoading] = useState(ownRecord);
  const [expandedUpload, setExpandedUpload] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  // Whether the phone-app panel is on the page: it offers nothing, and draws
  // nothing, where connecting a phone is not set up. Null until it has asked.
  const [phonePanelShown, setPhonePanelShown] = useState<boolean | null>(null);

  const fetchUploads = useCallback(async () => {
    try {
      const res = await clinicalClient().get(clinicalUrl('/v1/patient-records/wearable-uploads/'));
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
    if (!ownRecord) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const res = await clinicalClient().get(clinicalUrl('/v1/patient-records/wearable-uploads/'));
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
  }, [ownRecord]);

  const handleDelete = useCallback(async (uploadId: number) => {
    if (!confirm('Delete this upload and its associated measurements?')) return;
    setDeletingId(uploadId);
    try {
      await clinicalClient().delete(clinicalUrl(`/v1/patient-records/wearable-uploads/${uploadId}/`));
      setUploads(prev => prev.filter(u => u.id !== uploadId));
      if (expandedUpload === uploadId) setExpandedUpload(null);
      if (onRefresh) onRefresh();
    } catch {
      // A refusal is told in the section the upload is listed in.
      setMessageAt(uploads.find(u => u.id === uploadId)?.device_type === 'oura' ? 'oura' : 'top');
      setUploadResult(null);
      setUploadError('Failed to delete upload. Please try again.');
    } finally {
      setDeletingId(null);
    }
  }, [expandedUpload, onRefresh, uploads]);

  /** Upload a list of files with a given device type. Shared by file-picker and drag-and-drop. */
  const uploadFiles = useCallback(async (files: File[], deviceType: string, at: 'top' | 'oura' = 'top') => {
    setUploading(true);
    setMessageAt(at);
    setUploadResult(null);
    setUploadError(null);

    let totalCreated = 0;
    let totalDuplicates = 0;
    let totalProvisional = 0;
    let totalUnresolved = 0;
    let totalSettled = 0;
    let storedFiles = 0;

    try {
      for (const file of files) {
        const payload = new FormData();
        payload.append('file', file);
        payload.append('device_type', deviceType);
        // The record this page shows: the server refuses one that is not the
        // caller's own, so an upload is never filed under somebody else's page.
        if (personId != null) payload.append('person_id', String(personId));

        const res = await clinicalClient().post(clinicalUrl('/v1/patient-records/upload-wearable/'), payload, {
          headers: { 'Content-Type': 'multipart/form-data' },
        });

        totalCreated += res.data.samples_created || 0;
        totalDuplicates += res.data.duplicates_skipped || 0;
        totalProvisional += res.data.provisional_samples || 0;
        totalUnresolved += res.data.unresolved_samples || 0;
        totalSettled += res.data.settled_rows || 0;
        storedFiles += 1;
      }

      const pending = pendingClause({
        provisional_samples: totalProvisional, unresolved_samples: totalUnresolved,
      });
      setUploadResult(
        `Uploaded ${totalCreated} sample${totalCreated !== 1 ? 's' : ''}` +
        (totalDuplicates > 0 ? ` (${totalDuplicates} duplicate${totalDuplicates !== 1 ? 's' : ''} skipped)` : '') +
        (pending ? `. ${pending}` : '')
      );

      // Refresh upload history and patient record. The record is re-read when
      // rows were stored, and also when the upload settled earlier readings
      // whose mapping has been approved or proposed since (#2835).
      fetchUploads();
      if ((totalCreated > 0 || totalSettled > 0) && onRefresh) {
        onRefresh();
      }
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { error?: string } } })?.response?.data?.error
        || 'Upload failed. Please try again.';
      setUploadError(storedFiles > 0
        ? `${msg} ${storedFiles} of ${files.length} files were stored before it failed.` : msg);
      if (storedFiles > 0) {
        fetchUploads();
        if (onRefresh) onRefresh();
      }
    } finally {
      setUploading(false);
    }
  }, [onRefresh, fetchUploads, personId]);

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
      setMessageAt('top');
      setUploadResult(null);
      setUploadError('Unsupported file type. Please select .fit (Garmin) or .zip (Apple Health) files.');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }
    if (types.size > 1) {
      setMessageAt('top');
      setUploadResult(null);
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
    if (ownRecord && e.dataTransfer.types.includes('Files')) {
      setDragOver(true);
    }
  }, [ownRecord]);

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

    if (!ownRecord || uploading || syncing) return;

    const files = Array.from(e.dataTransfer.files);

    if (files.length === 0) {
      // MTP file managers (MacDroid, OpenMTP) provide URI/HTML references
      // instead of File objects — the browser can't access MTP files directly.
      const uriList = e.dataTransfer.getData('text/uri-list');
      const html = e.dataTransfer.getData('text/html');

      if (uriList || html) {
        setMessageAt('top');
        setUploadResult(null);
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
      setMessageAt('top');
      setUploadResult(null);
      setUploadError('Unsupported file type. Please drop .fit (Garmin) or .zip (Apple Health) files.');
      return;
    }

    if (types.size > 1) {
      setMessageAt('top');
      setUploadResult(null);
      setUploadError('Please drop files of one type at a time (.fit or .zip, not both).');
      return;
    }

    const deviceType = types.values().next().value as string;
    await uploadFiles(files, deviceType);
  }, [ownRecord, uploading, syncing, uploadFiles]);

  // Each upload sits in the section of its device (#2861): Garmin's in the
  // Garmin section, Apple Health's and Android's in the phone-app section.
  // One that has no such section on this page is not lost: it goes to a
  // collapsed block of its own.
  const PHONE_DEVICES = ['apple', 'android'];
  const garminUploads = canSyncOwnDevice ? uploads.filter(u => u.device_type === 'garmin') : [];
  const phoneUploads = canSyncOwnDevice && phonePanelShown
    ? uploads.filter(u => PHONE_DEVICES.includes(u.device_type))
    : [];
  // Oura's sit in the Oura section, which is offered wherever an upload is.
  const ouraUploads = ownRecord ? uploads.filter(u => u.device_type === 'oura') : [];
  const placed = new Set([...garminUploads, ...phoneUploads, ...ouraUploads].map(u => u.id));
  // The phone uploads wait while the panel has not said whether it is there,
  // rather than show here for a moment and then move.
  const waiting = canSyncOwnDevice && phonePanelShown === null;
  const leftoverUploads = uploads.filter(
    u => !placed.has(u.id) && !(waiting && PHONE_DEVICES.includes(u.device_type)));
  const uploadsProps = {
    deviceLabel,
    metricLabel: (metric: string) => METRIC_LABELS[metric] || metric,
    formatValue: formatMetricValue,
    expandedUpload,
    onToggleUpload: (id: number) => setExpandedUpload(expandedUpload === id ? null : id),
    onDelete: handleDelete,
    deletingId,
    busy: syncing,
  };

  return (
    <WearableMetricProvider value={{ personId, onOpenOmopRows }}>
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
      {ownRecord && <div className="mb-4 flex items-center gap-3">
        <button
          type="button"
          onClick={handleUploadClick}
          disabled={uploading || syncing}
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

        {messageAt === 'top' && uploadResult && (
          <span className="text-sm text-green-600">{uploadResult}</span>
        )}
        {messageAt === 'top' && uploadError && (
          <span className="text-sm text-red-600">{uploadError}</span>
        )}
      </div>}

      {/* Hidden file input — accepts both .fit and .zip; device type auto-detected */}
      {ownRecord && (
        <input
          ref={fileInputRef}
          type="file"
          accept=".fit,.zip"
          multiple
          className="hidden"
          onChange={handleFileChange}
        />
      )}

      {/* Somebody else's record: nothing here can add to it. */}
      {!ownRecord && (
        <p className="mb-4 text-sm text-gray-500">
          Wearable data is added by the patient, from their own record.
        </p>
      )}

      {/* Garmin, in one place (#2832): the account linked once, where this
          deployment has a Garmin app, after which Garmin sends each day by
          itself (#2802); and under it the sync by Personal API key, which
          reads past days through Garmin's API when asked (#2794). */}
      {canSyncOwnDevice && (
        <section aria-labelledby={garminHeadingId} className="mb-4 rounded-md border border-gray-200 px-4 pt-3 pb-2">
          <h3 id={garminHeadingId} className="text-sm font-medium text-portal-text-primary">Garmin</h3>
          <div className="divide-y divide-gray-200">
            <GarminAccountLink disabled={uploading || syncing || deletingId !== null} />
            <GarminConnectSync
              personId={personId}
              disabled={uploading || deletingId !== null}
              metricLabels={METRIC_LABELS}
              onBusyChange={setSyncing}
              onSynced={(changed) => {
                fetchUploads();
                if (changed && onRefresh) onRefresh();
              }}
            />
            {!uploadsLoading && <UploadsList {...uploadsProps} uploads={garminUploads} name="garmin" />}
          </div>
        </section>
      )}

      {/* Oura Ring: the account linked once (#2900), whose days this site reads,
          and under it the export file Oura's Membership Hub sends (#2889). */}
      {ownRecord && (
        <OuraUpload
          disabled={uploading || syncing || deletingId !== null}
          uploading={uploading && messageAt === 'oura'}
          result={messageAt === 'oura' ? uploadResult : null}
          error={messageAt === 'oura' ? uploadError : null}
          onFiles={(files) => void uploadFiles(files, 'oura', 'oura')}
          account={canSyncOwnDevice ? (
            <OuraAccountLink
              personId={personId}
              disabled={uploading || deletingId !== null}
              onBusyChange={setSyncing}
              onSynced={(changed) => {
                fetchUploads();
                if (changed && onRefresh) onRefresh();
              }}
            />
          ) : null}
        >
          {!uploadsLoading && <UploadsList {...uploadsProps} uploads={ouraUploads} name="oura" />}
        </OuraUpload>
      )}

      {/* A phone app built on Open Wearables, connected with a one-time code:
          it then sends what it reads from Apple Health or Health Connect. */}
      {canSyncOwnDevice && (
        <OpenWearablesLink
          disabled={uploading || syncing || deletingId !== null}
          onShownChange={setPhonePanelShown}
        >
          {!uploadsLoading && <UploadsList {...uploadsProps} uploads={phoneUploads} name="phone" />}
        </OpenWearablesLink>
      )}

      {/* Uploads with no section above to sit in (#2861): a device whose panel
          is not offered on this page, as on the embedded page. Collapsed, and
          still reachable, so no upload is hidden. */}
      {ownRecord && !uploadsLoading && leftoverUploads.length > 0 && (
        <div className="mb-4 rounded-md border border-gray-200 px-4">
          <UploadsList {...uploadsProps} uploads={leftoverUploads} name="other" />
        </div>
      )}

      {noData && !uploadsLoading && uploads.length === 0 && (
        <p className="mb-4 text-sm text-gray-500 italic">
          {ownRecord
            ? <>No wearable data synced yet. Upload or drag &amp; drop wearable data files to contribute to your record.</>
            : 'No wearable data synced yet.'}
        </p>
      )}

      <Section title="Data Coverage">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <label className="text-sm font-medium text-portal-text-primary">Last Sync</label>
              {/* Both are read from the same readings as the values below, so
                  they are provisional when all of those are (#2835). */}
              <ProvisionalBadge name="wearable_last_sync_at" fromDevice />
            </div>
            <p className="text-sm text-gray-700 py-1.5">
              {formatSyncDate(formData?.wearable_last_sync_at) || '\u2014'}
            </p>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <label className="text-sm font-medium text-portal-text-primary">
                Coverage Ratio (30 days)
              </label>
              <ProvisionalBadge name="wearable_coverage_ratio_30d" fromDevice />
            </div>
            <p className="text-sm text-gray-700 py-1.5">
              {formData?.wearable_coverage_ratio_30d != null
                ? `${(Number(formData.wearable_coverage_ratio_30d) * 100).toFixed(0)}%`
                : '\u2014'}
            </p>
          </div>
        </div>
      </Section>

      <div className="mb-5 rounded-md border border-border bg-muted/40 px-4 py-3">
        <p className="text-xs text-muted-foreground">
          Every value below is a 30-day aggregate over device readings, so none of
          them is typed in: a clinician does not enter a median.{' '}
          {ownRecord
            ? 'Upload device data above and these follow.'
            : 'They follow from the device data the patient adds.'}
          {' '}Each value is marked Accepted or Provisional. Provisional means the
          readings behind it have been stored and are not yet confirmed.
        </p>
      </div>

      <Section title="Activity (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Median Daily Steps"
            name="median_daily_steps_30d"
            descriptor={descriptors.median_daily_steps_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.median_daily_steps_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Active Minutes / Day"
            name="active_minutes_per_day_30d"
            descriptor={descriptors.active_minutes_per_day_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.active_minutes_per_day_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Activity Trend"
            name="activity_trend_30d"
            descriptor={descriptors.activity_trend_30d}
            showReason={false}
            // "Insufficient data" is the absence of a trend, not a reading to accept.
            showAccepted={formData?.activity_trend_30d !== 'insufficient_data'}
            type="select"
            value={formData?.activity_trend_30d}
            options={ACTIVITY_TREND_OPTIONS}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Cardiovascular (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Resting Heart Rate (bpm)"
            name="resting_heart_rate_avg_30d"
            descriptor={descriptors.resting_heart_rate_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.resting_heart_rate_avg_30d}
            onChange={() => {}}
          />
          {/* SDNN and RMSSD are distinct statistics and are shown as
              distinct fields. A patient normally has one or the other
              depending on their device, not both. */}
          <MetricField
            label="HRV SDNN (ms)"
            name="hrv_sdnn_avg_30d"
            descriptor={descriptors.hrv_sdnn_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.hrv_sdnn_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="HRV RMSSD (ms)"
            name="hrv_rmssd_avg_30d"
            descriptor={descriptors.hrv_rmssd_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.hrv_rmssd_avg_30d}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Respiratory (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Min SpO&#8322; (%)"
            name="oxygen_saturation_min_30d"
            descriptor={descriptors.oxygen_saturation_min_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.oxygen_saturation_min_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Avg SpO&#8322; (%)"
            name="oxygen_saturation_avg_30d"
            descriptor={descriptors.oxygen_saturation_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.oxygen_saturation_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Respiratory Rate (breaths/min)"
            name="respiratory_rate_avg_30d"
            descriptor={descriptors.respiratory_rate_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.respiratory_rate_avg_30d}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Sleep (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Avg Sleep Duration (hours)"
            name="sleep_duration_hours_avg_30d"
            descriptor={descriptors.sleep_duration_hours_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.sleep_duration_hours_avg_30d}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Fitness (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="VO&#8322; Max (mL/kg/min)"
            name="vo2_max_avg_30d"
            descriptor={descriptors.vo2_max_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.vo2_max_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Distance (km/day)"
            name="distance_km_per_day_30d"
            descriptor={descriptors.distance_km_per_day_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.distance_km_per_day_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Flights Climbed / Day"
            name="flights_climbed_per_day_30d"
            descriptor={descriptors.flights_climbed_per_day_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.flights_climbed_per_day_30d}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Gait & Mobility (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Walking Speed (km/hr)"
            name="walking_speed_avg_30d"
            descriptor={descriptors.walking_speed_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.walking_speed_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Step Length (cm)"
            name="walking_step_length_avg_30d"
            descriptor={descriptors.walking_step_length_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.walking_step_length_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Double Support (%)"
            name="walking_double_support_pct_avg_30d"
            descriptor={descriptors.walking_double_support_pct_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.walking_double_support_pct_avg_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Walking Heart Rate (bpm)"
            name="walking_hr_avg_30d"
            descriptor={descriptors.walking_hr_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.walking_hr_avg_30d}
            onChange={() => {}}
          />
        </div>
      </Section>

      <Section title="Energy & Body (30-Day)">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <MetricField
            label="Active Energy (kcal/day)"
            name="active_energy_per_day_30d"
            descriptor={descriptors.active_energy_per_day_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.active_energy_per_day_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Basal Energy (kcal/day)"
            name="basal_energy_per_day_30d"
            descriptor={descriptors.basal_energy_per_day_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.basal_energy_per_day_30d}
            onChange={() => {}}
          />
          <MetricField
            label="Body Mass (kg)"
            name="body_mass_avg_30d"
            descriptor={descriptors.body_mass_avg_30d}
            showReason={false}
            showAccepted
            type="number"
            value={formData?.body_mass_avg_30d}
            onChange={() => {}}
          />
        </div>
      </Section>
    </div>
    </WearableMetricProvider>
  );
}
