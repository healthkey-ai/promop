import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';

import { clinicalClient } from '@/api/clinicalTransport';
import GenomicsEditor from './GenomicsEditor';
import PatientFieldEditorSlot from './PatientFieldEditorSlot';
import TherapyLinesEditor from './TherapyLinesEditor';
import { editorIdentity } from './editorSession';
import { patientFormForClient } from './patientFormRuntime';
import { usePromopFieldBindings } from './usePromopFieldBindings';

interface Fixtures {
  patient?: Record<string, unknown>;
  fields?: Record<string, unknown>;
  onGet?: (url: string) => unknown;
}

/**
 * A host client: one authenticated axios-like object. The point of these
 * editors is that PROMOP never mints another one, so the tests assert against
 * the instance the host handed over.
 */
function hostClient(fixtures: Fixtures = {}) {
  const patient = {
    person_id: 12,
    disease: 'Multiple Myeloma',
    therapy_lines_count: '1',
    lines_of_therapy: [
      { line: 1, episode_id: 11, regimen: 'VRd', start_date: '2024-01-01' },
    ],
    height: 170,
    ...fixtures.patient,
  };
  const fields = {
    height: {
      writable: true,
      target: 'patient_record',
      control: 'number',
      label: 'Height',
      unit: 'cm',
      bindings: ['heightAtTrial'],
    },
    genetic_mutations: { writable: true, target: 'patient_record' },
    ...fixtures.fields,
  };
  const get = vi.fn(async (url: string, config?: { params?: Record<string, unknown> }) => {
    const custom = fixtures.onGet?.(url);
    if (custom !== undefined) return { data: custom };
    if (url.endsWith('/patient-info/me/')) {
      return { data: { patient_info: patient, write_enabled: true } };
    }
    if (url.includes('writable-fields')) {
      return { data: fields };
    }
    // Genomics: an empty catalog and no recorded findings is a complete answer.
    if (url.includes('genomics-catalog')) return { data: { markers: [], attributes: [] } };
    if (url.includes('genomics-legacy-cytogenetics')) {
      return { data: { results: [], next_cursor: null } };
    }
    if (url.includes('genomic')) return { data: [] };
    void config;
    return { data: [] };
  });
  const patch = vi.fn(async (_url: string, values: Record<string, unknown>) => ({
    data: { patient_info: { person_id: 12, ...values }, write: { applied: Object.keys(values) } },
  }));
  const post = vi.fn(async (_url: string, body: unknown) => ({
    data: { therapy_history_complete: true, patient_info: {}, ...(body as object) },
  }));
  return { get, patch, post, client: { get, patch, post } as unknown as AxiosInstance };
}

