import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import WearableTab from './WearableTab';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { ProvisionalContext } from '../ProvisionalContext';

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
    // The person's own record, where uploads are offered (#2819).
    ownRecord: true,
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

  it.each([
    ['garmin', 'Garmin'],
    ['apple', 'Apple'],
    // What a phone app syncs from Health Connect is filed as android.
    ['android', 'Android'],
    // A device type the tab has no name for is shown as the server sent it.
    ['oura', 'oura'],
    ['toString', 'toString'],
  ])('labels a %s upload %s', async (deviceType, label) => {
    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: url.includes('writable-fields') ? DESCRIPTORS : [{
        id: 3, device_type: deviceType, filename: 'Phone health app 2026-11', samples_created: 4,
        duplicates_skipped: 0, sample_summary: [], uploaded_at: '2026-11-20T10:00:00Z',
      }],
    }));
    renderTab();
    const row = (await screen.findByText('Phone health app 2026-11')).closest('button')!;
    expect(row.firstElementChild).toHaveTextContent(new RegExp(`^${label}$`));
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
describe("WearableTab — somebody else's record (#2819)", () => {
  const HISTORY = [{
    id: 1, device_type: 'garmin', filename: 'MINE.FIT', samples_created: 3, duplicates_skipped: 0,
    sample_summary: [{ metric: 'steps', date: '2026-08-07', value: 11589 }],
    uploaded_at: '2026-08-07T13:50:00Z',
  }];
  const withHistory = () => mockGet.mockImplementation((url: string) =>
    Promise.resolve({ data: url.includes('writable-fields') ? DESCRIPTORS : HISTORY }));

  it('offers no upload, and says who adds wearable data', async () => {
    renderTab({ ownRecord: false, formData: { person_id: 202 } });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/or drag & drop files here/i)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeNull();
    expect(screen.getByText(/added by the patient, from their own record/i)).toBeInTheDocument();
  });

  it("does not read or show the viewer's own upload history, or a delete", async () => {
    withHistory();
    renderTab({ ownRecord: false, formData: { person_id: 202 } });
    // The descriptor read is the tab's only request.
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    await Promise.resolve();
    expect(mockGet).not.toHaveBeenCalledWith('/v1/patient-records/wearable-uploads/');
    expect(screen.queryByText('MINE.FIT')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Delete upload')).not.toBeInTheDocument();
    expect(screen.queryByTestId('section-Upload History')).not.toBeInTheDocument();
  });

  it('ignores a dropped file: nothing is sent and no drop zone opens', async () => {
    renderTab({ ownRecord: false, formData: { person_id: 202 } });
    const container = screen.getByText(/added by the patient/i).closest('div[class*="relative"]')!;
    fireEvent.dragEnter(container, createDragEnterEvent());
    expect(screen.queryByText(/drop .fit or .zip files to upload/i)).not.toBeInTheDocument();
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    await Promise.resolve();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('says only that nothing is synced when the record has no wearable data', async () => {
    renderTab({ ownRecord: false, formData: { person_id: 202 } });
    expect(await screen.findByText('No wearable data synced yet.')).toBeInTheDocument();
    expect(screen.queryByText(/contribute to your record/i)).not.toBeInTheDocument();
  });

  it('still shows the wearable fields, and does not point at an upload that is not there', () => {
    renderTab({ ownRecord: false, formData: { person_id: 202, wearable_last_sync_at: '2024-07-01T00:00:00Z' } });
    expect(screen.getByTestId('section-Data Coverage')).toBeInTheDocument();
    expect(screen.queryByText(/Upload device data above/i)).not.toBeInTheDocument();
    expect(screen.getByText(/follow from the device data the patient adds/i)).toBeInTheDocument();
  });

  it('points at the upload on the own record', () => {
    renderTab({ formData: { person_id: 101 } });
    expect(screen.getByText(/Upload device data above and these follow/i)).toBeInTheDocument();
  });

  it('is what a caller that says nothing gets', async () => {
    render(<WearableTab formData={{ person_id: 202 }} onChange={vi.fn()} />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument();
  });
});

describe('WearableTab — an upload names the record it was started from (#2819)', () => {
  it("sends the page's person_id with the file", async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 2, duplicates_skipped: 0 } });
    renderTab({ formData: { person_id: 101 } });
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    const [url, payload] = mockPost.mock.calls[0] as [string, FormData];
    expect(url).toBe('/v1/patient-records/upload-wearable/');
    expect(payload.get('person_id')).toBe('101');
    expect(payload.get('device_type')).toBe('garmin');
  });

  it('sends none when the record has no person yet', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 1, duplicates_skipped: 0 } });
    renderTab({ formData: {} });
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
    expect((mockPost.mock.calls[0][1] as FormData).has('person_id')).toBe(false);
  });

  it("shows the server's refusal", async () => {
    mockPost.mockRejectedValue({ response: { data: { error: 'Wearable uploads go to your own record only.' } } });
    renderTab({ formData: { person_id: 101 } });
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    expect(await screen.findByText('Wearable uploads go to your own record only.')).toBeInTheDocument();
  });
});

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
 * The key is never kept in the page, so the field empties as soon as the
 * server has accepted a sync. Saving it on the server (#2834) is covered in
 * GarminConnectSync.test.tsx; here the site has not said it can save.
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
  // The button is aria-disabled, not disabled, so it keeps focus through a sync.
  const canSync = () => syncButton().getAttribute('aria-disabled') !== 'true';
  /** The tab as the patient sees it on their own record. */
  const renderOwn = (props: Partial<React.ComponentProps<typeof WearableTab>> = {}) =>
    renderTab({ canSyncOwnDevice: true, ...props });

  function typeKey(value = KEY) {
    fireEvent.change(keyField(), { target: { value } });
  }

  it('offers Garmin\'s automatic updates (Connect Garmin) to a patient on their own record, where a Garmin app is set up (#2802)', async () => {
    const first = renderTab({ ownRecord: false });
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    // A clinician's view of the record never asks for the patient's connection.
    expect(mockGet.mock.calls.some(([url]) => String(url).includes('/v1/garmin/connection/'))).toBe(false);
    first.unmount();

    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: String(url).includes('/v1/garmin/connection/')
        ? { configured: true, connected: false, connected_at: null, last_push_at: null, permissions: [] }
        : [],
    }));
    renderOwn();
    expect(await screen.findByRole('button', { name: 'Connect Garmin' })).toBeInTheDocument();
    // Beside it, not in place of, the sync by Personal API key.
    expect(screen.getByRole('heading', { name: 'Sync past days' })).toBeInTheDocument();
  });

  it('keeps both Garmin ways in under one Garmin heading, the account first (#2832)', async () => {
    mockGet.mockImplementation((url: string) => {
      if (String(url).includes('/v1/garmin/connection/')) {
        return Promise.resolve({
          data: { configured: true, connected: false, connected_at: null, last_push_at: null, permissions: [] },
        });
      }
      if (String(url).includes('/v1/open-wearables/connection/')) {
        return Promise.resolve({
          data: {
            enabled: true, connected: false, devices: 0, code_pending: false, last_sync_at: null, providers: [],
            host: 'https://records.example.org',
          },
        });
      }
      return Promise.resolve({ data: [] });
    });
    renderOwn();
    const garmin = screen.getByRole('region', { name: 'Garmin' });
    const connect = await within(garmin).findByRole('button', { name: 'Connect Garmin' });
    const account = within(garmin).getByRole('heading', { name: 'Automatic updates' });
    const sync = within(garmin).getByRole('heading', { name: 'Sync past days' });
    // Each part says what it is for.
    expect(garmin).toHaveTextContent('from the day you connect');
    expect(garmin).toHaveTextContent('up to 90 past days');
    expect(within(garmin).getByLabelText('Garmin Connect Personal API key')).toBeInTheDocument();
    // The account first, the sync by key under it.
    expect(account.compareDocumentPosition(sync) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(connect.compareDocumentPosition(sync) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The phone app is a panel of its own, after Garmin and not inside it.
    const phone = await screen.findByRole('heading', { name: 'Phone health app' });
    expect(garmin).not.toContainElement(phone);
    expect(garmin.compareDocumentPosition(phone) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows the Garmin section with the sync by key alone where no Garmin app is set up (#2832)', async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: String(url).includes('/v1/garmin/connection/')
        ? { configured: false, connected: false, connected_at: null, last_push_at: null, permissions: [] }
        : [],
    }));
    renderOwn();
    const garmin = screen.getByRole('region', { name: 'Garmin' });
    await waitFor(() => expect(
      mockGet.mock.calls.some(([url]) => String(url).includes('/v1/garmin/connection/'))).toBe(true));
    // Let the answer land before saying what it did not bring.
    await act(async () => {});
    expect(within(garmin).getByRole('heading', { name: 'Sync past days' })).toBeInTheDocument();
    expect(within(garmin).queryByRole('heading', { name: 'Automatic updates' })).not.toBeInTheDocument();
    expect(within(garmin).queryByRole('button', { name: 'Connect Garmin' })).not.toBeInTheDocument();
  });

  it('offers the phone health app panel to a patient on their own record, where it is set up (#2810)', async () => {
    const first = renderTab();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    // A clinician's view of the record never asks for the patient's connection.
    expect(mockGet.mock.calls.some(([url]) => String(url).includes('/v1/open-wearables/'))).toBe(false);
    expect(screen.queryByText('Phone health app')).not.toBeInTheDocument();
    first.unmount();

    // Asked for on the patient's own record, and not shown where the answer is not a status.
    const second = renderOwn();
    await waitFor(() => expect(
      mockGet.mock.calls.some(([url]) => String(url).includes('/v1/open-wearables/connection/'))).toBe(true));
    expect(screen.queryByText('Phone health app')).not.toBeInTheDocument();
    second.unmount();

    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: String(url).includes('/v1/open-wearables/connection/')
        ? {
          enabled: true, connected: false, devices: 0, code_pending: false, last_sync_at: null, providers: [],
          host: 'https://records.example.org',
        }
        : [],
    }));
    renderOwn();
    expect(await screen.findByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Phone health app' })).toBeInTheDocument();
    // Beside the Garmin ways in, not in place of them.
    expect(screen.getByRole('heading', { name: 'Sync past days' })).toBeInTheDocument();
  });

  it('is offered only to a patient on their own record', () => {
    // The server files a sync under whoever is signed in. A clinician on a
    // patient's page must not be asked for a key, nor offered an upload (#2819).
    const clinician = renderTab({ ownRecord: false });
    expect(screen.queryByRole('region', { name: 'Garmin' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sync past days' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Garmin Connect Personal API key')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /upload/i })).not.toBeInTheDocument();
    clinician.unmount();

    // The federated page is the patient's own record without this site's
    // session: no key sync, and the file upload as before.
    renderTab();
    expect(screen.queryByRole('region', { name: 'Garmin' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sync past days' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Garmin Connect Personal API key')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
  });

  it('asks for the key in a masked field no browser will store, and says it is not stored where the site cannot save', () => {
    renderOwn();
    // Not a password field: that would invite the browser to fill in the
    // saved sign-in password, and to save the key as the site's password.
    expect(keyField().type).toBe('text');
    expect(keyField().className).toContain('[-webkit-text-security:disc]');
    expect(keyField()).toHaveAttribute('autocomplete', 'off');
    expect(keyField()).toHaveAttribute('data-1p-ignore');
    expect(keyField()).toHaveAttribute('data-lpignore', 'true');
    expect(screen.getByText(/used for this sync only and is not stored/i)).toBeInTheDocument();
    expect(screen.getByText(/Settings, Account, Personal API\s+Token/i)).toBeInTheDocument();
  });

  it('cannot sync without a key', () => {
    renderOwn();
    expect(canSync()).toBe(false);
    fireEvent.click(syncButton());
    typeKey('   ');
    expect(canSync()).toBe(false);
    fireEvent.click(syncButton());
    fireEvent.keyDown(keyField(), { key: 'Enter' });
    expect(mockPost).not.toHaveBeenCalled();
    typeKey();
    expect(canSync()).toBe(true);
  });

  it('sends the key, the range and the local yesterday, then clears the key', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 10, 23, 9, 30));
    try {
      mockPost.mockResolvedValue({ data: SYNCED });
      const onRefresh = vi.fn();
      renderOwn({ onRefresh });
      const historyReads = () =>
        mockGet.mock.calls.filter(([url]) => String(url).includes('wearable-uploads')).length;
      await waitFor(() => expect(historyReads()).toBe(1));

      typeKey(`  ${KEY} `);
      fireEvent.change(screen.getByLabelText('Range'), { target: { value: '90' } });
      fireEvent.click(syncButton());

      await waitFor(() => {
        // Nothing here has said the site can save a key, so the sync is a one-off (#2834).
        expect(mockPost).toHaveBeenCalledWith('/v1/patient-records/sync-garmin/', {
          api_key: KEY, remember: false, days: 90, end_date: '2026-11-22',
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
    renderOwn();
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
    renderOwn({ onRefresh });
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText(/0 new values, 240 already stored/)).toBeInTheDocument();
    });
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('names the data Garmin did not return', async () => {
    mockPost.mockResolvedValue({ data: { ...SYNCED, datasets_failed: ['sleep', 'hrv', 'vo2_max'] } });
    renderOwn();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText(/Garmin did not return sleep, HRV, VO₂ max for this range/)).toBeInTheDocument();
    });
  });

  it('names metrics the server has no mapping for', async () => {
    mockPost.mockResolvedValue({ data: { ...SYNCED, unmapped_metrics: ['vo2_max'] } });
    renderOwn();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByText('Not stored: VO\u2082 Max. This site cannot store this metric yet.')).toBeInTheDocument();
    });
  });

  it("shows the server's reason when the key is refused, and keeps the key to correct", async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: { error: 'Garmin did not accept this API key.' } } });
    const onRefresh = vi.fn();
    renderOwn({ onRefresh });
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Garmin did not accept this API key.');
    });
    expect(keyField().value).toBe(KEY);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(canSync()).toBe(true);
    // Focus is back on the field that has to be corrected.
    expect(keyField()).toHaveFocus();
  });

  it('shows the throttle message, which arrives as detail', async () => {
    mockPost.mockRejectedValue({
      response: { status: 429, data: { detail: 'Request was throttled. Expected available in 120 seconds.' } },
    });
    renderOwn();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/throttled/));
  });

  it('falls back to a plain message when the request never reached the server', async () => {
    mockPost.mockRejectedValue(new Error('Network Error'));
    renderOwn();
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Sync failed. Please try again.'));
  });

  it('sends one request however often Sync is pressed while it runs', async () => {
    let finish: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    renderOwn();
    typeKey();
    fireEvent.click(syncButton());
    const busy = await screen.findByRole('button', { name: 'Syncing...' });
    expect(busy).toHaveAttribute('aria-disabled', 'true');
    // Said to a screen reader too, not only on the button.
    expect(screen.getByText(/Syncing with Garmin Connect/)).toBeInTheDocument();
    expect(keyField()).toHaveAttribute('readonly');
    fireEvent.click(busy);
    fireEvent.keyDown(keyField(), { key: 'Enter' });
    expect(mockPost).toHaveBeenCalledTimes(1);
    finish({ data: SYNCED });
    await waitFor(() => expect(keyField().value).toBe('')); // key cleared
    expect(canSync()).toBe(false);
    expect(screen.queryByText(/Syncing with Garmin Connect/)).not.toBeInTheDocument();
  });

  it('says whose record the page shows, so the server can refuse another person\'s', async () => {
    mockPost.mockResolvedValue({ data: SYNCED });
    renderOwn({ formData: { person_id: 4321 } });
    typeKey();
    fireEvent.click(syncButton());
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toMatchObject({ api_key: KEY, person_id: 4321 });
  });

  it('holds uploads and deletes while a sync runs', async () => {
    mockGet.mockImplementation((url: string) => Promise.resolve({
      data: url.includes('writable-fields') ? DESCRIPTORS : [{
        id: 7, device_type: 'garmin', filename: 'day.fit', samples_created: 1,
        duplicates_skipped: 0, sample_summary: [], uploaded_at: '2026-11-20T10:00:00Z',
      }],
    }));
    let finish: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    renderOwn();
    const upload = screen.getByRole('button', { name: /upload/i });
    const remove = await screen.findByTitle('Delete upload');
    expect(upload).toBeEnabled();

    typeKey();
    fireEvent.click(syncButton());
    await screen.findByRole('button', { name: 'Syncing...' });
    expect(upload).toBeDisabled();
    expect(remove).toBeDisabled();
    // A file dropped meanwhile is not sent.
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    expect(mockPost).toHaveBeenCalledTimes(1);

    finish({ data: SYNCED });
    await waitFor(() => expect(upload).toBeEnabled());
    expect(remove).toBeEnabled();
  });

  it('cannot sync while a file upload runs', async () => {
    let finish: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    renderOwn();
    typeKey();
    expect(canSync()).toBe(true);
    const container = screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;
    fireEvent.drop(container, createDropEvent([createFile('activity.fit')]));
    await waitFor(() => expect(canSync()).toBe(false));
    fireEvent.click(syncButton());
    expect(mockPost).toHaveBeenCalledTimes(1); // the upload only
    finish({ data: { samples_created: 1, duplicates_skipped: 0 } });
    await waitFor(() => expect(canSync()).toBe(true));
  });

  it("ends at the reader's own yesterday, not the server's or UTC's", async () => {
    // 00:30 on New Year's Day in Auckland is still 30 December in UTC: the
    // last whole day for this reader is 31 December.
    const zone = process.env.TZ;
    process.env.TZ = 'Pacific/Auckland';
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2027, 0, 1, 0, 30));
    try {
      mockPost.mockResolvedValue({ data: SYNCED });
      renderOwn();
      typeKey();
      fireEvent.click(syncButton());
      await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
      expect(mockPost.mock.calls[0][1]).toMatchObject({ end_date: '2026-12-31' });
    } finally {
      vi.useRealTimers();
      if (zone === undefined) delete process.env.TZ; else process.env.TZ = zone;
    }
  });
});

