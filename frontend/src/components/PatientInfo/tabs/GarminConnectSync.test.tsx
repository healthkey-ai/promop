/**
 * The saved Personal API key in the "Sync past days" part of the Wearables
 * tab's Garmin section (#2834). The sync itself is covered through the tab,
 * in WearableTab.test.tsx.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GarminConnectSync from './GarminConnectSync';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockDelete = vi.fn();
vi.mock('@/api/axios', () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    post: (...args: unknown[]) => mockPost(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

const KEY = 'gcpk_TEST-key.0123456789';
const KEY_URL = '/v1/patient-records/garmin-key/';
const SYNC_URL = '/v1/patient-records/sync-garmin/';
const keyStatus = (over: Record<string, unknown> = {}) => ({
  data: { can_save: true, saved: false, saved_at: null, last_used_at: null, ...over },
});
const synced = (over: Record<string, unknown> = {}) => ({
  data: {
    samples_created: 3, duplicates_skipped: 0, replaced: 0, unmapped_metrics: [],
    start_date: '2026-10-24', end_date: '2026-11-22', datasets_failed: [], ...over,
  },
});

const keyField = () => screen.getByLabelText(/Garmin Connect Personal API key/) as HTMLInputElement;
const syncButton = () => screen.getByRole('button', { name: 'Sync' });
const canSync = () => syncButton().getAttribute('aria-disabled') !== 'true';
const remember = () => screen.queryByLabelText('Save this key for next time') as HTMLInputElement | null;
const removeButton = () => screen.queryByRole('button', { name: 'Remove saved key' });
/** The body of the one sync request made. */
const sent = () => {
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost.mock.calls[0][0]).toBe(SYNC_URL);
  return mockPost.mock.calls[0][1] as Record<string, unknown>;
};

function renderSync(props: Partial<React.ComponentProps<typeof GarminConnectSync>> = {}) {
  const onSynced = vi.fn();
  render(<GarminConnectSync onSynced={onSynced} {...props} />);
  return { onSynced };
}

beforeEach(() => { mockGet.mockReset(); mockPost.mockReset(); mockDelete.mockReset(); });
afterEach(() => { vi.restoreAllMocks(); });

