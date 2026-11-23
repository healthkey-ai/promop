/**
 * The phone health app panel on the Wearables tab (#2810): get a one-time
 * connection code, see what is connected, and disconnect.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import OpenWearablesLink from './OpenWearablesLink';

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

const HOST = 'https://records.example.org';
const CODE = 'A3K9M7X2';
const LAST_SYNC = '2026-11-22T06:30:00+00:00';

const status = (over: Record<string, unknown> = {}) => ({
  data: {
    enabled: true, connected: false, devices: 0, code_pending: false, last_sync_at: null, providers: [],
    host: HOST, ...over,
  },
});
const connected = (over: Record<string, unknown> = {}) => status({
  connected: true, devices: 1, last_sync_at: LAST_SYNC, providers: ['apple'], ...over,
});
/** The answer to asking for a code: the status, the code and when it stops working. */
const invited = (over: Record<string, unknown> = {}) => ({
  status: 201,
  data: {
    ...status({ code_pending: true }).data, code: CODE,
    code_expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(), ...over,
  },
});
const clock = (value: string) => new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
/** A request that stays in flight until `finish` is called. */
function pending() {
  let finish: (value: unknown) => void = () => {};
  const promise = new Promise(resolve => { finish = resolve; });
  return { promise, finish: (value: unknown) => finish(value) };
}

beforeEach(() => {
  mockGet.mockReset(); mockPost.mockReset(); mockDelete.mockReset();
});
afterEach(() => { vi.restoreAllMocks(); });

