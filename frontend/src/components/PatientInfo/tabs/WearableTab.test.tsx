import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import WearableTab from './WearableTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockPost = vi.fn();
const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({
  default: {
    post: (...args: unknown[]) => mockPost(...args),
    get: (...args: unknown[]) => mockGet(...args),
  },
}));

// Stub Section and Field to simplify rendering
vi.mock('../Section', () => ({
  default: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div data-testid={`section-${title}`}>{children}</div>
  ),
}));

// The tab renders ClinicalField now, and no test asserted on the old Field stub,
// so it is gone rather than left as a mock of something nothing imports.

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderTab(props: Partial<React.ComponentProps<typeof WearableTab>> = {}) {
  const defaultProps = {
    formData: {},
    onChange: vi.fn(),
    onRefresh: vi.fn(),
    ...props,
  };
  return render(<WearableTab {...defaultProps} />);
}

function createFile(name: string, content = 'fake-data'): File {
  return new File([content], name, { type: 'application/octet-stream' });
}

function createDropEvent(files: File[]): Partial<React.DragEvent> {
  return {
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    dataTransfer: {
      files: files as unknown as FileList,
      types: ['Files'],
    } as unknown as DataTransfer,
  };
}

function createDragEnterEvent(): Partial<React.DragEvent> {
  return {
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    dataTransfer: { types: ['Files'] } as unknown as DataTransfer,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
  __resetWritableFieldsCache();
  // Upload history is empty; the writable-field descriptor is served separately
  // because the tab now asks what may be written before rendering a box.
  mockGet.mockImplementation((url: string) =>
    Promise.resolve({
      data: url.includes('writable-fields') ? DESCRIPTORS : [],
    }),
  );
});

/** Every wearable field is a 30-day aggregate over device readings. */
const aggregate = (metric: string) => ({
  kind: 'computed', writable: false, inputs: [metric], window_days: 30,
  reason: `A 30-day aggregate of ${metric} readings. Upload device data rather `
    + 'than entering a summary value.',
});

const DESCRIPTORS: Record<string, unknown> = {
  median_daily_steps_30d: aggregate('steps'),
  resting_heart_rate_avg_30d: aggregate('resting_hr'),
  vo2_max_avg_30d: aggregate('vo2_max'),
  body_mass_avg_30d: aggregate('body_mass'),
};

describe('WearableTab', () => {
  it('renders the Upload button', () => {
    renderTab();
    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
  });

  it('shows drag-and-drop hint text', () => {
    renderTab();
    expect(screen.getByText(/or drag & drop files here/i)).toBeInTheDocument();
  });

  it('shows no-data message when wearable_last_sync_at is absent', async () => {
    renderTab({ formData: {} });
    await waitFor(() => {
      expect(screen.getByText(/no wearable data synced yet/i)).toBeInTheDocument();
    });
  });

  it('hides no-data message when wearable_last_sync_at is present', () => {
    renderTab({ formData: { wearable_last_sync_at: '2024-07-01T00:00:00Z' } });
    expect(screen.queryByText(/no wearable data synced yet/i)).not.toBeInTheDocument();
  });
});

