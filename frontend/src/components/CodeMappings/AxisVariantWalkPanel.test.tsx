import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AxisVariantWalkPanel, { POLL_MS, WALK_URL, type Walk, type WalkStatus } from './AxisVariantWalkPanel';

const get = vi.fn();
const post = vi.fn();
vi.mock('@/api/axios', () => ({ default: {
  get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args),
} }));

const done: Walk = {
  id: 2, outcome: 'done', reason: 'migration 0307', rebuild_evidence: true, keys: 41000, done: 41000, refreshed: 1234,
  failure_reason: '', created_at: '2026-10-25T16:00:00Z', started_at: '2026-10-25T16:11:00Z',
  progress_at: '2026-10-25T19:00:00Z', completed_at: '2026-10-25T19:00:00Z',
  axis_evidence: { codes: 41000, axes: {
    component: { settled: 0, hint: 0, varies: 0, bases: {} },
    property: { settled: 30000, hint: 400, varies: 400, bases: { unit: 30000 } },
    time_aspect: { settled: 0, hint: 12, varies: 0, bases: {} },
    system: { settled: 3, hint: 0, varies: 1, bases: { specimen: 3 } },
    scale_type: { settled: 30500, hint: 0, varies: 0, bases: { value_type: 30500 } },
    method_type: { settled: 0, hint: 0, varies: 0, bases: {} },
  } },
};
const idle: WalkStatus = { active: null, last: done, patients_since: 57, walked_at: done.started_at };
const running: WalkStatus = {
  ...idle, active: { ...done, id: 3, outcome: 'running', done: 1500, keys: 41200, completed_at: null, axis_evidence: null },
};

beforeEach(() => { get.mockReset(); post.mockReset(); });
afterEach(() => { vi.useRealTimers(); });

async function renderPanel(canApprove = true) {
  await act(async () => { render(<AxisVariantWalkPanel canApprove={canApprove} />); });
}

describe('AxisVariantWalkPanel', () => {
  it('shows the last walk, the patients loaded since, and the axis evidence it counted', async () => {
    get.mockResolvedValue({ data: idle });
    await renderPanel();
    expect(get).toHaveBeenCalledWith(WALK_URL);
    const panel = screen.getByRole('region', { name: 'Source-code walk' });
    expect(panel).toHaveTextContent(/Source codes walked .* \(1,234 refreshed\)/);
    expect(panel).toHaveTextContent('57 patients loaded since');
    expect(within(panel).getByText('Axis evidence across 41,000 codes')).toBeInTheDocument();
    const rows = within(within(panel).getByRole('table', { name: 'LOINC axis evidence' })).getAllByRole('row');
    expect(rows[2]).toHaveTextContent(/Property\s*30,000\s*unit 30,000\s*400\s*400/);
    expect(rows[4]).toHaveTextContent(/System\s*3\s*specimen 3\s*0\s*1/);
    expect(rows[1]).toHaveTextContent(/Component\s*0\s*—\s*0\s*0/);
  });

  it('starts a rebuild walk and shows the request it made', async () => {
    get.mockResolvedValue({ data: idle });
    post.mockResolvedValue({ data: { ...running, created: true } });
    await renderPanel();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Walk source codes' })); });
    expect(post).toHaveBeenCalledWith(WALK_URL);
    expect(screen.getByRole('status')).toHaveTextContent('Walking source codes: 1,500 of 41,200 codes.');
    expect(screen.queryByRole('button', { name: 'Walk source codes' })).toBeNull();
  });

  it('polls while a walk is active and stops once it finishes', async () => {
    vi.useFakeTimers();
    get.mockResolvedValueOnce({ data: running }).mockResolvedValueOnce({ data: { ...running, active: { ...running.active!, done: 3000 } } })
      .mockResolvedValue({ data: idle });
    await renderPanel();
    expect(screen.getByRole('status')).toHaveTextContent('1,500 of 41,200');
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByRole('status')).toHaveTextContent('3,000 of 41,200');
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByRole('button', { name: 'Walk source codes' })).toBeInTheDocument();
    const calls = get.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 3); });
    expect(get.mock.calls.length).toBe(calls);
  });

  it('a queued walk says it waits for the worker', async () => {
    get.mockResolvedValue({ data: { ...running, active: { ...running.active!, outcome: 'queued', done: 0 } } });
    await renderPanel();
    expect(screen.getByRole('status')).toHaveTextContent('Source-code walk queued, waiting for the worker.');
  });

  it('without approval rights the button is disabled and says why', async () => {
    get.mockResolvedValue({ data: idle });
    await renderPanel(false);
    const button = screen.getByRole('button', { name: 'Walk source codes' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Approval rights are required to start a walk');
  });

  it('with no walk ever, says so and counts every patient', async () => {
    get.mockResolvedValue({ data: { active: null, last: null, patients_since: 3, walked_at: null } });
    await renderPanel();
    const panel = screen.getByRole('region', { name: 'Source-code walk' });
    expect(panel).toHaveTextContent('Source codes never walked. 3 patients loaded since.');
    expect(within(panel).queryByRole('table')).toBeNull();
  });

  it('a failed last walk is reported with its reason', async () => {
    get.mockResolvedValue({ data: { ...idle, last: { ...done, outcome: 'failed', failure_reason: 'Worker lost' } } });
    await renderPanel();
    expect(screen.getByRole('alert')).toHaveTextContent('The last walk failed: Worker lost. Starting another retries it.');
  });

  it('a failed start is reported and the button stays', async () => {
    get.mockResolvedValue({ data: idle });
    post.mockRejectedValue(new Error('503'));
    await renderPanel();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Walk source codes' })); });
    expect(screen.getByRole('alert')).toHaveTextContent('Could not start the walk.');
    expect(screen.getByRole('button', { name: 'Walk source codes' })).toBeEnabled();
  });
});
