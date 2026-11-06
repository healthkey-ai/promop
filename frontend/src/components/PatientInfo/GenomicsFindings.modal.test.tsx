import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GenomicsFindings from './GenomicsFindings';

const mocks = vi.hoisted(() => ({ historyGet: vi.fn(), get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), writable: true }));
vi.mock('@/api/clinicalTransport', () => ({ clinicalClient: () => ({ ...mocks, get: (path: string, config: unknown) => path.includes('genomics-legacy-cytogenetics') ? mocks.historyGet(path, config) : mocks.get(path, ...(config === undefined ? [] : [config])) }), clinicalUrl: (path: string) => `/host${path}` }));
vi.mock('@/hooks/useWritableFields', () => ({ useWritableFields: () => ({ descriptors: { genetic_mutations: { writable: mocks.writable } } }) }));

const saved = { id: 123, provenance: 'asserted', gene: 'BRCA1', variant: 'c.68_69delAG', origin: 'Germline', interpretation: 'Pathogenic', genome_assembly: 'GRCh38' };
const url = '/host/v1/patient-records/42/genomics/';

beforeEach(() => {
  vi.clearAllMocks(); mocks.writable = true;
  mocks.historyGet.mockResolvedValue({ data: { results: [], next_cursor: null } });
  mocks.get.mockImplementation(async (path: string) => ({ data: path.includes('genomics-catalog') ? { markers: [] } : [saved] }));
  mocks.post.mockImplementation(async (_url, data) => ({ data: { ...data, id: 456 } }));
  mocks.patch.mockImplementation(async (_url, data) => ({ data }));
  mocks.delete.mockResolvedValue({});
});

const renderModal = (props: Partial<Parameters<typeof GenomicsFindings>[0]> = {}) => render(
  <GenomicsFindings personId={42} disease="Breast Cancer" variant="modal" onClose={() => {}} {...props} />,
);

describe('genomic findings modal', () => {
  it('is the dialog itself and lists the recorded findings in it', async () => {
    renderModal();

    const dialog = await screen.findByRole('dialog', { name: 'Genomic findings' });
    expect(within(dialog).getByText('c.68_69delAG')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Add genomic finding' })).toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith(url);
    expect(mocks.get).toHaveBeenCalledWith('/host/v1/patient-records/42/genomics-catalog/', { params: { disease: 'Breast Cancer' } });
  });

  it('replaces the list with the form instead of stacking a second dialog', async () => {
    renderModal();
    fireEvent.click(await screen.findByRole('button', { name: 'Add genomic finding' }));

    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'New genomic finding' })).toBeInTheDocument();
    expect(screen.getByText('Specimen ID')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByRole('dialog', { name: 'Genomic findings' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Specimen ID')).not.toBeInTheDocument();
  });

  it('opens one finding inside the same dialog and goes back to the list', async () => {
    renderModal();
    fireEvent.click(await screen.findByRole('button', { name: 'View' }));

    const details = screen.getByRole('dialog', { name: 'Genomic finding details' });
    expect(within(details).getByText('GRCh38')).toBeInTheDocument();
    fireEvent.click(within(details).getByRole('button', { name: 'Back to findings' }));

    expect(await screen.findByRole('dialog', { name: 'Genomic findings' })).toBeInTheDocument();
  });

  it('tells the host after a write, because the host row is rendered from its own query', async () => {
    const onChanged = vi.fn();
    renderModal({ onChanged });
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Interpretation'), { target: { value: 'Likely pathogenic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));

    await screen.findByText('Genomic finding saved.');
    expect(mocks.patch).toHaveBeenCalledWith(`${url}123/`, expect.objectContaining({ interpretation: 'Likely pathogenic' }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('tells the host after a delete as well', async () => {
    const onChanged = vi.fn();
    renderModal({ onChanged });
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));

    await screen.findByText('Genomic finding deleted.');
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('offers no editing to a read-only host', async () => {
    renderModal({ readOnly: true });
    await screen.findByText('c.68_69delAG');

    expect(screen.queryByRole('button', { name: 'Add genomic finding' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'View' })).toBeInTheDocument();
  });

  it('reports dismissal so the host can unmount the widget', async () => {
    const onClose = vi.fn();
    renderModal({ onClose });
    const dialog = await screen.findByRole('dialog', { name: 'Genomic findings' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('closes from the done button without a write', async () => {
    const onClose = vi.fn();
    renderModal({ onClose });
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));

    expect(onClose).toHaveBeenCalled();
    expect(mocks.patch).not.toHaveBeenCalled();
    expect(mocks.post).not.toHaveBeenCalled();
  });

  it('keeps a failed save in the form and reports the server message', async () => {
    mocks.patch.mockRejectedValue({ response: { data: { test_date: 'Use a valid date.' } } });
    renderModal();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Use a valid date.');
    expect(screen.getByRole('dialog', { name: 'Edit genomic finding' })).toBeInTheDocument();
  });

  it('stays closeable while the findings are still loading', async () => {
    mocks.get.mockImplementation(() => new Promise(() => {}));
    const onClose = vi.fn();
    renderModal({ onClose });

    const dialog = await screen.findByRole('dialog', { name: 'Genomic findings' });
    expect(within(dialog).getByRole('status')).toHaveTextContent('Loading genomics');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('stays closeable when the findings fail to load, and offers a retry', async () => {
    mocks.get.mockRejectedValue(new Error('offline'));
    const onClose = vi.fn();
    renderModal({ onClose });

    const dialog = await screen.findByRole('dialog', { name: 'Genomic findings' });
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not load genomics');
    expect(within(dialog).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('uses the transport it was given, not whichever one is registered globally', async () => {
    const own = {
      get: vi.fn(async (path: string) => ({ data: path.includes('genomics-catalog') ? { markers: [] }
        : path.includes('genomics-legacy-cytogenetics') ? { results: [], next_cursor: null }
        : [saved] })),
      post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    };
    renderModal({ client: own as never, url: (path: string) => `/own${path}` });

    await screen.findByText('c.68_69delAG');
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));

    await waitFor(() => expect(own.patch).toHaveBeenCalledWith(
      '/own/v1/patient-records/42/genomics/123/',
      expect.objectContaining({ id: 123 }),
    ));
    expect(mocks.patch).not.toHaveBeenCalled();
  });
});