describe('WearableTab — every 30-day value says Accepted or Provisional (#2835)', () => {
  /** The tab with the record's provisional marks, as the patient page provides them. */
  async function renderMarked(formData: Record<string, unknown>, marks: Record<string, number> = {}) {
    const view = render(
      <ProvisionalContext.Provider value={{ marks, patientMode: false }}>
        <WearableTab formData={formData} onChange={vi.fn()} onRefresh={vi.fn()} ownRecord />
      </ProvisionalContext.Provider>,
    );
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    return view;
  }

  it('marks a metric that has a value and no provisional mark Accepted', async () => {
    await renderMarked({ median_daily_steps_30d: 8000, activity_trend_30d: 'stable' });

    const badge = screen.getByTestId('accepted-median_daily_steps_30d');
    expect(badge).toHaveTextContent('Accepted');
    expect(badge.title).toMatch(/code mapping is approved/);
    expect(screen.getByTestId('accepted-activity_trend_30d')).toBeInTheDocument();
    expect(screen.queryByTestId('provisional-median_daily_steps_30d')).not.toBeInTheDocument();
  });

  it('marks a metric the record holds as a proposal Provisional, and not Accepted', async () => {
    await renderMarked(
      { median_daily_steps_30d: 8000, vo2_max_avg_30d: 42, resting_heart_rate_avg_30d: 61 },
      { median_daily_steps_30d: 70, vo2_max_avg_30d: 80 },
    );

    expect(screen.getByTestId('provisional-median_daily_steps_30d').textContent).toBe('Provisional 70%');
    expect(screen.getByTestId('provisional-vo2_max_avg_30d').textContent).toBe('Provisional 80%');
    expect(screen.queryByTestId('accepted-median_daily_steps_30d')).not.toBeInTheDocument();
    expect(screen.queryByTestId('accepted-vo2_max_avg_30d')).not.toBeInTheDocument();
    // The value beside them came in under an approved mapping.
    expect(screen.getByTestId('accepted-resting_heart_rate_avg_30d')).toBeInTheDocument();
  });

  it('marks a metric with no value neither', async () => {
    await renderMarked({
      median_daily_steps_30d: null, vo2_max_avg_30d: '', activity_trend_30d: '', resting_heart_rate_avg_30d: 61,
    });

    for (const name of ['median_daily_steps_30d', 'vo2_max_avg_30d', 'activity_trend_30d', 'body_mass_avg_30d']) {
      expect(screen.queryByTestId(`accepted-${name}`)).not.toBeInTheDocument();
      expect(screen.queryByTestId(`provisional-${name}`)).not.toBeInTheDocument();
    }
    expect(screen.getAllByText('Accepted')).toHaveLength(1);
  });

  it('counts zero as a value', async () => {
    await renderMarked({ flights_climbed_per_day_30d: 0 });
    expect(screen.getByTestId('accepted-flights_climbed_per_day_30d')).toBeInTheDocument();
  });

  it('says what the two marks mean, once, in the note above the values', async () => {
    await renderMarked({});
    const note = screen.getByText(/a clinician does not enter a median/i);
    expect(note).toHaveTextContent(
      /Each value is marked Accepted or Provisional by the mapping its readings came in under: provisional means the mapping of the device code is proposed and not yet approved\./);
  });
});

