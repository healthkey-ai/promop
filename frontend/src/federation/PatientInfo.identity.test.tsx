/**
 * A host that keeps one axios instance across a sign-in or impersonation
 * change. The store for the old patient never adopts the new one (its queue
 * and values belong to the old patient); the form gets a fresh store instead.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { AxiosInstance } from 'axios';
import { QueryClient } from '@tanstack/react-query';
import PatientInfo from './PatientInfo';
import PatientFieldEditorSlot from './PatientFieldEditorSlot';
import { patientFormForClient } from './patientFormRuntime';
import { __resetWritableFieldsCache } from '@/hooks/useWritableFields';
import { resetClinicalTransport } from '@/api/clinicalTransport';

vi.mock('@/hooks/useVocabulary', () => ({
  useVocabulary: () => ({ options: [], source: null, loading: false }),
}));

vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ grouped: new Map(), isLoading: false })),
}));

const DESCRIPTORS = {
  email: { kind: 'direct', writable: true, target: 'patient_record', control: 'email' },
};

const PATIENTS: Record<number, { email: string }> = {
  261: { email: 'first@example.org' },
  262: { email: 'second@example.org' },
};

let client: AxiosInstance;
/** Who the host's client currently speaks for. */
let signedIn = 261;
let patches: Array<[string, unknown]>;

beforeEach(() => {
  vi.clearAllMocks();
  __resetWritableFieldsCache();
  resetClinicalTransport();
  signedIn = 261;
  patches = [];
  client = {
    get: vi.fn((url: string) => {
      if (url.includes('writable-fields')) return Promise.resolve({ data: DESCRIPTORS });
      if (url.includes('/patient-info/me/')) {
        return Promise.resolve({ data: {
          patient_info: { person_id: signedIn, ...PATIENTS[signedIn],
            no_pre_existing_conditions: true, preexisting_conditions: [] },
          patient_name: `Patient ${signedIn}`,
        } });
      }
      return Promise.resolve({ data: [] });
    }),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    patch: vi.fn((url: string, body: unknown) => {
      patches.push([url, body]);
      return new Promise(() => {});
    }),
  } as unknown as AxiosInstance;
});

afterEach(() => {
  resetClinicalTransport();
  window.history.replaceState({}, '', '/');
});

function form(connectionKey?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <PatientInfo apiClient={client} apiBasePath="/api" queryClient={qc} connectionKey={connectionKey} />;
}

describe('patient identity with a reused client', () => {
  it('starts a fresh store from Retry when the client now speaks for another patient', async () => {
    const first = render(form());
    expect(await screen.findByDisplayValue('first@example.org')).toBeInTheDocument();
    const original = patientFormForClient(client, '/api');
    first.unmount();

    signedIn = 262;
    render(form());
    const retry = await screen.findByRole('button', { name: 'Retry' });
    expect(screen.getByRole('alert')).toHaveTextContent('The linked patient changed');
    fireEvent.click(retry);

    expect(await screen.findByDisplayValue('second@example.org')).toBeInTheDocument();
    // The old store keeps its own patient; it was not retargeted.
    expect(original.getSnapshot().personId).toBe(261);
    expect(original.getSnapshot().values.email).toBe('first@example.org');
    expect(patientFormForClient(client, '/api')).not.toBe(original);
    expect(patientFormForClient(client, '/api').getSnapshot().personId).toBe(262);
  });

  it('loads a fresh store when the host passes a new connection key', async () => {
    const view = render(form('session-a'));
    expect(await screen.findByDisplayValue('first@example.org')).toBeInTheDocument();

    signedIn = 262;
    view.rerender(form('session-b'));

    expect(await screen.findByDisplayValue('second@example.org')).toBeInTheDocument();
    expect(screen.queryByText(/linked patient changed/)).not.toBeInTheDocument();
  });

  it('never sends the old patient\'s draft through the fresh store', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form('session-a'));
      fireEvent.change(await screen.findByDisplayValue('first@example.org'),
        { target: { value: 'typed-for-first@example.org' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
      expect(patches).toEqual([['/api/v1/patient-records/261/', { email: 'typed-for-first@example.org' }]]);

      signedIn = 262;
      view.rerender(form('session-b'));
      expect(await screen.findByDisplayValue('second@example.org')).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

      const fresh = patientFormForClient(client, '/api', 'session-b').getSnapshot();
      expect(fresh.pending).toEqual({});
      expect(patches.every(([url]) => !url.includes('/262/'))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  async function typeFirst() {
    fireEvent.change(await screen.findByDisplayValue('first@example.org'),
      { target: { value: 'typed-for-first@example.org' } });
  }

  it('does not send an unsent edit through the client after a connection-key switch', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form('session-a'));
      await typeFirst();
      const old = patientFormForClient(client, '/api', 'session-a');

      // The host re-authenticates the same client and switches keys before
      // the debounce fires.
      signedIn = 262;
      view.rerender(form('session-b'));
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

      expect(patches).toEqual([]);
      expect(old.getSnapshot().failed).toEqual({ email: 'typed-for-first@example.org' });
      expect(old.getSnapshot().errors.email).toMatch(/patient changed/);
    } finally {
      vi.useRealTimers();
    }
  });

  it('tells the reader that the previous patient\'s unsent edit was dropped', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form('session-a'));
      await typeFirst();

      signedIn = 262;
      view.rerender(form('session-b'));

      expect(await screen.findByRole('status')).toHaveTextContent(
        '1 unsent edit for the previous patient was not saved.');
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByText(/unsent edit/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not send it either when the form remounts under a new key', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form('session-a'));
      await typeFirst();
      view.unmount();
      signedIn = 262;
      render(form('session-b'));
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

      expect(patches).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('still drains a queued edit after a plain unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form('session-a'));
      await typeFirst();
      view.unmount();
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });

      expect(patches).toEqual([['/api/v1/patient-records/261/', { email: 'typed-for-first@example.org' }]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('gives a writable store back when the host returns to the omitted key', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const view = render(form());
      expect(await screen.findByDisplayValue('first@example.org')).toBeInTheDocument();
      // Impersonate on the same client, then end the impersonation.
      signedIn = 262;
      view.rerender(form('impersonating-262'));
      expect(await screen.findByDisplayValue('second@example.org')).toBeInTheDocument();
      signedIn = 261;
      view.rerender(form());
      fireEvent.change(await screen.findByDisplayValue('first@example.org'),
        { target: { value: 'back@example.org' } });
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });

      expect(patientFormForClient(client, '/api').isRetired).toBe(false);
      expect(patches).toEqual([['/api/v1/patient-records/261/', { email: 'back@example.org' }]]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('renews a trial-row slot from its Retry when the patient changed', async () => {
    render(<PatientFieldEditorSlot apiClient={client} apiBasePath="/api" name="email" label="Email" />);
    expect(await screen.findByDisplayValue('first@example.org')).toBeInTheDocument();
    const original = patientFormForClient(client, '/api');

    signedIn = 262;
    await act(async () => { await original.load(); });
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

    expect(await screen.findByDisplayValue('second@example.org')).toBeInTheDocument();
    expect(original.getSnapshot().personId).toBe(261);
  });
});
