/**
 * The phone health app panel on the Wearables tab (#2810): get a one-time
 * connection code, see what is connected, and disconnect.
 */
import { act, render, screen, waitFor } from '@testing-library/react';
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
/** When the phone already connected joined, and when a later one does. */
const JOINED = '2026-11-20T09:00:00+00:00';
const JOINED_LATER = '2026-11-23T18:31:00+00:00';
/** The server's name for the code on screen, and for one that replaced it. */
const CODE_ENDS = '2026-11-23T18:45:00+00:00';
const OTHER_CODE_ENDS = '2026-11-23T18:52:00+00:00';
const UNUSABLE = /That code can no longer be used\./;
const EXPIRED = /That code has expired\./;
const PHONE_CONNECTED = /Your phone is connected\./;

type Answer = { status?: number; data: Record<string, unknown> };

const status = (over: Record<string, unknown> = {}): Answer => ({
  data: {
    enabled: true, connected: false, devices: 0, code_pending: false, code_expires_at: null,
    last_connected_at: null, last_sync_at: null, providers: [], host: HOST, ...over,
  },
});
const connected = (over: Record<string, unknown> = {}) => status({
  connected: true, devices: 1, last_connected_at: JOINED, last_sync_at: LAST_SYNC, providers: ['apple'], ...over,
});
/** `base` with the code on screen still waiting to be used. */
const waiting = (base: Answer = status(), over: Record<string, unknown> = {}): Answer => ({
  data: { ...base.data, code_pending: true, code_expires_at: CODE_ENDS, ...over },
});
/** The answer to asking for a code: the status, the code and how long it lasts. */
const invited = (over: Record<string, unknown> = {}, base: Answer = status()): Answer => ({
  status: 201,
  data: { ...waiting(base).data, code: CODE, code_expires_in: 900, ...over },
});
const clock = (value: number) => new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
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

  it('gets a code and shows it with the server address, sending the browser time zone', async () => {
    const resolved = Intl.DateTimeFormat().resolvedOptions();
    vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions')
      .mockReturnValue({ ...resolved, timeZone: 'America/Denver' });
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited());
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      expect.stringContaining('/v1/open-wearables/invitation-code/'), { time_zone: 'America/Denver' });
    const code = await screen.findByText(CODE);
    expect(code.parentElement).toHaveClass('font-mono', 'text-3xl');
    // A screen reader is given the characters one at a time, not the code as a word.
    expect(code).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByText('A 3 K 9 M 7 X 2')).toHaveClass('sr-only');
    // Announced politely, with the rest of what the patient types into the app.
    expect(code.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.getByText(HOST)).toBeInTheDocument();
    expect(screen.getByText(/This code works once and expires at /)).toBeInTheDocument();
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
    mockGet.mockResolvedValue(waiting());
    render(<OpenWearablesLink />);
    expect(await screen.findByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('replaces the code on screen with a new one', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(invited())
      .mockResolvedValueOnce(invited({ code: 'B7T2Q9WZ', code_expires_at: OTHER_CODE_ENDS }));
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
    mockPost.mockResolvedValueOnce(invited({}, connected()));
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
    mockPost.mockResolvedValueOnce(invited({}, connected({ devices: 3 })));
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

  const THROTTLED = {
    response: { status: 429, data: { detail: 'Request was throttled. Expected available in 3412 seconds.' } },
  };

  it('says in plain words that too many codes were asked for, not how many seconds the server counted', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockRejectedValueOnce(THROTTLED);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/^Too many codes requested\. Try again in a few minutes\.$/);
    expect(alert).not.toHaveTextContent(/throttled|3412/);
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toHaveAttribute('aria-disabled', 'false');
  });

  it('says in plain words that disconnecting was limited, and keeps the connection shown', async () => {
    mockGet.mockResolvedValue(connected());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockRejectedValueOnce(THROTTLED);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/^Too many requests\. Try again in a few minutes\.$/);
    expect(alert).not.toHaveTextContent(/throttled|3412/);
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
  });

  it.each([
    ['a request that never reached the server', new Error('Network Error')],
    ['an error with no wording of its own', { response: { status: 500, data: '<html>' } }],
  ])('falls back to plain wording for %s', async (_name, failure) => {
    mockGet.mockResolvedValue(status());
    mockPost.mockRejectedValueOnce(failure);
    render(<OpenWearablesLink />);
    await userEvent.click(await screen.findByRole('button', { name: 'Get a connection code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/^Could not get a connection code\. Please try again\.$/);
  });

  it('shows no code for an answer that carries none', async () => {
    mockGet.mockResolvedValue(status());
    mockPost.mockResolvedValueOnce(waiting());
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

    request.finish(invited({}, connected()));
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
});

/**
 * A code on screen is watched: every ten seconds the panel asks whether a
 * phone has used it, and once more when its time is up. The clock here is
 * the test's, moved by hand, so nothing depends on how fast the test runs.
 */
describe('OpenWearablesLink, while a code is on screen', () => {
  /** When the browser thinks it is: ten seconds into a minute, so no test crosses one by accident. */
  const NOW = new Date('2026-11-23T18:30:10Z').getTime();
  const LIFETIME = 900_000;
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(NOW);
    user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  });
  afterEach(() => { vi.useRealTimers(); });

  /** Moves the clock on, by one look at the server unless told otherwise. */
  const pass = (ms = 10_000) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  /** How often the server has been asked where the connection stands. */
  const asked = () => mockGet.mock.calls.length;

  /** The panel with a code just made; the first look found `initial`. */
  async function showCode(initial: Answer = status(), made: Answer = invited({}, initial)) {
    mockGet.mockResolvedValueOnce(initial).mockResolvedValue(waiting(initial));
    mockPost.mockResolvedValueOnce(made);
    const view = render(<OpenWearablesLink />);
    await user.click(await screen.findByRole('button', {
      name: initial.data.connected ? 'Connect another phone' : 'Get a connection code',
    }));
    await screen.findByText(CODE);
    return view;
  }

  it('asks every ten seconds, and keeps the code while the server says it is still waiting', async () => {
    await showCode();
    expect(asked()).toBe(1);
    await pass(5_000);
    expect(asked()).toBe(1);
    await pass(5_000);
    expect(asked()).toBe(2);
    await pass(30_000);
    expect(asked()).toBe(5);
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
  });

  it.each([
    ['five hours ahead of', '2026-11-23T13:45:00+00:00'],
    ['five hours behind', '2026-11-23T23:45:00+00:00'],
  ])('times the code from when it arrived, with a browser clock %s the server', async (_name, serverEnds) => {
    await showCode(status(), invited({ code_expires_at: serverEnds }));
    // Fifteen minutes from now by this browser, whatever instant the server named.
    expect(screen.getByText(new RegExp(`This code works once and expires at ${clock(NOW + LIFETIME)}\\.`)))
      .toBeInTheDocument();
    expect(clock(NOW + LIFETIME)).not.toBe(clock(new Date(serverEnds).getTime()));

    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: serverEnds }));
    await pass();
    await pass();
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.queryByText(EXPIRED)).not.toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
  });

  it.each([
    ['missing', {}],
    ['not a number', { code_expires_in: 'soon' }],
    ['not a time at all', { code_expires_in: -5 }],
  ])('gives the code fifteen minutes when how long it lasts is %s', async (_name, over) => {
    const made = invited(over);
    if (!('code_expires_in' in over)) delete made.data.code_expires_in;
    await showCode(status(), made);
    expect(screen.getByText(new RegExp(`expires at ${clock(NOW + LIFETIME)}\\.`))).toBeInTheDocument();
    // Still on screen a few seconds short of fifteen minutes.
    await pass(LIFETIME - 5_000);
    expect(screen.getByText(CODE)).toBeInTheDocument();
  });

  it('uses the lifetime the server gives', async () => {
    await showCode(status(), invited({ code_expires_in: 300 }));
    expect(screen.getByText(new RegExp(`expires at ${clock(NOW + 300_000)}\\.`))).toBeInTheDocument();
    await pass(295_000);
    expect(screen.getByText(CODE)).toBeInTheDocument();
    mockGet.mockResolvedValue(status());
    await pass();
    expect(screen.getByText(EXPIRED)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('notices the phone that used the code, takes the code off the screen and asks no more', async () => {
    await showCode();
    await pass();
    expect(screen.getByText(CODE)).toBeInTheDocument();

    mockGet.mockResolvedValue(connected({ last_connected_at: JOINED_LATER, last_sync_at: null, providers: [] }));
    await pass();
    const notice = screen.getByText(PHONE_CONNECTED);
    expect(notice.closest('[aria-live="polite"]')).not.toBeNull();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    // The connection shown is the one that answer described.
    expect(screen.getByText(/1 phone connected\./)).toHaveTextContent('No data received yet.');
    expect(screen.getByRole('button', { name: 'Connect another phone' })).toBeInTheDocument();
    const before = asked();
    await pass(60_000);
    expect(asked()).toBe(before);
  });

  it('notices a sixth phone that displaces the oldest of five: the count does not move, the newest joining does', async () => {
    const five = connected({ devices: 5, providers: ['apple'] });
    await showCode(five);
    expect(screen.getByText(/5 phones connected\./)).toHaveTextContent('Synced from Apple Health.');
    await pass();
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.queryByText(PHONE_CONNECTED)).not.toBeInTheDocument();

    mockGet.mockResolvedValue(connected({
      devices: 5, last_connected_at: JOINED_LATER, providers: ['apple', 'health_connect'],
    }));
    await pass();
    expect(screen.getByText(PHONE_CONNECTED)).toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getByText(/5 phones connected\./)).toHaveTextContent('Synced from Apple Health and Health Connect.');
  });

  it('does not claim a phone connected when the code stopped working without one', async () => {
    // One fewer phone and no code waiting: a phone signed out, and the code was used up or withdrawn.
    await showCode(connected({ devices: 2 }));
    mockGet.mockResolvedValue(connected({ devices: 1 }));
    await pass();
    expect(screen.getByText(UNUSABLE)).toBeInTheDocument();
    expect(screen.queryByText(PHONE_CONNECTED)).not.toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect another phone' })).toBeInTheDocument();
    const before = asked();
    await pass(60_000);
    expect(asked()).toBe(before);
  });

  it('takes a code off the screen when a newer one replaced it elsewhere', async () => {
    await showCode();
    // Another page of the same patient made a code: one is pending, but not this one.
    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: OTHER_CODE_ENDS }));
    await pass();
    expect(screen.getByText(UNUSABLE)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
  });

  it('takes the old code off the screen when a new one was made but its answer was lost', async () => {
    await showCode();
    mockPost.mockRejectedValueOnce(new Error('Network Error'));
    await user.click(screen.getByRole('button', { name: 'Get a new code' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not get a connection code.');
    // Nothing says yet that the old code is dead.
    expect(screen.getByText(CODE)).toBeInTheDocument();

    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: OTHER_CODE_ENDS }));
    await pass();
    expect(screen.getByText(UNUSABLE)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('cannot tell a replaced code where the server does not name the pending one, and keeps it', async () => {
    await showCode(status(), invited({ code_expires_at: null }));
    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: OTHER_CODE_ENDS }));
    await pass();
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
  });

  it.each([
    ['says a phone joined', () => connected({ last_connected_at: JOINED_LATER }), PHONE_CONNECTED],
    ['names another code as the one pending', () => waiting(status(), { code_expires_at: OTHER_CODE_ENDS }), UNUSABLE],
    ['says no code is pending', () => status(), EXPIRED],
    // The browser's fifteen minutes are up even if the server still counts this code.
    ['still names this code', () => waiting(), EXPIRED],
    ['is an error', () => Promise.reject({ response: { status: 503, data: '<html>' } }), EXPIRED],
    ['never arrives', () => Promise.reject(new Error('Network Error')), EXPIRED],
  ])('looks once more when the time is up, and says what that answer %s means', async (_name, answer, text) => {
    await showCode();
    // Every look before the last finds the code still waiting.
    await pass(LIFETIME - 5_000);
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(asked()).toBe(90);

    mockGet.mockImplementation(() => Promise.resolve().then(answer));
    await pass();
    expect(asked()).toBe(91);
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    for (const other of [PHONE_CONNECTED, UNUSABLE, EXPIRED].filter(each => each !== text)) {
      expect(screen.queryByText(other)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // That was the last look.
    await pass(120_000);
    expect(asked()).toBe(91);
  });

  it('asks nothing more once the panel is gone', async () => {
    const view = await showCode();
    await pass();
    expect(asked()).toBe(2);
    view.unmount();
    await pass(LIFETIME + 60_000);
    expect(asked()).toBe(2);
  });

  it('drops an answer that arrives after the panel is gone', async () => {
    const view = await showCode();
    const look = pending();
    mockGet.mockReturnValue(look.promise);
    await pass();
    expect(asked()).toBe(2);
    view.unmount();
    const complaint = vi.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { look.finish(connected({ last_connected_at: JOINED_LATER })); });
    expect(complaint).not.toHaveBeenCalled();
    expect(view.container).toBeEmptyDOMElement();
  });

  it('does not ask again over a look that has not been answered', async () => {
    await showCode();
    const look = pending();
    mockGet.mockReturnValue(look.promise);
    await pass(40_000);
    expect(asked()).toBe(2);

    await act(async () => { look.finish(waiting()); });
    mockGet.mockResolvedValue(waiting());
    await pass();
    expect(asked()).toBe(3);
    expect(screen.getByText(CODE)).toBeInTheDocument();
  });

  it('leaves a new code alone when a look at the old one is answered after it', async () => {
    await showCode();
    const look = pending();
    mockGet.mockReturnValueOnce(look.promise);
    await pass();
    expect(asked()).toBe(2);

    mockPost.mockResolvedValueOnce(invited({ code: 'B7T2Q9WZ', code_expires_at: OTHER_CODE_ENDS }));
    await user.click(screen.getByRole('button', { name: 'Get a new code' }));
    await screen.findByText('B7T2Q9WZ');

    // Asked about the old code: by now none is pending under its name, and a phone has joined.
    await act(async () => { look.finish(connected({ last_connected_at: JOINED_LATER })); });
    expect(screen.getByText('B7T2Q9WZ')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toBeInTheDocument();
    expect(screen.queryByText(PHONE_CONNECTED)).not.toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
    expect(screen.queryByText(/phone connected\./)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();

    // The new code is watched in its turn.
    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: OTHER_CODE_ENDS }));
    await pass();
    expect(screen.getByText('B7T2Q9WZ')).toBeInTheDocument();
  });

  it('shows a new code without the verdict on the one before it', async () => {
    await showCode();
    const making = pending();
    mockPost.mockReturnValueOnce(making.promise);
    await user.click(screen.getByRole('button', { name: 'Get a new code' }));
    await screen.findByRole('button', { name: 'Getting a code...' });

    // The server has made the new code already; a look says the one on screen is replaced.
    mockGet.mockResolvedValue(waiting(status(), { code_expires_at: OTHER_CODE_ENDS }));
    await pass();
    expect(screen.getByText(UNUSABLE)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();

    await act(async () => { making.finish(invited({ code: 'B7T2Q9WZ', code_expires_at: OTHER_CODE_ENDS })); });
    expect(await screen.findByText('B7T2Q9WZ')).toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toBeInTheDocument();
  });

  it('does not bring a phone back when a look is answered after disconnecting', async () => {
    await showCode(connected());
    const look = pending();
    mockGet.mockReturnValueOnce(look.promise);
    await pass();
    expect(asked()).toBe(2);

    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockDelete.mockResolvedValueOnce(status());
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));
    await screen.findByText(/Your phone is disconnected\./);
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();

    // What the server said before the disconnect reached it.
    await act(async () => { look.finish(waiting(connected())); });
    expect(screen.queryByText(/1 phone connected\./)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    expect(screen.getByText(/Your phone is disconnected\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a connection code' })).toBeInTheDocument();
    const before = asked();
    await pass(60_000);
    expect(asked()).toBe(before);
  });

  it.each([
    ['a limit', () => Promise.reject({
      response: { status: 429, data: { detail: 'Request was throttled. Expected available in 12 seconds.' } },
    })],
    ['a server error', () => Promise.reject({ response: { status: 503, data: '<html>' } })],
    ['a request that never reached the server', () => Promise.reject(new Error('Network Error'))],
    // A client that does not throw on an error status hands the error body back as an answer.
    ['a limit handed back as an answer',
      () => Promise.resolve({ status: 429, data: { detail: 'Request was throttled.' } })],
    ['a server error handed back as an answer',
      () => Promise.resolve({ status: 500, data: { ...status().data } })],
    ['an error body with no status code', () => Promise.resolve({ data: { detail: 'Server error.' } })],
    ['no answer at all', () => Promise.resolve(undefined)],
  ])('keeps the code on screen through %s, and goes on asking', async (_name, failure) => {
    await showCode();
    mockGet.mockImplementation(failure);
    await pass();
    await pass();
    expect(asked()).toBe(3);
    expect(screen.getByText(CODE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Get a new code' })).toHaveAttribute('aria-disabled', 'false');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
    expect(screen.queryByText(EXPIRED)).not.toBeInTheDocument();

    // The next answer that can be read is acted on.
    mockGet.mockReset();
    mockGet.mockResolvedValue(connected({ last_connected_at: JOINED_LATER }));
    await pass();
    expect(screen.getByText(PHONE_CONNECTED)).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it('takes the code off the screen, and every way of getting one, when connecting is switched off meanwhile', async () => {
    await showCode(connected());
    mockGet.mockResolvedValue(waiting(connected({ enabled: false })));
    await pass();
    expect(screen.getByText('New phones cannot be connected on this server right now.')).toBeInTheDocument();
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Disconnect']);
    expect(screen.getByText(/1 phone connected\./)).toBeInTheDocument();
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument();
    // The code is forgotten, so nothing more is asked about it.
    const before = asked();
    await pass(60_000);
    expect(asked()).toBe(before);
  });
});