describe('GarminConnectSync: a key saved for later syncs', () => {
  it('asks whether a key is saved, and offers to save one where the site can', async () => {
    mockGet.mockResolvedValue(keyStatus());
    renderSync();
    expect(await screen.findByLabelText('Save this key for next time')).toBeChecked();
    expect(mockGet).toHaveBeenCalledWith(KEY_URL);
    expect(screen.getByText(/kept on this site, encrypted, for your next sync/)).toBeInTheDocument();
    expect(screen.queryByText(/is not stored/)).not.toBeInTheDocument();
    // Nothing saved yet: a key is still needed, and there is nothing to remove.
    expect(canSync()).toBe(false);
    expect(removeButton()).not.toBeInTheDocument();
  });

  it('says the key is not stored, and asks for a one-off sync, where the site cannot save', async () => {
    mockGet.mockResolvedValue(keyStatus({ can_save: false }));
    mockPost.mockResolvedValue(synced({ key_saved: false }));
    renderSync();
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(screen.getByText(/used for this sync only and is not stored/)).toBeInTheDocument();
    expect(remember()).not.toBeInTheDocument();
    await userEvent.type(keyField(), KEY);
    await userEvent.click(syncButton());
    await screen.findByText(/^Synced/);
    expect(sent()).toMatchObject({ api_key: KEY, remember: false });
    expect(removeButton()).not.toBeInTheDocument();
  });

  it('never claims "not stored" of a key that is: unknown status means a one-off sync', async () => {
    for (const answer of [Promise.reject(new Error('offline')), Promise.resolve({ data: [] })]) {
      mockGet.mockReset(); mockPost.mockReset();
      mockGet.mockReturnValue(answer);
      mockPost.mockResolvedValue(synced());
      const view = render(<GarminConnectSync onSynced={vi.fn()} />);
      await waitFor(() => expect(mockGet).toHaveBeenCalled());
      expect(screen.getByText(/is not stored/)).toBeInTheDocument();
      await userEvent.type(keyField(), KEY);
      await userEvent.click(syncButton());
      await screen.findByText(/^Synced/);
      expect(sent()).toMatchObject({ api_key: KEY, remember: false });
      view.unmount();
    }
  });

  it('saves the key with the sync when the box is left ticked, and shows it as saved', async () => {
    mockGet.mockResolvedValue(keyStatus());
    mockPost.mockResolvedValue(synced({ key_saved: true }));
    const { onSynced } = renderSync({ personId: 44 });
    await screen.findByLabelText('Save this key for next time');
    await userEvent.type(keyField(), KEY);
    await userEvent.click(syncButton());
    expect(await screen.findByText(/Your key is saved/)).toBeInTheDocument();
    expect(sent()).toMatchObject({ api_key: KEY, remember: true, person_id: 44 });
    expect(onSynced).toHaveBeenCalledWith(true);
    // The key is not kept in the page either.
    expect(keyField().value).toBe('');
    expect(screen.queryByDisplayValue(KEY)).not.toBeInTheDocument();
    expect(removeButton()).toBeInTheDocument();
    // With a key saved and none typed, the box has nothing to decide.
    expect(remember()).not.toBeInTheDocument();
  });

  it('syncs once, without saving, when the box is unticked', async () => {
    mockGet.mockResolvedValue(keyStatus());
    mockPost.mockResolvedValue(synced({ key_saved: false }));
    renderSync();
    await userEvent.click(await screen.findByLabelText('Save this key for next time'));
    await userEvent.type(keyField(), KEY);
    await userEvent.click(syncButton());
    await screen.findByText(/^Synced/);
    expect(sent()).toMatchObject({ api_key: KEY, remember: false });
    expect(screen.queryByText(/Your key is saved/)).not.toBeInTheDocument();
    expect(removeButton()).not.toBeInTheDocument();
  });

  it('syncs with the saved key when none is typed, sending no key at all', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true, saved_at: '2026-11-20T10:00:00+00:00' }));
    mockPost.mockResolvedValue(synced({ key_saved: true }));
    renderSync();
    expect(await screen.findByText(/Your key is saved/)).toBeInTheDocument();
    expect(screen.getByLabelText('Garmin Connect Personal API key (optional)')).toBeInTheDocument();
    expect(canSync()).toBe(true);
    await userEvent.click(syncButton());
    await screen.findByText(/^Synced/);
    const body = sent();
    expect(body).not.toHaveProperty('api_key');
    expect(body).not.toHaveProperty('remember');
    expect(body).toMatchObject({ days: 30 });
    expect(removeButton()).toBeInTheDocument();
  });

  it('replaces the saved key with one typed, and offers the choice again', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    mockPost.mockResolvedValue(synced({ key_saved: true }));
    renderSync();
    await screen.findByText(/Your key is saved/);
    expect(remember()).not.toBeInTheDocument();
    await userEvent.type(keyField(), KEY);
    expect(remember()).toBeChecked();
    await userEvent.click(syncButton());
    await screen.findByText(/^Synced/);
    expect(sent()).toMatchObject({ api_key: KEY, remember: true });
  });

  it('asks for a key again when Garmin refused the saved one and the server removed it', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    mockPost.mockRejectedValue({ response: { data: {
      error: 'Garmin no longer accepts your saved API key, so it has been removed.', key_saved: false,
    } } });
    renderSync();
    await screen.findByText(/Your key is saved/);
    await userEvent.click(syncButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('has been removed');
    expect(screen.queryByText(/Your key is saved/)).not.toBeInTheDocument();
    expect(removeButton()).not.toBeInTheDocument();
    expect(canSync()).toBe(false);
    expect(keyField()).toHaveFocus();
  });

  it('keeps the saved key shown when a sync fails for another reason', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    mockPost.mockRejectedValue({ response: { data: { error: 'Could not reach Garmin Connect. Try again later.' } } });
    renderSync();
    await screen.findByText(/Your key is saved/);
    await userEvent.click(syncButton());
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach Garmin Connect');
    expect(screen.getByText(/Your key is saved/)).toBeInTheDocument();
    expect(canSync()).toBe(true);
  });

  it('removes the saved key after asking, and then needs a key again', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    mockDelete.mockResolvedValue(keyStatus({ saved: false }));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    renderSync();
    await userEvent.click(await screen.findByRole('button', { name: 'Remove saved key' }));
    // Declined: nothing is asked of the server.
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();
    expect(removeButton()).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Remove saved key' }));
    expect(await screen.findByText('Your saved key has been removed.')).toBeInTheDocument();
    expect(mockDelete).toHaveBeenCalledWith(KEY_URL);
    expect(removeButton()).not.toBeInTheDocument();
    expect(canSync()).toBe(false);
    expect(remember()).toBeChecked();
    expect(keyField()).toHaveFocus();
  });

  it('says so, and keeps the key shown, when removing fails', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    mockDelete.mockRejectedValue(new Error('offline'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderSync();
    await userEvent.click(await screen.findByRole('button', { name: 'Remove saved key' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not remove your saved key');
    expect(removeButton()).toBeInTheDocument();
    expect(canSync()).toBe(true);
  });

  it('holds sync and removal while another write to the record is running', async () => {
    mockGet.mockResolvedValue(keyStatus({ saved: true }));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderSync({ disabled: true });
    const remove = await screen.findByRole('button', { name: 'Remove saved key' });
    expect(remove).toHaveAttribute('aria-disabled', 'true');
    expect(canSync()).toBe(false);
    await userEvent.click(remove);
    await userEvent.click(syncButton());
    expect(confirm).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
