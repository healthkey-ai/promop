import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PreexistingConditionsAnswer from './PreexistingConditionsAnswer';
import { PatientFormContext } from '@/federation/PatientFormContext';
import type { PatientFormStore } from '@/federation/patientFormStore';

const post = vi.fn();
const get = vi.fn();
vi.mock('@/api/clinicalTransport', () => ({
  clinicalClient: () => ({ post, get }),
  clinicalUrl: (path: string) => `/host${path}`,
}));
vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ loading: false, options: [{ value: 'infections', label: 'Infections' }] }),
}));

beforeEach(() => { post.mockReset(); get.mockReset(); });

function editor(flag: boolean | null = true, categories: string[] = [], readOnly = false) {
  const saved = vi.fn();
  render(<PreexistingConditionsAnswer personId={42} flag={flag} categories={categories}
    readOnly={readOnly} onSaved={saved} />);
  return saved;
}

describe('explicit pre-existing-conditions answers', () => {
  it('loads the dedicated snapshot when the host withholds the raw flag from its projection', async () => {
    get.mockResolvedValue({ data: { no_pre_existing_conditions: true, preexisting_conditions: [] } });
    post.mockResolvedValue({ data: { no_pre_existing_conditions: true, preexisting_conditions: [] } });
    render(<PreexistingConditionsAnswer personId={42} flag={undefined} categories={[]}
      readOnly={false} onSaved={vi.fn()} />);
    expect(post).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][1].expected_flag).toBe(true);
    expect(get).toHaveBeenCalledWith('/host/v1/patient-records/42/preexisting-conditions-answer/');
  });

  it('does not offer confirmation when the dedicated snapshot cannot be loaded', async () => {
    get.mockRejectedValue(new Error('unsupported'));
    render(<PreexistingConditionsAnswer personId={42} flag={undefined} categories={[]}
      readOnly={false} onSaved={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load');
    expect(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });
  it('does not confirm on mount or while selecting categories', () => {
    editor();
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText('Infections'));
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' })).toBeDisabled();
  });

  it('reconfirms unchanged legacy True only on a deliberate button press', async () => {
    const answer = { no_pre_existing_conditions: true, preexisting_conditions: [] };
    post.mockResolvedValue({ data: answer });
    const saved = editor();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(answer));
    expect(post).toHaveBeenCalledWith('/host/v1/patient-records/42/preexisting-conditions-answer/', {
      action: 'confirm_none', expected_flag: true, expected_categories: [],
    });
  });

  it('shows unknown without inventing a negative answer', () => {
    editor(null);
    expect(screen.getByText(/Not answered/)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it('saves category codes separately from autosave and withdraws none', async () => {
    post.mockResolvedValue({ data: { no_pre_existing_conditions: null, preexisting_conditions: ['infections'] } });
    editor();
    fireEvent.click(screen.getByLabelText('Infections'));
    fireEvent.click(screen.getByRole('button', { name: /Save categories/ }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][1]).toEqual({
      action: 'set_categories', categories: ['infections'], expected_flag: true, expected_categories: [],
    });
  });

  it('withdraws without deleting recorded categories', async () => {
    post.mockResolvedValue({ data: { no_pre_existing_conditions: null, preexisting_conditions: ['infections'] } });
    editor(null, ['infections']);
    expect(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Withdraw answer' }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][1].action).toBe('withdraw');
    expect(post.mock.calls[0][1]).not.toHaveProperty('categories');
  });

  it('reports conflict without claiming confirmation succeeded', async () => {
    post.mockRejectedValue({ response: { status: 409 } });
    const saved = editor();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Reload');
    expect(saved).not.toHaveBeenCalled();
  });

  it('disables actions for read-only callers', () => {
    editor(true, [], true);
    expect(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' })).toBeDisabled();
    expect(screen.getByLabelText('Infections')).toBeDisabled();
  });

  it.each([
    ['success', () => post.mockResolvedValue({ data: { no_pre_existing_conditions: true, preexisting_conditions: [] } })],
    ['failure', () => post.mockRejectedValue({ response: { status: 500 } })],
  ])('releases the bypass-write hold after a %s', async (_, arrange) => {
    arrange();
    const holds = { count: 0 };
    const store = {
      beginBypassWrite: vi.fn(() => { holds.count += 1; }),
      endBypassWrite: vi.fn(() => { holds.count -= 1; }),
      failBypassWrite: vi.fn(),
    } as unknown as PatientFormStore;
    render(<PatientFormContext.Provider value={store}>
      <PreexistingConditionsAnswer personId={42} flag={true} categories={[]}
        readOnly={false} onSaved={vi.fn()} />
    </PatientFormContext.Provider>);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' }));
    await waitFor(() => expect(store.endBypassWrite).toHaveBeenCalledTimes(1));
    expect(store.beginBypassWrite).toHaveBeenCalledTimes(1);
    expect(holds.count).toBe(0);
  });
});

describe('after a save the host re-renders with the new answer', () => {
  it('keeps the confirmation and re-seeds the selection from the new props', async () => {
    const answer = { no_pre_existing_conditions: null, preexisting_conditions: ['infections'] };
    post.mockResolvedValue({ data: answer });
    const props = { personId: 42, readOnly: false };
    // The host applies the saved answer inside onSaved, while the save is busy.
    const onSaved = vi.fn(() => {
      view.rerender(<PreexistingConditionsAnswer {...props} flag={null} categories={['infections']}
        onSaved={onSaved} />);
    });
    const view = render(<PreexistingConditionsAnswer {...props} flag={true} categories={[]}
      onSaved={onSaved} />);
    fireEvent.click(screen.getByLabelText('Infections'));
    fireEvent.click(screen.getByRole('button', { name: /Save categories/ }));
    expect(await screen.findByText('Answer saved.')).toBeInTheDocument();
    expect(screen.getByLabelText('Infections')).toBeChecked();

    view.rerender(<PreexistingConditionsAnswer {...props} flag={null} categories={[]}
      onSaved={onSaved} />);
    expect(screen.getByLabelText('Infections')).not.toBeChecked();
    expect(screen.queryByText('Answer saved.')).not.toBeInTheDocument();
  });

  it('drops a stale conflict alert once the host delivers the changed answer', async () => {
    post.mockRejectedValue({ response: { status: 409 } });
    const props = { personId: 42, readOnly: false, onSaved: vi.fn() };
    const view = render(<PreexistingConditionsAnswer {...props} flag={true} categories={[]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm no pre-existing conditions' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Reload');

    view.rerender(<PreexistingConditionsAnswer {...props} flag={null} categories={['infections']} />);
    await waitFor(() => expect(screen.queryByText(/This answer changed/)).not.toBeInTheDocument());
  });
});
