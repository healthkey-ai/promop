import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AxiosInstance } from 'axios';

import { patientFormForClient } from './patientFormRuntime';
import TrialEditors from './TrialEditors';

function hostClient() {
  const patient = {
    person_id: 12,
    disease: 'Multiple Myeloma',
    therapy_lines_count: '1',
    lines_of_therapy: [
      { line: 1, episode_id: 11, regimen: 'VRd', start_date: '2024-01-01' },
    ],
    height: 170,
    weight: 60,
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
    weight: {
      writable: true,
      target: 'patient_record',
      control: 'number',
      label: 'Weight',
      unit: 'kg',
      bindings: ['weightAtTrial'],
    },
    genetic_mutations: { writable: true, target: 'genomics' },
  };
  const get = vi.fn(async (url: string) => {
    if (url.endsWith('/patient-info/me/')) {
      return { data: { patient_info: patient, write_enabled: true } };
    }
    if (url.includes('writable-fields')) return { data: fields };
    if (url.includes('genomics-catalog')) return { data: { markers: [], attributes: [] } };
    if (url.includes('genomics-legacy-cytogenetics')) {
      return { data: { results: [], next_cursor: null } };
    }
    if (url.includes('genomic')) return { data: [] };
    return { data: [] };
  });
  const patch = vi.fn(async (_url: string, values: Record<string, unknown>) => ({
    data: { patient_info: { person_id: 12, ...values }, write: { applied: Object.keys(values) } },
  }));
  return { get, patch, client: { get, patch } as unknown as AxiosInstance };
}

function renderAdapter(onChanged?: () => void) {
  const host = hostClient();
  render(
    <TrialEditors
      apiClient={host.client}
      apiBasePath=""
      readOnly={false}
      onChanged={onChanged}
    >
      {(slots) => (
        <div>
          <div data-testid="scalar">
            {slots.renderFieldEditor({ field: 'heightAtTrial', label: 'Height' })}
          </div>
          <div data-testid="scalar-two">
            {slots.renderFieldEditor({ field: 'weightAtTrial', label: 'Weight' })}
          </div>
          <div data-testid="therapy">
            {slots.renderFieldEditor({ field: 'priorTherapy', label: 'Prior Therapy' })}
          </div>
          <div data-testid="unknown">
            {slots.renderFieldEditor({ field: 'notInTheDescriptor', label: 'Unknown Row' })}
          </div>
          <span data-testid="owns-scalar">
            {String(slots.fieldEditorOwnsValue({ field: 'heightAtTrial', label: 'Height' }))}
          </span>
          <span data-testid="owns-therapy">
            {String(slots.fieldEditorOwnsValue({ field: 'priorTherapy', label: 'Prior Therapy' }))}
          </span>
        </div>
      )}
    </TrialEditors>,
  );
  return host;
}

function renderGenomics(onChanged?: () => void) {
  const host = hostClient();
  render(
    <TrialEditors apiClient={host.client} apiBasePath="" readOnly={false} onChanged={onChanged}>
      {(slots) => slots.renderGenomicsEditor({ label: 'Mutation Genes', onClose: () => {} })}
    </TrialEditors>,
  );
  return host;
}

describe('the shared trial editor adapter', () => {
  it('routes each row kind to its editor and leaves unresolved rows to EXACT', async () => {
    renderAdapter();

    // Scalars the descriptor resolves through a binding get the field editor.
    expect(await screen.findByRole('spinbutton', { name: 'Height' })).toHaveValue(170);
    // Therapy rows get one pencil that opens the line editor.
    expect(screen.getByRole('button', { name: 'Edit Prior Therapy' })).toBeInTheDocument();
    // A row PROMOP cannot resolve renders nothing, so EXACT keeps its editor.
    expect(screen.getByTestId('unknown')).toBeEmptyDOMElement();
    // The scalar slot owns the value; the therapy pencil does not.
    expect(screen.getByTestId('owns-scalar')).toHaveTextContent('true');
    expect(screen.getByTestId('owns-therapy')).toHaveTextContent('false');
    // A second scalar row is a second editor against the same store.
    expect(screen.getByRole('spinbutton', { name: 'Weight' })).toHaveValue(60);
  });

  it('opens the therapy-line editor in a non-modal dialog', async () => {
    renderAdapter();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Prior Therapy' }));

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(await screen.findByText(/Line 1:/)).toBeInTheDocument();
  });

  it('closes the therapy panel from a visible control', async () => {
    renderAdapter();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Prior Therapy' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Close therapy editor' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    // The scroll lock is released with it.
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('mounts the genomics dialog with the findings editor', async () => {
    // EXACT mounts this slot only when the reader opens the genomics row; an
    // always-open modal aria-hides the rest of the trial page.
    renderGenomics();
    // Wait for the loaded findings view, not the loading shell: Radix replaces
    // the dialog node when the record arrives.
    expect(await screen.findByText(/0 genomic finding records/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Genomic findings' })).toBeInTheDocument();
  });

  it('reports a confirmed field save to the host', async () => {
    const onChanged = vi.fn();
    const host = renderAdapter(onChanged);
    await screen.findByRole('spinbutton', { name: 'Height' });

    const store = patientFormForClient(host.client, '');
    await act(async () => { store.enqueue('height', 180); await store.flush(); });

    // Two scalar rows share the store; one acknowledged batch is one
    // notification, not one per row.
    expect(onChanged).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(host.patch).toHaveBeenCalled());
  });
});
