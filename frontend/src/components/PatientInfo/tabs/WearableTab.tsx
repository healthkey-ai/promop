import Field from '../Field';
import Section from '../Section';

const ACTIVITY_TREND_OPTIONS = ['improving', 'stable', 'declining', 'insufficient_data'];

interface Props {
  formData: Record<string, unknown>;
  onChange: (field: string, value: unknown) => void;
}

function formatSyncDate(raw: unknown): string {
  if (!raw) return '';
  try {
    return new Date(raw as string).toLocaleString();
  } catch {
    return String(raw);
  }
}

export default function WearableTab({ formData }: Props) {
  const noData = !formData?.wearable_last_sync_at;

  return (
    <div>
      {noData && (
        <p className="mb-4 text-sm text-gray-500 italic">
          No wearable data synced yet. Connect an Apple Health source via the mobile app to populate these fields.
        </p>
      )}

      <Section title="Data Coverage">
        <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">Last Sync</label>
            <p className="text-sm text-gray-700 py-1.5">
              {formatSyncDate(formData?.wearable_last_sync_at) || '—'}
            </p>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-portal-text-primary">
              Coverage Ratio (30 days)
            </label>
            <p className="text-sm text-gray-700 py-1.5">
              {formData?.wearable_coverage_ratio_30d != null
                ? `${(Number(formData.wearable_coverage_ratio_30d) * 100).toFixed(0)}%`
                : '—'}
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
            label="Min SpO₂ (%)"
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