describe('WearableTab — upload history', () => {
  it('fetches upload history on mount', async () => {
    renderTab();
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith('/v1/patient-records/wearable-uploads/');
    });
  });

  it('renders upload history list when uploads exist', async () => {
    mockGet.mockResolvedValue({
      data: [
        {
          id: 1,
          device_type: 'garmin',
          filename: 'M87E3429.FIT',
          samples_created: 3,
          duplicates_skipped: 0,
          sample_summary: [
            { metric: 'steps', date: '2026-08-07', value: 11589 },
            { metric: 'resting_hr', date: '2026-08-07', value: 48 },
          ],
          uploaded_at: '2026-08-07T13:50:00Z',
        },
      ],
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText('M87E3429.FIT')).toBeInTheDocument();
    });
    expect(screen.getByText('3 samples')).toBeInTheDocument();
    expect(screen.getByText('Garmin')).toBeInTheDocument();
  });

  it('expands upload to show sample details on click', async () => {
    mockGet.mockResolvedValue({
      data: [
        {
          id: 1,
          device_type: 'garmin',
          filename: 'test.FIT',
          samples_created: 2,
          duplicates_skipped: 0,
          sample_summary: [
            { metric: 'steps', date: '2026-08-07', value: 11589 },
            { metric: 'resting_hr', date: '2026-08-07', value: 48 },
          ],
          uploaded_at: '2026-08-07T13:50:00Z',
        },
      ],
    });
    renderTab();

    await waitFor(() => {
      expect(screen.getByText('test.FIT')).toBeInTheDocument();
    });

    // Click to expand
    fireEvent.click(screen.getByText('test.FIT'));

    await waitFor(() => {
      expect(screen.getByText('Steps')).toBeInTheDocument();
      expect(screen.getByText('Resting Heart Rate')).toBeInTheDocument();
      expect(screen.getByText('11,589')).toBeInTheDocument();
      expect(screen.getByText('48 bpm')).toBeInTheDocument();
    });
  });
});

describe('WearableTab — drag and drop', () => {
  it('shows drop overlay on dragEnter', () => {
    renderTab();
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.dragEnter(container, createDragEnterEvent());
    expect(screen.getByText(/drop .fit or .zip files to upload/i)).toBeInTheDocument();
  });

  it('hides drop overlay on dragLeave', () => {
    renderTab();
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.dragEnter(container, createDragEnterEvent());
    expect(screen.getByText(/drop .fit or .zip files to upload/i)).toBeInTheDocument();
    fireEvent.dragLeave(container, createDragEnterEvent());
    expect(screen.queryByText(/drop .fit or .zip files to upload/i)).not.toBeInTheDocument();
  });

  it('uploads .fit files as garmin on drop', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 5, duplicates_skipped: 0 } });
    const onRefresh = vi.fn();
    renderTab({ onRefresh });

    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    const files = [createFile('activity.fit')];
    fireEvent.drop(container, createDropEvent(files));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(
        '/v1/patient-records/upload-wearable/',
        expect.any(FormData),
        expect.objectContaining({ headers: { 'Content-Type': 'multipart/form-data' } }),
      );
    });

    // Verify device_type=garmin was sent
    const sentFormData = mockPost.mock.calls[0][1] as FormData;
    expect(sentFormData.get('device_type')).toBe('garmin');

    await waitFor(() => {
      expect(screen.getByText(/uploaded 5 samples/i)).toBeInTheDocument();
    });
    expect(onRefresh).toHaveBeenCalled();
  });

  it('uploads .zip files as apple on drop', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 3, duplicates_skipped: 1 } });
    renderTab();

    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('export.zip')]));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalled();
    });

    const sentFormData = mockPost.mock.calls[0][1] as FormData;
    expect(sentFormData.get('device_type')).toBe('apple');
  });

  it('rejects unsupported file extensions', async () => {
    renderTab();
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('data.csv')]));

    await waitFor(() => {
      expect(screen.getByText(/unsupported file type/i)).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('rejects mixed file types in a single drop', async () => {
    renderTab();
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('a.fit'), createFile('b.zip')]));

    await waitFor(() => {
      expect(screen.getByText(/one type at a time/i)).toBeInTheDocument();
    });
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('uploads multiple .fit files in a single drop', async () => {
    mockPost
      .mockResolvedValueOnce({ data: { samples_created: 3, duplicates_skipped: 0 } })
      .mockResolvedValueOnce({ data: { samples_created: 2, duplicates_skipped: 1 } });
    renderTab();

    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('a.fit'), createFile('b.fit')]));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(2);
    });

    await waitFor(() => {
      expect(screen.getByText(/uploaded 5 samples/i)).toBeInTheDocument();
      expect(screen.getByText(/1 duplicate.*skipped/i)).toBeInTheDocument();
    });
  });

  it('shows MTP error when files array is empty but URI data present', async () => {
    renderTab();
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;

    const mtpDropEvent: Partial<React.DragEvent> = {
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
      dataTransfer: {
        files: [] as unknown as FileList,
        types: ['text/uri-list', 'text/html'],
        getData: (type: string) => type === 'text/uri-list' ? 'file:///some/path' : '',
      } as unknown as DataTransfer,
    };
    fireEvent.drop(container, mtpDropEvent);

    await waitFor(() => {
      expect(screen.getByText(/cannot be read directly by the browser/i)).toBeInTheDocument();
    });
  });
});

