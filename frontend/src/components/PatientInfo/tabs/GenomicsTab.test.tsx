import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GenomicsTab from './GenomicsTab';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), writable: true }));
vi.mock('@/api/clinicalTransport', () => ({ clinicalClient: () => mocks, clinicalUrl: (path: string) => `/host${path}` }));
vi.mock('@/hooks/useWritableFields', () => ({ useWritableFields: () => ({ descriptors: { genetic_mutations: { writable: mocks.writable } } }) }));

const saved = { id: 123, provenance: 'asserted', gene: 'BRCA1', variant: 'c.68_69delAG', origin: 'Germline', interpretation: 'Pathogenic', genome_assembly: 'GRCh38', variant_description: 'Complete source report', allelic_frequency: 0, allelic_frequency_unit: '%' };
const url = '/host/v1/patient-records/42/genomics/';

beforeEach(() => {
  vi.clearAllMocks(); mocks.writable = true;
  mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: [] } : [saved] }));
  mocks.post.mockImplementation(async (_url, data) => ({ data: { ...data, id: 456 } }));
  mocks.patch.mockImplementation(async (_url, data) => ({ data }));
  mocks.delete.mockResolvedValue({});
});

describe('Genomics tab', () => {
  const priority = ['BRCA1', 'BRCA2', 'PIK3CA', 'TP53'].map(gene => ({
    key: gene.toLowerCase(), field_name: `genomics_${gene.toLowerCase()}`, gene,
    label: gene, kind: 'gene', aliases: [], writable: true, expert_review: '',
  }));

  it('precreates priority rows without writes and opens details on row activation', async () => {
    mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: priority } : [] }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'Breast Cancer' }} />);
    await screen.findByText('BRCA1');
    for (const marker of priority) expect(screen.getByText(marker.gene)).toBeInTheDocument();
    expect(screen.getAllByText('Not recorded')).toHaveLength(4);
    expect(mocks.post).not.toHaveBeenCalled();
    expect(mocks.patch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('row', { name: 'BRCA1 BRCA1' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit result' }));
    expect(screen.getByLabelText('Gene *')).toHaveValue('BRCA1');
    expect(screen.getByLabelText('Specimen ID')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Variant / transcript DNA change (HGVS)'), { target: { value: 'c.68_69delAG' } });
    mocks.patch.mockResolvedValueOnce({ data: { genetic_mutations: [{ ...saved, marker_key: 'brca1' }] } });
    fireEvent.click(screen.getByRole('button', { name: 'Save variant' }));
    await screen.findByText('Variant saved.');
    expect(mocks.patch).toHaveBeenCalledWith('/host/v1/patient-records/42/', {
      genomics_brca1: [expect.objectContaining({ gene: 'BRCA1', marker_key: 'brca1', variant: 'c.68_69delAG' })],
    });
  });

  it('keeps repeated variants and restores a priority placeholder after deletion', async () => {
    mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: priority } : [{ ...saved, marker_key: 'brca1' }] }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'BC' }} />);
    await screen.findByText('c.68_69delAG');
    expect(screen.getAllByText('BRCA1')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await screen.findByText('Variant deleted.');
    expect(screen.getByText('BRCA1')).toBeInTheDocument();
    expect(screen.getAllByText('Not recorded')).toHaveLength(4);
  });

  it('switches disease placeholders without removing saved findings', async () => {
    mocks.get.mockImplementation(async (path, config) => ({ data: path.includes('genomics-catalog')
      ? { markers: config.params.disease === 'BC' ? priority : [{ ...priority[0], key: 'ezh2', gene: 'EZH2', label: 'EZH2', field_name: 'genomics_ezh2' }] }
      : [saved] }));
    const { rerender } = render(<GenomicsTab formData={{ person_id: 42, disease: 'BC' }} />);
    await screen.findByText('BRCA2');
    rerender(<GenomicsTab formData={{ person_id: 42, disease: 'FL' }} />);
    await screen.findByText('EZH2');
    expect(screen.queryByText('BRCA2')).not.toBeInTheDocument();
    expect(screen.getByText('c.68_69delAG')).toBeInTheDocument();
    expect(mocks.delete).not.toHaveBeenCalled();
  });

  it('shows unapproved priority mappings without edit controls', async () => {
    mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: [{ ...priority[0], writable: false }] } : [] }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'BC' }} />);
    const row = await screen.findByRole('row', { name: 'BRCA1 BRCA1' });
    expect(within(row).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
  });
  it('lists and displays details, including legacy variant strings and zero frequency', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    expect(await screen.findByText('c.68_69delAG')).toBeInTheDocument();
    expect(mocks.get).toHaveBeenCalledWith(url);
    fireEvent.click(screen.getByRole('button', { name: 'View' }));
    expect(screen.getByText('GRCh38')).toBeInTheDocument();
    expect(screen.getByText('Complete source report')).toBeInTheDocument();
    expect(screen.getByText('0 %')).toBeInTheDocument();
  });

  it('creates only after Save and permits genes outside preset lists', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add variant' }));
    fireEvent.change(screen.getByLabelText('Gene *'), { target: { value: 'NTRK3' } });
    fireEvent.change(screen.getByLabelText('Variant name'), { target: { value: 'ETV6-NTRK3 fusion' } });
    expect(mocks.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save variant' }));
    expect(await screen.findByText('Variant saved.')).toBeInTheDocument();
    expect(mocks.post).toHaveBeenCalledWith(url, expect.objectContaining({ gene: 'NTRK3', variant_name: 'ETV6-NTRK3 fusion' }));
    expect(screen.getByText('NTRK3')).toBeInTheDocument();
  });

  it('updates the specific record, preserves other fields and cancels without saving', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Variant / transcript DNA change (HGVS)')).toHaveValue('c.68_69delAG');
    fireEvent.change(screen.getByLabelText('Interpretation'), { target: { value: 'Likely pathogenic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save variant' }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
    expect(mocks.patch).toHaveBeenCalledWith(`${url}123/`, expect.objectContaining({ id: 123, provenance: 'asserted', genome_assembly: 'GRCh38', interpretation: 'Likely pathogenic' }));
    expect(await screen.findByText('Variant saved.')).toBeInTheDocument();
    expect(screen.getByText('Likely pathogenic')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Gene *'), { target: { value: 'Changed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Changed')).not.toBeInTheDocument();
  });

  it('requires confirmation and removes the row only when DELETE succeeds', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(mocks.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    expect(await screen.findByText('Variant deleted.')).toBeInTheDocument();
    expect(mocks.delete).toHaveBeenCalledWith(`${url}123/`);
    expect(screen.getByText(/No genomic variants recorded/)).toBeInTheDocument();
  });

  it('keeps failed edits and reports server validation errors', async () => {
    mocks.patch.mockRejectedValue({ response: { data: { test_date: 'Use a valid date.' } } });
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save variant' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Use a valid date.');
    expect(screen.getByLabelText('Gene *')).toHaveValue('BRCA1');
  });

  it.each([true, false])('respects host readOnly=%s and caller permissions', async readOnly => {
    mocks.writable = readOnly;
    render(<GenomicsTab formData={{ person_id: 42 }} readOnly={readOnly} />);
    await screen.findByText('BRCA1');
    expect(screen.queryByRole('button', { name: 'Add variant' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('offers retry on loading failures', async () => {
    mocks.get.mockRejectedValueOnce(new Error('offline'));
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('BRCA1')).toBeInTheDocument());
  });

  it('renders select dropdowns for enumerated fields in edit form', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    for (const field of ['Origin', 'Interpretation', 'Genome assembly', 'Variant category', 'Genomic source class', 'Variant analysis method type', 'Result assessment', 'Finding status', 'Zygosity', 'Chromosome']) {
      const el = screen.getByLabelText(field);
      expect(el.tagName).toBe('SELECT');
    }
    // Free-text fields remain inputs
    for (const field of ['Variant name', 'Variant / transcript DNA change (HGVS)', 'Transcript reference sequence ID']) {
      const el = screen.getByLabelText(field);
      expect(el.tagName).toBe('INPUT');
    }
  });

  it('populates select dropdowns with the correct options', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    const origin = screen.getByLabelText('Origin') as HTMLSelectElement;
    const optionTexts = Array.from(origin.options).map(o => o.text);
    expect(optionTexts).toContain('Germline');
    expect(optionTexts).toContain('Somatic');
    expect(optionTexts).toContain('Unknown');
    expect(optionTexts).toContain('— Select —');
  });

  it('shows variant name datalist suggestions for abnormality markers', async () => {
    const abnormality = {
      key: 'del17p', field_name: 'genomics_del17p', gene: 'TP53',
      label: 'del(17p)', kind: 'abnormality', aliases: ['del(17p)', 'del(17p13)', 'del17p13'],
      writable: true, expert_review: '',
    };
    mocks.get.mockImplementation(async path => ({
      data: path.includes('genomics-catalog') ? { markers: [abnormality] } : [],
    }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'CLL' }} />);
    await screen.findByText('TP53');
    fireEvent.click(screen.getByRole('row', { name: 'TP53 del(17p)' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit result' }));
    const variantInput = screen.getByLabelText('Variant name') as HTMLInputElement;
    expect(variantInput.tagName).toBe('INPUT');
    expect(variantInput.getAttribute('list')).toBeTruthy();
    const datalist = document.getElementById(variantInput.getAttribute('list')!);
    expect(datalist).not.toBeNull();
    const suggestions = Array.from(datalist!.querySelectorAll('option')).map(o => o.value);
    expect(suggestions).toContain('del(17p)');
    expect(suggestions).toContain('del(17p13)');
    expect(suggestions).toContain('del17p13');
  });
});