describe('WearableTab — samples stored as provisional or still waiting (#2835)', () => {
  const dropZone = () => screen.getByText(/or drag & drop files here/i).closest('div[class*="relative"]')!;

  it('says how many uploaded samples are provisional', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 5, duplicates_skipped: 0, provisional_samples: 3 } });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('activity.fit')]));

    expect(await screen.findByText(
      'Uploaded 5 samples. 3 provisional until their mapping is approved.')).toBeInTheDocument();
  });

  it('adds them up across the files of one upload, after the duplicates', async () => {
    mockPost
      .mockResolvedValueOnce({ data: { samples_created: 2, duplicates_skipped: 1, provisional_samples: 1 } })
      .mockResolvedValueOnce({ data: { samples_created: 3, duplicates_skipped: 0 } });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('a.fit'), createFile('b.fit')]));

    expect(await screen.findByText(
      'Uploaded 5 samples (1 duplicate skipped). 1 provisional until its mapping is approved.')).toBeInTheDocument();
  });

  it('says how many uploaded samples are stored and waiting for a mapping', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 5, duplicates_skipped: 0, unresolved_samples: 2 } });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('activity.fit')]));

    expect(await screen.findByText(
      'Uploaded 5 samples. 2 stored and waiting for a mapping.')).toBeInTheDocument();
  });

  it('counts a sample a proposal made provisional once: provisional first, then the rest still waiting', async () => {
    mockPost.mockResolvedValue({
      data: { samples_created: 9, duplicates_skipped: 0, unresolved_samples: 5, provisional_samples: 3 },
    });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('activity.fit')]));

    expect(await screen.findByText(
      'Uploaded 9 samples. 3 provisional until their mapping is approved. 2 stored and waiting for a mapping.',
    )).toBeInTheDocument();
  });

  it('says nothing is waiting when every unresolved sample became provisional', async () => {
    mockPost.mockResolvedValue({
      data: { samples_created: 4, duplicates_skipped: 0, unresolved_samples: 4, provisional_samples: 4 },
    });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('activity.fit')]));

    expect(await screen.findByText(
      'Uploaded 4 samples. 4 provisional until their mapping is approved.')).toBeInTheDocument();
    expect(screen.queryByText(/waiting for a mapping/i)).not.toBeInTheDocument();
  });

  it('says nothing about provisional samples when the upload stored none', async () => {
    mockPost.mockResolvedValue({ data: { samples_created: 5, duplicates_skipped: 0 } });
    renderTab();
    fireEvent.drop(dropZone(), createDropEvent([createFile('activity.fit')]));

    expect(await screen.findByText('Uploaded 5 samples')).toBeInTheDocument();
    expect(screen.queryByText(/provisional until/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting for a mapping/i)).not.toBeInTheDocument();
  });

  const SYNCED = {
    samples_created: 210, duplicates_skipped: 30, replaced: 12, unmapped_metrics: [],
    start_date: '2026-10-24', end_date: '2026-11-22', datasets_failed: [],
  };

  async function sync(data: Record<string, unknown>) {
    mockPost.mockResolvedValue({ data });
    renderTab({ canSyncOwnDevice: true });
    fireEvent.change(screen.getByLabelText('Garmin Connect Personal API key'), { target: { value: 'gcpk_TEST-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sync' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalled());
  }

  it('says how many synced values are provisional', async () => {
    await sync({ ...SYNCED, provisional_samples: 40, provisional_metrics: ['vo2_max'] });

    expect(await screen.findByText(
      'Synced 2026-10-24 to 2026-11-22: 210 new values, 12 replaced, 30 already stored. '
      + '40 provisional until their mapping is approved.')).toBeInTheDocument();
  });

  it('says how many synced values are stored and waiting for a mapping, after the provisional ones', async () => {
    await sync({ ...SYNCED, unresolved_samples: 70, unresolved_metrics: ['vo2_max', 'spo2'], provisional_samples: 40 });

    expect(await screen.findByText(
      'Synced 2026-10-24 to 2026-11-22: 210 new values, 12 replaced, 30 already stored. '
      + '40 provisional until their mapping is approved. 30 stored and waiting for a mapping.')).toBeInTheDocument();
  });

  it('says how many synced values wait when none is provisional', async () => {
    await sync({ ...SYNCED, unresolved_samples: 1, unresolved_metrics: ['vo2_max'] });

    expect(await screen.findByText(
      'Synced 2026-10-24 to 2026-11-22: 210 new values, 12 replaced, 30 already stored. '
      + '1 stored and waiting for a mapping.')).toBeInTheDocument();
  });

  it('says nothing about provisional values when the sync stored none', async () => {
    await sync(SYNCED);

    expect(await screen.findByText(
      'Synced 2026-10-24 to 2026-11-22: 210 new values, 12 replaced, 30 already stored.')).toBeInTheDocument();
    expect(screen.queryByText(/provisional until/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting for a mapping/i)).not.toBeInTheDocument();
  });
});