describe('OpenWearablesLink', () => {
  it('shows nothing where connecting a phone is not set up, or when its state cannot be read', async () => {
    mockGet.mockResolvedValueOnce(status({ enabled: false }));
    const first = render(<OpenWearablesLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith(
      expect.stringContaining('/v1/open-wearables/connection/')));
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    // Switched off with a code waiting and no phone: still nothing to offer.
    mockGet.mockResolvedValueOnce(status({ enabled: false, code_pending: true }));
    const second = render(<OpenWearablesLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(second.container).toBeEmptyDOMElement();
    second.unmount();

    mockGet.mockRejectedValueOnce(new Error('down'));
    const third = render(<OpenWearablesLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(3));
    expect(third.container).toBeEmptyDOMElement();
    third.unmount();

    // An answer that is not a status at all (a list, say) offers nothing either.
    mockGet.mockResolvedValueOnce({ data: [] });
    const fourth = render(<OpenWearablesLink />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(4));
    expect(fourth.container).toBeEmptyDOMElement();
  });

  it.each([
    ['says it is connected', connected({ enabled: false, devices: 2, providers: ['apple', 'health_connect'] })],
    // The count alone is enough: a phone on record can be disconnected.
    ['only counts phones', connected({ enabled: false, connected: false, devices: 2,
      providers: ['apple', 'health_connect'] })],
  ])('still offers Disconnect, and no code, where connecting is switched off and the status %s', async (_name, answer) => {
    mockGet.mockResolvedValue(answer);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockResolvedValueOnce(status({ enabled: false }));
    const view = render(<OpenWearablesLink />);

    const last = new Date(LAST_SYNC).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    const line = await screen.findByText(/2 phones connected\./);
    expect(line).toHaveTextContent('Synced from Apple Health and Health Connect.');
    expect(line).toHaveTextContent(`Last sync ${last}.`);
    expect(screen.getByText('New phones cannot be connected on this server right now.')).toBeInTheDocument();
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Disconnect']);
    expect(mockPost).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(confirm.mock.calls[0][0]).toMatch(/^Disconnect all 2 phones\?/);
    expect(mockDelete).toHaveBeenCalledWith(expect.stringContaining('/v1/open-wearables/connection/'));
    // With nothing connected and nothing to connect, the panel has nothing left to show.
    await waitFor(() => expect(view.container).toBeEmptyDOMElement());
  });

  it('keeps the phone and Disconnect in view where connecting is switched off and disconnecting fails', async () => {
    mockGet.mockResolvedValue(connected({ enabled: false }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockRejectedValueOnce(new Error('down'));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not disconnect. Please try again.');
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Disconnect']);
  });

  it('takes the code off the screen, and every way of getting one, when connecting is switched off meanwhile', async () => {
    mockGet.mockResolvedValueOnce(connected())
      .mockResolvedValue(connected({ enabled: false, code_pending: true }));
    mockPost.mockResolvedValueOnce(invited({ ...connected({ code_pending: true }).data }));
    render(<OpenWearablesLink pollMs={20} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect another phone' }));
    expect(await screen.findByText('New phones cannot be connected on this server right now.')).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Disconnect']);
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
    // The code is forgotten, so nothing more is asked about it.
    const asked = mockGet.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 80));
    expect(mockGet.mock.calls.length).toBe(asked);
  });

  it('says what it connects, and offers a code to a patient with no phone connected', async () => {
    mockGet.mockResolvedValue(status());
    render(<OpenWearablesLink />);
    expect(await screen.findByRole('heading', { name: 'Phone health app' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Phone health app' }))
      .toHaveTextContent(/built on Open Wearables that reads Apple Health or Google Health Connect/);
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toHaveAttribute('aria-disabled', 'false');
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    expect(screen.queryByText(/connected\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/cannot be connected on this server/)).not.toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('gets a code and shows it with the server address and when it expires, sending the browser time zone', async () => {
    const resolved = Intl.DateTimeFormat().resolvedOptions();
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions')
      .mockReturnValue({ ...resolved, timeZone: 'America/Denver' });
    mockGet.mockResolvedValue(status());
    const expires = '2026-11-23T18:45:00+00:00';
    mockPost.mockResolvedValueOnce(invited({ code_expires_at: expires }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      expect.stringContaining('/v1/open-wearables/invitation-code/'), { time_zone: 'America/Denver' });
    const code = await screen.findByText(CODE);
    expect(code).toHaveClass('font-mono', 'text-3xl');
    // Announced politely, with the rest of what the patient types into the app.
    expect(code.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.getByText(HOST)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`This code works once and expires at ${clock(expires)}\\.`)))
      .toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toHaveAttribute('aria-disabled', 'false');
    expect(screen.queryByRole('button', { name: 'Get a connection code' })).not.toBeInTheDocument();
  });

  it('does not keep the code: after a reload the panel offers a new one', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited());
    const first = render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(CODE);
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain(CODE);
    first.unmount();

    // The server says a code is waiting; which one it is, only the old page knew.
    mockGet.mockResolvedValue(status({ code_pending: true }));
    render(<OpenWearablesLink />);
    expect(await screen.findByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('replaces the code on screen with a new one', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited()).mockResolvedValueOnce(invited({ code: 'B7T2Q9WZ' }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    await userEvent.click(screen.getByRole('button', { name: 'Get a new code' }));
    expect(await screen.findByText('B7T2Q9WZ')).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledTimes(2);
  });

  it('shows the address the page is on when the server names none', async () => {
    mockGet.mockResolvedValue(status({ host: '' }));
    mockPost.mockResolvedValueOnce(invited({ host: '' }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    expect(screen.getByText(window.location.origin)).toBeInTheDocument();
  });

  it('shows a connected phone with what has synced and when', async () => {
    mockGet.mockResolvedValue(connected({ devices: 2, providers: ['apple', 'health_connect'] }));
    render(<OpenWearablesLink />);
    const last = new Date(LAST_SYNC).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    const line = await screen.findByText(/2 phones connected\./);
    expect(line).toHaveTextContent('Synced from Apple Health and Health Connect.');
    expect(line).toHaveTextContent(`Last sync ${last}.`);
    expect(screen.getByRole('button', { name: 'Connect another phone' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Get a connection code' })).not.toBeInTheDocument();
  });

  it('says so when a connected phone has sent nothing yet, and names a source it does not know as sent', async () => {
    mockGet.mockResolvedValueOnce(connected({ last_sync_at: null, providers: [] }));
    const first = render(<OpenWearablesLink />);
    const line = await screen.findByText(/1 phone connected\./);
    expect(line).toHaveTextContent('No data received yet.');
    expect(line).not.toHaveTextContent('Synced from');
    first.unmount();

    mockGet.mockResolvedValueOnce(connected({ providers: ['health_connect', 'toString'] }));
    render(<OpenWearablesLink />);
    expect(await screen.findByText(/1 phone connected\./))
      .toHaveTextContent('Synced from Health Connect and toString.');
  });

  it('gives a connected patient a code for another phone, and keeps what is connected in view', async () => {
    mockGet.mockResolvedValue(connected());
    mockPost.mockResolvedValueOnce(invited({ ...connected({ code_pending: true }).data }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect another phone' }));
    expect(mockPost).toHaveBeenCalledWith(
      expect.stringContaining('/v1/open-wearables/invitation-code/'),
      { time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone });
    expect(await screen.findByText(CODE)).toBeInTheDocument();
    expect(screen.getByText(HOST)).toBeInTheDocument();
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
  });

  it('disconnects after asking, and says the stored values stay', async () => {
    mockGet.mockResolvedValue(connected());
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true);
    mockDelete.mockResolvedValueOnce(status());
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toMatch(/Values already stored stay/);
    expect(confirm.mock.calls[0][0]).toMatch(/remove them from the upload history/);
    expect(mockDelete).not.toHaveBeenCalled();
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(mockDelete).toHaveBeenCalledTimes(1);
    expect(mockDelete).toHaveBeenCalledWith(expect.stringContaining('/v1/open-wearables/connection/'));
    const notice = await screen.findByText(/Your phone is disconnected\./);
    expect(notice).toHaveTextContent('remove them from the upload history');
    expect(notice.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    expect(screen.queryByText(/phone connected\./)).not.toBeInTheDocument();
  });

  it('takes a code off the screen when the phones are disconnected: it no longer works', async () => {
    mockGet.mockResolvedValue(connected({ devices: 3 }));
    mockPost.mockResolvedValueOnce(invited({ ...connected({ devices: 3, code_pending: true }).data }));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockResolvedValueOnce(status());
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect another phone' }));
    await screen.findByText(CODE);
    await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(confirm.mock.calls[0][0]).toMatch(/^Disconnect all 3 phones\?/);
    expect(await screen.findByText(/Your phones are disconnected\./)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('keeps the connection shown when disconnecting fails', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockRejectedValueOnce(new Error('down'));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not disconnect. Please try again.');
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toHaveAttribute('aria-disabled', 'false');
  });

  it("shows the server's reason when a code is refused, and can be tried again", async () => {
    mockGet.mockResolvedValue(status());
    mockPost
      .mockRejectedValueOnce({ response: { status: 403, data: { error: 'This record is switched off.' } } })
      .mockResolvedValueOnce(invited());
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('This record is switched off.');
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();

    const again = screen.getByRole('button', { name: 'Get a connection code' });
    expect(again).toHaveAttribute('aria-disabled', 'false');
    await userEvent.click(again);
    expect(await screen.findByText(CODE)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    ['a limit, which arrives as detail',
      { response: { status: 429, data: { detail: 'Request was throttled. Expected available in 60 seconds.' } } },
      'Request was throttled.'],
    ['a request that never reached the server', new Error('Network Error'),
      'Could not get a connection code. Please try again.'],
    ['an error with no wording of its own', { response: { status: 500, data: '<html>' } },
      'Could not get a connection code. Please try again.'],
  ])('falls back to plain wording for %s', async (_name, failure, text) => {
    mockGet.mockResolvedValue(status());
    mockPost.mockRejectedValueOnce(failure);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
  });

  it('shows no code for an answer that carries none', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(status({ code_pending: true }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not get a connection code.');
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
  });

  it('holds both buttons while a code is being made, and sends one request however often it is pressed', async () => {
    mockGet.mockResolvedValue(connected());
    const request = pending();
    mockPost.mockReturnValue(request.promise);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Connect another phone' }));

    const getting = await screen.findByRole('button', { name: 'Getting a code...' });
    expect(getting).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('button', { name: 'Disconnect' })).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(getting);
    await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(confirm).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();

    request.finish(invited({ ...connected({ code_pending: true }).data }));
    expect(await screen.findByText(CODE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toHaveAttribute('aria-disabled', 'false');
    expect(screen.getByRole('button', { name: 'Disconnect' })).toHaveAttribute('aria-disabled', 'false');
  });

  it('holds both buttons while disconnecting', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const request = pending();
    mockDelete.mockReturnValue(request.promise);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));

    const disconnecting = await screen.findByRole('button', { name: 'Disconnecting...' });
    expect(disconnecting).toHaveAttribute('aria-disabled', 'true');
    const another = screen.getByRole('button', { name: 'Connect another phone' });
    expect(another).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(another);
    await userEvent.click(disconnecting);
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockDelete).toHaveBeenCalledTimes(1);

    request.finish(status());
    expect(await screen.findByRole('button', { name: 'Get a connection code' }))
      .toHaveAttribute('aria-disabled', 'false');
  });

  it('does nothing while another write to the record is running', async () => {
    mockGet.mockResolvedValue(connected());
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<OpenWearablesLink disabled />);
    const another = await screen.findByRole('button', { name: 'Connect another phone' });
    expect(another).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(another);
    await userEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    expect(mockPost).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it('notices the phone that used the code, and takes the code off the screen', async () => {
    // Still waiting on the first look after the code is made, used by the second.
    mockGet.mockResolvedValueOnce(status()).mockResolvedValueOnce(status({ code_pending: true }))
      .mockResolvedValue(connected({ last_sync_at: null, providers: [] }));
    mockPost.mockResolvedValueOnce(invited());
    render(<OpenWearablesLink pollMs={20} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);

    const notice = await screen.findByText(/Your phone is connected\./);
    expect(notice.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getByText(/1 phone connected\./)).toHaveTextContent('No data received yet.');
    expect(screen.getByRole('button', { name: 'Connect another phone' })).toBeInTheDocument();
    // Nothing more is asked once no code is on screen.
    const asked = mockGet.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 80));
    expect(mockGet.mock.calls.length).toBe(asked);
  });

  it('does not claim a phone connected when the code stopped working without one', async () => {
    // The connection is gone when next asked: disconnected from another page, say.
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited());
    render(<OpenWearablesLink pollMs={20} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    expect(await screen.findByText(/That code can no longer be used\./)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.queryByText(/Your phone is connected/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
  });

  it('takes an expired code off the screen without asking the server', async () => {
    mockGet.mockResolvedValueOnce(status());
    mockPost.mockResolvedValueOnce(invited({ code_expires_at: new Date(Date.now() + 40).toISOString() }));
    render(<OpenWearablesLink pollMs={60} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    expect(await screen.findByText(/That code has expired\./)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it('keeps the code on screen while the server cannot be asked', async () => {
    mockGet.mockResolvedValueOnce(status()).mockRejectedValue(new Error('down'));
    mockPost.mockResolvedValueOnce(invited());
    render(<OpenWearablesLink pollMs={20} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says how long the code lasts when the server does not say when it ends', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited({ code_expires_at: 'soon' }));
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    await screen.findByText(CODE);
    expect(screen.getByText(/This code works once and expires in 15 minutes\./)).toBeInTheDocument();
  });
});
