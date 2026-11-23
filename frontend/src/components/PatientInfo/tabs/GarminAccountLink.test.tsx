/**
 * The Garmin account panel on the Wearables tab (#2802): connect by OAuth,
 * what the callback said, and disconnect.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GarminAccountLink from './GarminAccountLink';

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

const status = (over: Record<string, unknown> = {}) => ({
  data: { configured: true, connected: false, connected_at: null, last_push_at: null, permissions: [], ...over },
});
const connected = (over: Record<string, unknown> = {}) => status({
  connected: true, connected_at: '2026-11-20T10:00:00+00:00', last_push_at: '2026-11-22T06:30:00+00:00',
  permissions: ['HEALTH_EXPORT'], ...over,
});

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockDelete.mockReset();
  window.history.replaceState(null, '', '/');
});
afterEach(() => { vi.restoreAllMocks(); });

describe('GarminAccountLink', () => {
  it('shows nothing where no Garmin app is set up, or when its state cannot be read', async () => {
    mockGet.mockResolvedValueOnce(status({ configured: false }));
    const first = render(<GarminAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(expect.stringContaining('/v1/garmin/connection/')));
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    mockGet.mockRejectedValueOnce(new Error('down'));
    const second = render(<GarminAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(second.container).toBeEmptyDOMElement();
    second.unmount();

    // An answer that is not a status at all (a list, say) offers nothing either.
    mockGet.mockResolvedValueOnce({ data: [] });
    const third = render(<GarminAccountLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    expect(third.container).toBeEmptyDOMElement();
  });

  it('sends a patient who is not connected to the consent page the server names', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValue({ data: { authorize_url: 'https://connect.garmin.com/oauth2Confirm?state=s' } });
    const leave = vi.fn();
    render(<GarminAccountLink leave={leave} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Garmin' }));
    expect(mockPost).toHaveBeenCalledWith(expect.stringContaining('/v1/garmin/oauth/start/'));
    await waitFor(() => expect(leave).toHaveBeenCalledWith('https://connect.garmin.com/oauth2Confirm?state=s'));
    // It stays busy: the page is on its way out.
    expect(screen.getByRole('button', { name: 'Opening Garmin...' })).toHaveAttribute('aria-disabled', 'true');
  });

  it('says why when connecting cannot start, and can be tried again', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockRejectedValueOnce({ response: { data: { error: 'Connecting a Garmin account is not set up on this server.' } } });
    const leave = vi.fn();
    render(<GarminAccountLink leave={leave} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Garmin' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not set up on this server');
    expect(leave).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Connect Garmin' })).toHaveAttribute('aria-disabled', 'false');
  });

  it('does not start while another write to the record is running', async () => {
    mockGet.mockResolvedValue(status());
    render(<GarminAccountLink disabled />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Garmin' }));
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('shows a connected account with when it was linked and when data last came', async () => {
    mockGet.mockResolvedValue(connected());
    render(<GarminAccountLink />);
    const since = new Date('2026-11-20T10:00:00+00:00').toLocaleDateString();
    const last = new Date('2026-11-22T06:30:00+00:00').toLocaleDateString();
    expect(await screen.findByText(new RegExp(`Connected since ${since}`))).toHaveTextContent(`Last received ${last}.`);
    expect(screen.getByRole('button', { name: 'Disconnect Garmin' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect Garmin' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Sharing health data was not allowed/)).not.toBeInTheDocument();
  });

  it('says so when nothing has come yet, and when health data was not allowed', async () => {
    mockGet.mockResolvedValue(connected({ last_push_at: null, permissions: ['ACTIVITY_EXPORT'] }));
    render(<GarminAccountLink />);
    expect(await screen.findByText(/Nothing has been received yet\./)).toBeInTheDocument();
    expect(screen.getByText(/Sharing health data was not allowed/)).toBeInTheDocument();
  });

  it.each([[[]], [['healthExport']], [['something']]])(
    'does not claim health data was refused when the permissions are not known (%j)', async (permissions) => {
      mockGet.mockResolvedValue(connected({ permissions }));
      render(<GarminAccountLink />);
      await screen.findByRole('button', { name: 'Disconnect Garmin' });
      expect(screen.queryByText(/Sharing health data was not allowed/)).not.toBeInTheDocument();
    });

  it('still offers Disconnect for a connected account where the Garmin app is no longer set up', async () => {
    mockGet.mockResolvedValue(connected({ configured: false }));
    render(<GarminAccountLink />);
    expect(await screen.findByRole('button', { name: 'Disconnect Garmin' })).toBeInTheDocument();
  });

  it('does not leave the page for an answer that names no consent page', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce({ data: {} });
    const leave = vi.fn();
    render(<GarminAccountLink leave={leave} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Garmin' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not start connecting');
    expect(leave).not.toHaveBeenCalled();
  });

  it('is usable again when the page is restored by the Back button', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValue({ data: { authorize_url: 'https://connect.garmin.com/oauth2Confirm?state=s' } });
    render(<GarminAccountLink leave={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect Garmin' }));
    await screen.findByRole('button', { name: 'Opening Garmin...' });
    const restored = new Event('pageshow');
    Object.defineProperty(restored, 'persisted', { value: true });
    window.dispatchEvent(restored);
    expect(await screen.findByRole('button', { name: 'Connect Garmin' })).toHaveAttribute('aria-disabled', 'false');
  });

  it('disconnects after asking, and says whether Garmin was told', async () => {
    mockGet.mockResolvedValue(connected());
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    mockDelete.mockResolvedValueOnce({ data: { disconnected: true, garmin_notified: true } });
    render(<GarminAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Garmin' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(mockDelete).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Disconnect Garmin' }));
    expect(mockDelete).toHaveBeenCalledWith(expect.stringContaining('/v1/garmin/connection/'));
    expect(await screen.findByText(/Garmin has been told to stop sending data/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Garmin' })).toBeInTheDocument();
  });

  it('tells the patient to remove the app in Garmin Connect when Garmin could not be told', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockResolvedValueOnce({ data: { disconnected: true, garmin_notified: false } });
    render(<GarminAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Garmin' }));
    expect(await screen.findByText(/Garmin could not be told/)).toHaveTextContent('Connected Apps');
  });

  it('keeps the connection shown when disconnecting fails', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockRejectedValueOnce(new Error('down'));
    render(<GarminAccountLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect Garmin' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not disconnect');
    expect(screen.getByRole('button', { name: 'Disconnect Garmin' })).toBeInTheDocument();
  });

  it.each([
    ['connected', /Your Garmin account is connected/],
    ['denied', /access was not approved/],
    ['in_use', /already connected to another record/],
    ['error', /could not be connected/],
    ['something-else', /could not be connected/],
    ['toString', /could not be connected/],
  ])('says what the callback reported (%s) and takes it out of the address', async (value, text) => {
    window.history.replaceState(null, '', `/?garmin=${value}&keep=1#top`);
    mockGet.mockResolvedValue(value === 'connected' ? connected() : status());
    render(<GarminAccountLink />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(window.location.search).toBe('?keep=1');
    expect(window.location.hash).toBe('#top');
  });
});