/**
 * Rendered from the descriptor (plan step 4).
 *
 * Nothing here is writable and nothing ever was — every field is a 30-day
 * aggregate over device readings, and a clinician does not type a median. The
 * boxes were already `disabled`, so this conversion is not about locking them;
 * it is about saying *why*, from the server, and pointing at the upload control
 * that is the actual way to change these numbers.
 */
describe('WearableTab — descriptor-driven', () => {
  it('offers no editable control', async () => {
    renderTab({ formData: { median_daily_steps_30d: 8000, vo2_max_avg_30d: 42 } });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    // Number fields carry role spinbutton, not textbox.
    const inputs = [...screen.queryAllByRole('textbox'),
                    ...screen.queryAllByRole('spinbutton')];
    expect(inputs.length).toBeGreaterThan(0);
    for (const input of inputs) expect(input).toBeDisabled();
  });

  it('explains once that these follow from uploaded readings', async () => {
    // Twenty near-identical reasons differing only by metric name — which the
    // label already carries — would bury the one thing worth reading.
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByText(/a clinician does not enter a median/i)).toBeInTheDocument();
    expect(screen.queryByTestId('reason-median_daily_steps_30d')).not.toBeInTheDocument();
  });

  it('still shows the aggregated values', async () => {
    // Read-only is not hidden: reading these back is the whole purpose of the tab.
    renderTab({ formData: { median_daily_steps_30d: 8000 } });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByDisplayValue('8000')).toBeInTheDocument();
  });

  it('keeps the upload control, which is how these values actually change', async () => {
    renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
  });

  it('stays read-only when the descriptor cannot be fetched', async () => {
    mockGet.mockRejectedValue(new Error('offline'));
    renderTab({ formData: { median_daily_steps_30d: 8000 } });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await waitFor(() => {
      const inputs = [...screen.queryAllByRole('textbox'),
                      ...screen.queryAllByRole('spinbutton')];
      expect(inputs.length).toBeGreaterThan(0);
      for (const input of inputs) expect(input).toBeDisabled();
    });
  });
});

/**
 * Sync from Garmin Connect with the patient's own Personal API key (#2794).
 * The key goes in one request and is kept nowhere, so the field empties as
 * soon as the server has accepted a sync.
 */
