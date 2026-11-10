import { render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';

import { clinicalClient } from '@/api/clinicalTransport';
import GenomicsEditor from './GenomicsEditor';
import PatientFieldEditorSlot from './PatientFieldEditorSlot';
import TherapyLinesEditor from './TherapyLinesEditor';
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
  return { get, patch, client: { get, patch } as unknown as AxiosInstance };
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

  it('makes the host client the clinical transport while mounted and restores it after', async () => {
    const host = hostClient();
    const before = clinicalClient();
    const view = render(
      <TherapyLinesEditor apiClient={host.client} apiBasePath="" readOnly={false} />,
    );
    await screen.findByText(/Line 1:/);
    expect(clinicalClient()).toBe(host.client);

    view.unmount();
    // The previous owner is back, so a widget mounted elsewhere is not left
    // writing through a client this editor took away.
    expect(clinicalClient()).toBe(before);
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
    expect(host.get).toHaveBeenCalledWith(
      '/fed/v1/patient-records/writable-fields/',
      expect.objectContaining({ headers: { 'X-Patient-Form': '1' } }),
    );
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
