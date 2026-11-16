import type { ComponentProps } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ExplicitAnswerActions from './ExplicitAnswerActions';
import { PatientFormStore } from '@/federation/patientFormStore';

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


describe('explicit answers travel on the editor connection (#2606 review)', () => {
  it('uses the store client, not the module-level transport', async () => {
    const client = {
      get: vi.fn(async () => ({ data: { value: false, confirmed: false, source: [] } })),
      post: vi.fn(async () => ({ data: { value: true, confirmed: true, source: [] } })),
    };
    const store = new PatientFormStore(client as never, '/host');
    render(<ExplicitAnswerActions personId={42} name="no_other_active_malignancies"
      endpoint="negative-answer" actions={NEGATIVE.actions} actionLabels={NEGATIVE.actionLabels}
      value={false} store={store} onSaved={vi.fn()} />);

    const confirm = await screen.findByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);

    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/host/v1/patient-records/42/negative-answer/',
      expect.objectContaining({ field: 'no_other_active_malignancies', action: 'confirm_negative' }),
    ));
    expect(client.get).toHaveBeenCalledWith(
      '/host/v1/patient-records/42/negative-answer/', { params: { field: 'no_other_active_malignancies' } },
    );
    // The module-level transport is only for a surface with no store.
    expect(get).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it('re-reads the attestation when another mounted editor writes', async () => {
    let confirmed = false;
    const client = {
      get: vi.fn(async () => ({ data: { value: confirmed, confirmed, source: [] } })),
      post: vi.fn(async () => {
        confirmed = true;
        return { data: { value: true, confirmed: true, source: [] } };
      }),
    };
    const store = new PatientFormStore(client as never, '');
    const saved = vi.fn(async (values: Record<string, unknown>) => store.applyServerValues(values));
    const props = { personId: 42, name: 'no_other_active_malignancies', endpoint: 'negative-answer',
      actions: NEGATIVE.actions, actionLabels: NEGATIVE.actionLabels, value: false, store, onSaved: saved };
    render(<><ExplicitAnswerActions {...props} /><ExplicitAnswerActions {...props} /></>);

    await waitFor(() => expect(client.get).toHaveBeenCalledTimes(2));
    expect(screen.queryByText('Explicitly confirmed.')).toBeNull();
    const [first] = screen.getAllByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(first).toBeEnabled());
    fireEvent.click(first);

    // The other editor must stop saying "not confirmed", not just re-request.
    await waitFor(() => expect(screen.getAllByText('Explicitly confirmed.')).toHaveLength(2));
  });
});


describe('explicit answer failures reach the host (#2606 review)', () => {
  function storeFor(getImpl: () => Promise<unknown>, postImpl: () => Promise<unknown>) {
    const client = { get: vi.fn(getImpl), post: vi.fn(postImpl) };
    const store = new PatientFormStore(client as never, '');
    return { store, client };
  }

  it('shows the server sentence and marks the bypass failed on a conflict', async () => {
    const { store, client } = storeFor(
      async () => ({ data: { value: false, confirmed: false, source: [] } }),
      async () => { throw { response: { status: 409, data: { detail: 'The answer or its source changed. Reload before confirming.' } } }; },
    );
    render(<ExplicitAnswerActions personId={42} name="no_other_active_malignancies"
      endpoint="negative-answer" actions={NEGATIVE.actions} actionLabels={NEGATIVE.actionLabels}
      value={false} store={store} onSaved={vi.fn()} />);

    const confirm = await screen.findByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);

    expect(await screen.findByRole('alert')).toHaveTextContent('Reload before confirming');
    expect(store.getSnapshot().bypassError).toBe('Could not save the explicit answer.');
    expect(client.post).toHaveBeenCalledTimes(1);
  });

  it('balances the bypass hold on success', async () => {
    const { store } = storeFor(
      async () => ({ data: { value: false, confirmed: false, source: [] } }),
      async () => ({ data: { value: true, confirmed: true, source: [] } }),
    );
    const begin = vi.spyOn(store, 'beginBypassWrite');
    const end = vi.spyOn(store, 'endBypassWrite');
    render(<ExplicitAnswerActions personId={42} name="no_other_active_malignancies"
      endpoint="negative-answer" actions={NEGATIVE.actions} actionLabels={NEGATIVE.actionLabels}
      value={false} store={store} onSaved={vi.fn()} />);
    const confirm = await screen.findByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    await waitFor(() => expect(end).toHaveBeenCalledTimes(1));
    expect(begin).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().bypassing).toBe(false);
  });

  it('balances the bypass hold on a failure', async () => {
    const { store } = storeFor(
      async () => ({ data: { value: false, confirmed: false, source: [] } }),
      async () => { throw { response: { status: 500 } }; },
    );
    const begin = vi.spyOn(store, 'beginBypassWrite');
    const end = vi.spyOn(store, 'endBypassWrite');
    render(<ExplicitAnswerActions personId={42} name="no_other_active_malignancies"
      endpoint="negative-answer" actions={NEGATIVE.actions} actionLabels={NEGATIVE.actionLabels}
      value={false} store={store} onSaved={vi.fn()} />);
    const confirm = await screen.findByRole('button', { name: 'Confirm no other active malignancies' });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    await waitFor(() => expect(end).toHaveBeenCalledTimes(1));
    expect(begin).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().bypassing).toBe(false);
  });
});
