import type { ComponentProps } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExplicitAnswerActions from './ExplicitAnswerActions';

const get = vi.fn();
const post = vi.fn();
vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => ({ get, post }),
  clinicalUrl: (path: string) => `/host${path}`,
}));

const NEGATIVE = {
  actions: ['confirm_negative', 'withdraw'],
  actionLabels: {
    confirm_negative: 'Confirm no other active malignancies',
    withdraw: 'Withdraw confirmation',
  },
};

beforeEach(() => { get.mockReset(); post.mockReset(); });

function renderActions(overrides: Partial<ComponentProps<typeof ExplicitAnswerActions>> = {}) {
  const saved = vi.fn();
  render(<ExplicitAnswerActions personId={42} name="no_other_active_malignancies"
    endpoint="negative-answer" actions={NEGATIVE.actions} actionLabels={NEGATIVE.actionLabels}
    value={false} onSaved={saved} {...overrides} />);
  return saved;
}

describe('explicit negative answers', () => {
  it('renders the labels the descriptor supplies and posts the snapshot', async () => {
    get.mockResolvedValue({ data: {
      field: 'no_other_active_malignancies', value: false, confirmed: false, source: [],
    } });
    post.mockResolvedValue({ data: {
      field: 'no_other_active_malignancies', value: true, confirmed: true, source: [],
    } });
    const saved = renderActions();

    const confirm = screen.getByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    expect(screen.getByText(/derived from the recorded facts/)).toBeVisible();
    fireEvent.click(confirm);

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][0]).toBe('/host/v1/patient-records/42/negative-answer/');
    expect(post.mock.calls[0][1]).toEqual({
      field: 'no_other_active_malignancies',
      action: 'confirm_negative',
      expected_value: false,
      expected_source: [],
    });
    await waitFor(() => expect(saved).toHaveBeenCalledWith({ no_other_active_malignancies: true }));
    expect(await screen.findByText('Answer confirmed.')).toBeVisible();
    expect(await screen.findByText('Explicitly confirmed.')).toBeVisible();
  });

  it('offers withdrawal only once an attestation exists', async () => {
    get.mockResolvedValue({ data: {
      field: 'no_other_active_malignancies', value: false, confirmed: false, source: [],
    } });
    renderActions();

    await screen.findByText(/derived from the recorded facts/);
    expect(screen.queryByRole('button', { name: 'Withdraw confirmation' })).toBeNull();
  });

  it('withdraws a confirmed answer and returns the derived value', async () => {
    get.mockResolvedValue({ data: {
      field: 'no_other_active_malignancies', value: true, confirmed: true, source: [],
    } });
    post.mockResolvedValue({ data: {
      field: 'no_other_active_malignancies', value: false, confirmed: false, source: [],
    } });
    const saved = renderActions();

    const withdraw = await screen.findByRole('button', { name: 'Withdraw confirmation' });
    await waitFor(() => expect(withdraw).toBeEnabled());
    expect(screen.getByText('Explicitly confirmed.')).toBeVisible();
    fireEvent.click(withdraw);

    await waitFor(() => expect(saved).toHaveBeenCalledWith({ no_other_active_malignancies: false }));
    expect(await screen.findByText('Confirmation withdrawn.')).toBeVisible();
    expect(await screen.findByText(/derived from the recorded facts/)).toBeVisible();
  });

  it('offers no action it has no reviewed label for', async () => {
    get.mockResolvedValue({ data: { value: false, confirmed: false, source: [] } });
    renderActions({ actionLabels: { confirm_negative: 'Confirm no other active malignancies' } });

    await screen.findByText(/derived from the recorded facts/);
    expect(screen.getByRole('button', { name: 'Confirm no other active malignancies' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Withdraw confirmation' })).toBeNull();
  });

  it('shows the server sentence when the fact contradicts the confirmation', async () => {
    get.mockResolvedValue({ data: { value: false, confirmed: false, source: 'a,b' } });
    post.mockRejectedValue({ response: { status: 400, data: {
      detail: 'More than one active malignancy is recorded; correct that list before confirming none.',
    } } });
    renderActions();

    const confirm = screen.getByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'More than one active malignancy is recorded; correct that list before confirming none.',
    );
  });

  it('does not confirm against an unloaded snapshot', async () => {
    get.mockRejectedValue(new Error('offline'));
    renderActions();

    expect(await screen.findByText(/Could not load whether this answer is confirmed/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Confirm no other active malignancies' })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it('keeps the answer visible but disabled in a read-only form', async () => {
    get.mockResolvedValue({ data: { value: true, confirmed: true, source: [] } });
    renderActions({ readOnly: true });

    const withdraw = await screen.findByRole('button', { name: 'Withdraw confirmation' });
    await waitFor(() => expect(withdraw).toBeDisabled());
    expect(screen.getByText('Explicitly confirmed.')).toBeVisible();
  });
});