describe('the PROMOP editors for a host that shares the React tree', () => {
  it('reads the therapy lines through the host client at its own base path', async () => {
    const host = hostClient();
    render(
      <TherapyLinesEditor apiClient={host.client} apiBasePath="/fed/" readOnly={false} />,
    );

    expect(await screen.findByText(/Line 1:/)).toBeInTheDocument();
    expect(host.get).toHaveBeenCalledWith('/fed/patient-info/me/');
    expect(host.get).toHaveBeenCalledWith(
      '/fed/v1/patient-records/writable-fields/',
      expect.objectContaining({ headers: { 'X-Patient-Form': '1' } }),
    );
  });

  it('never touches the global clinical transport and writes through the host client', async () => {
    const host = hostClient();
    // Another widget on the page owns the global transport. This editor must
    // neither steal it nor write through it.
    const ownerBefore = clinicalClient();
    render(
      <TherapyLinesEditor apiClient={host.client} apiBasePath="/fed" readOnly={false} />,
    );
    await screen.findByText(/Line 1:/);
    expect(clinicalClient()).toBe(ownerBefore);

    // The completeness flag is one of the writes that used to go through the
    // global transport. It must land on the host's client at the host's base.
    await act(async () => {
      screen.getByRole('checkbox', { name: 'Treatment history is complete' }).click();
    });
    await waitFor(() => expect(host.post).toHaveBeenCalledWith(
      '/fed/v1/therapy-history/completeness/',
      { person: 12, complete: true },
    ));
    expect(clinicalClient()).toBe(ownerBefore);
  });

  it('draws the genomic findings dialog and reports its close', async () => {
    const host = hostClient();
    const onClose = vi.fn();
    render(
      <GenomicsEditor
        apiClient={host.client}
        apiBasePath=""
        readOnly={false}
        onClose={onClose}
        onChanged={() => {}}
      />,
    );

    // Wait for the loaded view, not the loading shell: Radix replaces the
    // dialog node when the record arrives, so a node found mid-load is gone.
    expect(await screen.findByText(/0 genomic finding records/)).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Genomic findings' })).toBeInTheDocument();
    expect(host.get).toHaveBeenCalledWith('/patient-info/me/');
  });

  it('resolves a trial-row binding through the descriptor and shows the value', async () => {
    const host = hostClient();
    render(
      <PatientFieldEditorSlot
        apiClient={host.client}
        apiBasePath=""
        name="heightAtTrial"
        label="Height"
        readOnly={false}
      />,
    );

    // `heightAtTrial` is not a descriptor key: it resolves through `bindings`.
    expect(await screen.findByRole('spinbutton', { name: 'Height' })).toHaveValue(170);
    expect(screen.queryByText(/No reviewed PROMOP field binding/)).toBeNull();
  });

  it('tells the host which trial rows it can resolve, including bindings', async () => {
    const host = hostClient({ fields: { meets_slim: { bindings: ['meetsSLIM'] } } });
    const { result } = renderHook(() =>
      usePromopFieldBindings({ apiClient: host.client, apiBasePath: '/fed/' }),
    );

    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.canResolve('height')).toBe(true);
    expect(result.current.canResolve('heightAtTrial')).toBe(true);
    expect(result.current.canResolve('meetsSLIM')).toBe(true);
    expect(result.current.canResolve('therapy_types')).toBe(false);
    expect(result.current.entryFor('heightAtTrial')?.control).toBe('number');
    expect(host.get).toHaveBeenCalledWith(
      '/fed/v1/patient-records/writable-fields/',
      expect.objectContaining({ headers: { 'X-Patient-Form': '1' } }),
    );
  });

  it('prefers a canonical key over a later binding and keeps the first binding', async () => {
    const fields = {
      // `noHiv` is a real field; `other` also claims it as a binding. The
      // store resolves the canonical first, so the adapter must too.
      noHiv: { kind: 'negated' },
      other: { kind: 'direct', label: 'Other', bindings: ['noHiv', 'shared'] },
      third: { kind: 'direct', label: 'Third', bindings: ['shared'] },
    };
    const host = hostClient({ fields });
    const { result } = renderHook(() =>
      usePromopFieldBindings({ apiClient: host.client, apiBasePath: '' }),
    );

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.entryFor('noHiv')?.kind).toBe('negated');
    // Two entries declare `shared`; the first one wins, as in the store.
    expect(result.current.entryFor('shared')?.label).toBe('Other');
  });

  it('falls back to the host editor when the descriptor cannot be read', async () => {
    const client = {
      get: vi.fn(async () => {
        throw new Error('offline');
      }),
    } as unknown as AxiosInstance;
    const { result } = renderHook(() =>
      usePromopFieldBindings({ apiClient: client, apiBasePath: '' }),
    );

    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.canResolve('height')).toBe(false);
  });

  it('gives a new connectionKey a new store and descriptor answer', async () => {
    const host = hostClient();
    // One axios instance whose interceptor now signs as somebody else: the
    // key, not the client reference, is what starts a new queue.
    expect(patientFormForClient(host.client, '/api', 'session-a'))
      .not.toBe(patientFormForClient(host.client, '/api', 'session-b'));
    expect(patientFormForClient(host.client, '/api', 'session-a'))
      .toBe(patientFormForClient(host.client, '/api', 'session-a'));

    const { result, rerender } = renderHook(
      ({ connectionKey }: { connectionKey: string }) =>
        usePromopFieldBindings({ apiClient: host.client, apiBasePath: '', connectionKey }),
      { initialProps: { connectionKey: 'session-a' } },
    );
    await waitFor(() => expect(result.current.ready).toBe(true));
    rerender({ connectionKey: 'session-b' });
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(result.current.ready).toBe(true));
  });

  it('remounts the editor subtree when the connection changes', async () => {
    const host = hostClient();
    const { rerender } = render(
      <TherapyLinesEditor apiClient={host.client} apiBasePath="" readOnly={false} connectionKey="a" />,
    );
    await screen.findByText(/Line 1:/);
    fireEvent.click(screen.getByRole('button', { name: 'Add therapy line' }));
    expect(screen.getByRole('dialog', { name: 'Record a line of therapy' })).toBeInTheDocument();

    // Same patient form, new identity: an open dialog still holds the old
    // patient's episode, so it must not survive.
    rerender(
      <TherapyLinesEditor apiClient={host.client} apiBasePath="" readOnly={false} connectionKey="b" />,
    );
    // Wait for the NEW store to finish loading: while it loads the panel
    // renders its status line, which would hide a surviving dialog too.
    await screen.findByText(/Line 1:/);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('notifies the host after an acknowledged field save', async () => {
    const host = hostClient();
    const onChanged = vi.fn();
    render(
      <PatientFieldEditorSlot
        apiClient={host.client}
        apiBasePath=""
        name="height"
        label="Height"
        readOnly={false}
        onChanged={onChanged}
      />,
    );
    await screen.findByRole('spinbutton', { name: 'Height' });

    // The slot writes through the shared store; drive that store the way the
    // editor does and let it acknowledge the batch.
    const store = patientFormForClient(host.client, '');
    await act(async () => { store.enqueue('height', 180); await store.flush(); });

    expect(host.patch).toHaveBeenCalledWith(
      '/v1/patient-records/12/',
      { height: 180 },
      expect.objectContaining({ headers: { 'X-Patient-Form': '1' } }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it('does not multiply notifications when the host passes a new callback', async () => {
    const host = hostClient();
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(
      <PatientFieldEditorSlot
        apiClient={host.client}
        apiBasePath=""
        name="height"
        label="Height"
        readOnly={false}
        onChanged={first}
      />,
    );
    await screen.findByRole('spinbutton', { name: 'Height' });
    const store = patientFormForClient(host.client, '');

    // Queue a batch, then hand over a fresh inline callback while it is still
    // pending: the store keeps the old listener until the queue drains, so a
    // subscription keyed on the callback would deliver one invalidation twice.
    await act(async () => { store.enqueue('height', 180); });
    rerender(
      <PatientFieldEditorSlot
        apiClient={host.client}
        apiBasePath=""
        name="height"
        label="Height"
        readOnly={false}
        onChanged={second}
      />,
    );
    await act(async () => { await store.flush(); });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('keeps identity keys distinct for colliding base and key text', () => {
    const host = hostClient();
    // ":" used to join the parts, so these two different stores produced one
    // React key and kept the old patient's drafts in place.
    expect(editorIdentity({ apiClient: host.client, apiBasePath: '/api', connectionKey: 'tenant:x' }))
      .not.toBe(editorIdentity({ apiClient: host.client, apiBasePath: '/api:tenant', connectionKey: 'x' }));
  });

  it('notifies once under StrictMode when a batch was already pending', async () => {
    const host = hostClient();
    const onChanged = vi.fn();
    // Queue a batch BEFORE the slot mounts, so StrictMode's setup → cleanup →
    // setup finds listeners it cannot remove yet. The store refuses an edit
    // until it has loaded (writable is only known from the server).
    const store = patientFormForClient(host.client, '');
    await act(async () => { await store.load(); store.enqueue('height', 180); });

    render(
      <StrictMode>
        <PatientFieldEditorSlot
          apiClient={host.client}
          apiBasePath=""
          name="height"
          label="Height"
          readOnly={false}
          onChanged={onChanged}
        />
      </StrictMode>,
    );
    await screen.findByRole('spinbutton', { name: 'Height' });
    await act(async () => { await store.flush(); });

    expect(onChanged).toHaveBeenCalledTimes(1);
    // The deferred cleanup from the discarded setup must not retire the live
    // listener once the queue drains: the next save still reaches the host.
    await act(async () => { store.enqueue('height', 181); await store.flush(); });
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it('stays quiet when the server refuses the field write', async () => {
    const host = hostClient();
    const onChanged = vi.fn();
    render(
      <PatientFieldEditorSlot
        apiClient={host.client}
        apiBasePath=""
        name="height"
        label="Height"
        readOnly={false}
        onChanged={onChanged}
      />,
    );
    await screen.findByRole('spinbutton', { name: 'Height' });
    vi.mocked(host.patch).mockRejectedValueOnce(new Error('server refused'));

    const store = patientFormForClient(host.client, '');
    await act(async () => { store.enqueue('height', 181); await store.flush(); });

    // The row still shows the last saved value; the host must not treat the
    // failed batch as a reason to invalidate and redraw it.
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('does not answer a new identity from the previous descriptor', async () => {
    const first = hostClient({ fields: { from_first: {} } });
    const second = hostClient({ fields: { from_second: {} } });
    const { result, rerender } = renderHook(
      ({ apiClient }: { apiClient: AxiosInstance }) =>
        usePromopFieldBindings({ apiClient, apiBasePath: '' }),
      { initialProps: { apiClient: first.client } },
    );

    await waitFor(() => expect(result.current.canResolve('from_first')).toBe(true));
    rerender({ apiClient: second.client });
    expect(result.current.ready).toBe(false);
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.canResolve('from_first')).toBe(false);
    expect(result.current.canResolve('from_second')).toBe(true);
  });
});
