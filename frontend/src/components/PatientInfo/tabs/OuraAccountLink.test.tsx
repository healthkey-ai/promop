/**
 * The linked-account part of the Wearables tab's Oura Ring section (#2900):
 * connect by OAuth, the sync that reads Oura's API, what the callback said, and
 * disconnect.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OuraAccountLink from './OuraAccountLink';

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

const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3600 * 1000).toISOString();

const status = (over: Record<string, unknown> = {}) => ({
  data: { configured: true, connected: false, connected_at: null, last_sync_at: null, synced_through: null, ...over },
});
/** Connected, and synced an hour ago, so nothing is read when the tab opens. */
const connected = (over: Record<string, unknown> = {}) => status({
  connected: true, connected_at: '2026-11-20T10:00:00+00:00', last_sync_at: hoursAgo(1),
  synced_through: '2026-11-26', ...over,
});
const synced = (over: Record<string, unknown> = {}) => ({
  data: {
    samples_created: 12, replaced: 3, start_date: '2026-08-28', end_date: '2026-11-26',
    unavailable: [], incomplete: false, ...over,
  },
});

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockDelete.mockReset();
  window.history.replaceState(null, '', '/');
});
afterEach(() => { vi.restoreAllMocks(); });

describe('OuraAccountLink', () => {
  it('shows nothing where no Oura app is set up, or when its state cannot be read', async () => {
    mockGet.mockResolvedValueOnce(status({ configured: false }));
    const first = render(<OuraAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(expect.stringContaining('/v1/oura/connection/')));
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    mockGet.mockRejectedValueOnce(new Error('down'));
    const second = render(<OuraAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(second.container).toBeEmptyDOMElement();
    second.unmount();

    mockGet.mockResolvedValueOnce({ data: [] });
    const third = render(<OuraAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    expect(third.container).toBeEmptyDOMElement();
  });

  it('sends a patient who is not connected to the consent page the server names', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValue({ data: { authorize_url: 'https://cloud.ouraring.com/oauth/authorize?state=s' } });
    const leave = vi.fn();
    render(<OuraAccountLink leave={leave} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Oura' }));
    expect(mockPost).toHaveBeenCalledWith(expect.stringContaining('/v1/oura/oauth/start/'));
    await waitFor(() => expect(leave).toHaveBeenCalledWith('https://cloud.ouraring.com/oauth/authorize?state=s'));
    // It stays busy: the page is on its way out.
    expect(screen.getByRole('button', { name: 'Opening Oura...' })).toHaveAttribute('aria-disabled', 'true');
    // Nothing is read before they are connected.
    expect(mockPost).not.toHaveBeenCalledWith(expect.stringContaining('/v1/oura/sync/'), expect.anything());
  });

  it('says why when connecting cannot start, and does not leave for an answer with no consent page', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockRejectedValueOnce({ response: { data: { error: 'Connecting an Oura account is not set up on this server.' } } });
    const leave = vi.fn();
    render(<OuraAccountLink leave={leave} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Oura' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not set up on this server');
    mockPost.mockResolvedValueOnce({ data: {} });
    await userEvent.click(screen.getByRole('button', { name: 'Connect Oura' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not start connecting'));
    expect(leave).not.toHaveBeenCalled();
  });

  it('does not start or sync while another write to the record is running', async () => {
    mockGet.mockResolvedValue(status());
    const first = render(<OuraAccountLink disabled />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Oura' }));
    expect(mockPost).not.toHaveBeenCalled();
    first.unmount();

    mockGet.mockResolvedValue(connected());
    render(<OuraAccountLink disabled />);
    expect(await screen.findByRole('button', { name: 'Sync now' })).toBeDisabled();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('shows a connected account with when it was linked and last synced, and reads nothing yet', async () => {
    mockGet.mockResolvedValue(connected());
    render(<OuraAccountLink />);
    const since = new Date('2026-11-20T10:00:00+00:00').toLocaleDateString();
    expect(await screen.findByText(new RegExp(`Connected since ${since}`))).toHaveTextContent('Last synced');
    expect(screen.getByRole('button', { name: 'Disconnect Oura' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect Oura' })).not.toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('syncs when asked, for its own record, and says what was stored', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockResolvedValue(synced({ provisional_samples: 4 }));
    const onSynced = vi.fn();
    const onBusyChange = vi.fn();
    render(<OuraAccountLink personId={7} onSynced={onSynced} onBusyChange={onBusyChange} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    expect(mockPost).toHaveBeenCalledWith(expect.stringContaining('/v1/oura/sync/'), { person_id: 7 });
    const text = await screen.findByText(/Synced 2026-08-28 to 2026-11-26/);
    expect(text).toHaveTextContent('12 new values, 3 replaced.');
    expect(text).toHaveTextContent('4 are provisional');
    expect(onSynced).toHaveBeenCalledWith(true);
    expect(onBusyChange.mock.calls.map(call => call[0])).toEqual([true, false]);
  });

  it('says so when a sync changed nothing, and when the server skipped it', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockResolvedValueOnce(synced({ samples_created: 0, replaced: 0 }));
    const onSynced = vi.fn();
    render(<OuraAccountLink onSynced={onSynced} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    expect(await screen.findByText(/0 new values/)).toBeInTheDocument();
    expect(onSynced).toHaveBeenCalledWith(false);

    mockPost.mockResolvedValueOnce({ data: { skipped: 'recent', samples_created: 0, replaced: 0 } });
    await userEvent.click(screen.getByRole('button', { name: 'Sync now' }));
    expect(await screen.findByText('Your Oura data is already up to date.')).toBeInTheDocument();
  });

  it('names the data Oura did not share, and says when only part was read', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockResolvedValue(synced({ unavailable: ['spo2', 'vo2'], incomplete: true }));
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    const text = await screen.findByText(/Oura did not share blood oxygen, VO₂ max/);
    expect(text).toHaveTextContent('only part of the range was read');
  });

  it('reads the days when the tab opens and the last sync is old', async () => {
    mockGet.mockResolvedValue(connected({ last_sync_at: hoursAgo(30) }));
    mockPost.mockResolvedValue(synced());
    render(<OuraAccountLink personId={7} />);
    expect(await screen.findByText(/Synced 2026-08-28/)).toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(expect.stringContaining('/v1/oura/sync/'), { person_id: 7 });
  });

  it('does not read the days by itself while another write to the record is running', async () => {
    mockGet.mockResolvedValue(connected({ last_sync_at: hoursAgo(30) }));
    render(<OuraAccountLink disabled />);
    expect(await screen.findByRole('button', { name: 'Sync now' })).toBeDisabled();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('still tells the tab its rows were written when it is left before the sync ends', async () => {
    mockGet.mockResolvedValue(connected());
    let finish: (value: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const onSynced = vi.fn();
    const onBusyChange = vi.fn();
    const view = render(<OuraAccountLink onSynced={onSynced} onBusyChange={onBusyChange} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    view.unmount();
    finish(synced());
    await waitFor(() => expect(onSynced).toHaveBeenCalledWith(true));
    expect(onBusyChange.mock.calls.map(call => call[0])).toEqual([true, false]);
  });

  it('reads the days at once after connecting, even though the account is new', async () => {
    window.history.replaceState(null, '', '/?oura=connected&keep=1#top');
    mockGet.mockResolvedValue(connected({ last_sync_at: null }));
    mockPost.mockResolvedValue(synced());
    render(<OuraAccountLink />);
    expect(await screen.findByText('Your Oura account is connected.')).toBeInTheDocument();
    expect(await screen.findByText(/Synced 2026-08-28/)).toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(window.location.search).toBe('?keep=1');
    expect(window.location.hash).toBe('#top');
  });

  it('offers Connect again when Oura no longer accepts the connection', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockRejectedValueOnce({ response: { status: 409, data: {
      error: 'Oura no longer accepts this connection. Connect your Oura account again.', reconnect: true } } });
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connect your Oura account again');
    expect(screen.getByRole('button', { name: 'Connect again' })).toBeInTheDocument();
  });

  it('shows a failed sync without offering to connect again', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockRejectedValueOnce({ response: { status: 502, data: { error: 'Could not reach Oura. Try again later.', reconnect: false } } });
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sync now' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach Oura');
    expect(screen.queryByRole('button', { name: 'Connect again' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sync now' })).toBeEnabled();
  });

  it('still offers Disconnect for a connected account where the Oura app is no longer set up', async () => {
    mockGet.mockResolvedValue(connected({ configured: false }));
    render(<OuraAccountLink />);
    expect(await screen.findByRole('button', { name: 'Disconnect Oura' })).toBeInTheDocument();
  });

  it('is usable again when the page is restored by the Back button', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValue({ data: { authorize_url: 'https://cloud.ouraring.com/oauth/authorize?state=s' } });
    render(<OuraAccountLink leave={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Oura' }));
    await screen.findByRole('button', { name: 'Opening Oura...' });
    const restored = new Event('pageshow');
    Object.defineProperty(restored, 'persisted', { value: true });
    window.dispatchEvent(restored);
    expect(await screen.findByRole('button', { name: 'Connect Oura' })).toHaveAttribute('aria-disabled', 'false');
  });

  it('disconnects after asking, and says whether Oura was told', async () => {
    mockGet.mockResolvedValue(connected());
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    mockDelete.mockResolvedValueOnce({ data: { disconnected: true, oura_notified: true } });
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Oura' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Disconnect Oura' }));
    expect(mockDelete).toHaveBeenCalledWith(expect.stringContaining('/v1/oura/connection/'));
    expect(await screen.findByText(/Oura has been told to end this access/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Oura' })).toBeInTheDocument();
  });

  it('tells the patient to remove the app in Oura when Oura could not be told', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockResolvedValueOnce({ data: { disconnected: true, oura_notified: false } });
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Oura' }));
    expect(await screen.findByText(/Oura could not be told/)).toHaveTextContent('Connected apps');
  });

  it('keeps the connection shown when disconnecting fails', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockRejectedValueOnce(new Error('down'));
    render(<OuraAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Oura' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not disconnect');
    expect(screen.getByRole('button', { name: 'Disconnect Oura' })).toBeInTheDocument();
  });

  it.each([
    ['denied', /access was not approved/],
    ['error', /could not be connected/],
    ['something-else', /could not be connected/],
    ['toString', /could not be connected/],
  ])('says what the callback reported (%s) and takes it out of the address', async (value, text) => {
    window.history.replaceState(null, '', `/?oura=${value}&keep=1`);
    mockGet.mockResolvedValue(status());
    render(<OuraAccountLink />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(window.location.search).toBe('?keep=1');
    expect(mockPost).not.toHaveBeenCalled();
  });
});