describe('WearableTab — Garmin Connect sync', () => {
  const KEY = 'gcpk_TEST-key.0123456789';
  const SYNCED = {
    samples_created: 210, duplicates_skipped: 30, replaced: 12,
    unmapped_samples: 0, unmapped_metrics: [],
    start_date: '2026-10-24', end_date: '2026-11-22', datasets_failed: [],
  };

  const keyField = () => screen.getByLabelText('Garmin Connect Personal API key') as HTMLInputElement;
  const syncButton = () => screen.getByRole('button', { name: 'Sync' });

  function typeKey(value = KEY) {
    fireEvent.change(keyField(), { target: { value } });
  }

  it('asks for the key in a masked field and says it is not stored', () => {
    renderTab();
    expect(keyField().type).toBe('password');
    // Not "off": a browser would still offer a saved sign-in password, which
    // would then be sent to Garmin as the key.
    expect(keyField()).toHaveAttribute('autocomplete', 'new-password');
    expect(screen.getByText(/used for this sync only and is not stored/i)).toBeInTheDocument();
    expect(screen.getByText(/Settings, Account, Personal API\s+Token/i)).toBeInTheDocument();
  });

  it('cannot sync without a key', () => {
    renderTab();
    expect(syncButton()).toBeDisabled();
    typeKey('   ');
    expect(syncButton()).toBeDisabled();
    typeKey();
    expect(syncButton()).toBeEnabled();
  });

  it('sends the key, the range and the local yesterday, then clears the key', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 23, 9, 30));
    try {
      mockPost.mockResolvedValue({ data: SYNCED });
      const onRefresh = vi.fn();
      renderTab({ onRefresh });
      const historyReads = () =>
        mockGet.mock.calls.filter(([url]) => String(url).includes('wearable-uploads')).length;
      await waitFor(() => expect(historyReads()).toBe(1));

      typeKey(`  ${KEY} `);
      fireEvent.change(screen.getByLabelText('Range'), { target: { value: '90' } });
      fireEvent.click(syncButton());

      await waitFor(() => {
        expect(mockPost).toHaveBeenCalledWith('/v1/patient-records/sync-garmin/', {
          api_key: KEY, days: 90, end_date: '2026-11-22',
        });
      });
      await waitFor(() => {
        expect(screen.getByText(
          'Synced 2026-10-24 to 2026-11-22: 210 new values, 12 replaced, 30 already stored.',
        )).toBeInTheDocument();
      });
      expect(keyField().value).toBe('');
      expect(onRefresh).toHaveBeenCalledTimes(1);
      // The sync is a row in the upload history, so the history is read again.
      await waitFor(() => expect(historyReads()).toBe(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it('defaults to 30 days and syncs on Enter', async () => {
    mockPost.mockResolvedValue({ data: { ...SYNCED, samples_created: 1, replaced: 0, duplicates_skipped: 0 } });
    renderTab();
    typeKey();
    fireEvent.keyDown(keyField(), { key: 'Enter' });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toMatchObject({ api_key: KEY, days: 30 });
    await waitFor(() => {
      expect(screen.getByText('Synced 2026-10-24 to 2026-11-22: 1 new value.')).toBeInTheDocument();
    });
  });

  it('does not reload the record when nothing changed', async () => {
    mockPost.mockResolvedValue({
      data: { ...SYNCED, samples_created: 0, replaced: 0, duplicates_skipped: 240 },
    });
    const onRefresh = vi.fn();
    renderTab({ onRefresh });
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText(/0 new values, 240 already stored/)).toBeInTheDocument();
    });
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('names the data Garmin did not return', async () => {
    mockPost.mockResolvedValue({ data: { ...SYNCED, datasets_failed: ['sleep', 'hrv', 'vo2_max'] } });
    renderTab();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText(/Garmin did not return sleep, HRV, VO₂ max for this range/)).toBeInTheDocument();
    });
  });

  it('names metrics the server has no mapping for', async () => {
    mockPost.mockResolvedValue({ data: { ...SYNCED, unmapped_metrics: ['vo2_max'] } });
    renderTab();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText(/no approved mapping for: vo2_max/)).toBeInTheDocument();
    });
  });

  it("shows the server's reason when the key is refused, and keeps the key to correct", async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: { error: 'Garmin did not accept this API key.' } } });
    const onRefresh = vi.fn();
    renderTab({ onRefresh });
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Garmin did not accept this API key.');
    });
    expect(keyField().value).toBe(KEY);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(syncButton()).toBeEnabled();
  });

  it('shows the throttle message, which arrives as detail', async () => {
    mockPost.mockRejectedValue({
      response: { status: 429, data: { detail: 'Request was throttled. Expected available in 120 seconds.' } },
    });
    renderTab();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/throttled/));
  });

  it('falls back to a plain message when the request never reached the server', async () => {
    mockPost.mockRejectedValue(new Error('Network Error'));
    renderTab();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Sync failed. Please try again.'));
  });

  it('sends one request however often Sync is pressed while it runs', async () => {
    let finish: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    renderTab();
    typeKey();
    fireEvent.click(syncButton());
    const busy = await screen.findByRole('button', { name: 'Syncing...' });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    fireEvent.keyDown(keyField(), { key: 'Enter' });
    expect(mockPost).toHaveBeenCalledTimes(1);
    finish({ data: SYNCED });
    await waitFor(() => expect(syncButton()).toBeDisabled()); // key cleared
  });
});
