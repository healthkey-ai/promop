import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import GenomicsTab from './GenomicsTab';

const mocks = vi.hoisted(() => ({ historyGet: vi.fn(), get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn(), writable: true }));
vi.mock('@/api/clinicalTransport', () => ({ clinicalClient: () => ({ ...mocks, get: (path: string, config: unknown) => path.includes('genomics-legacy-cytogenetics') ? mocks.historyGet(path, config) : mocks.get(path, ...(config === undefined ? [] : [config])) }), clinicalUrl: (path: string) => `/host${path}` }));
vi.mock('@/hooks/useWritableFields', () => ({ useWritableFields: () => ({ descriptors: { genetic_mutations: { writable: mocks.writable } } }) }));

const saved = { id: 123, provenance: 'asserted', gene: 'BRCA1', variant: 'c.68_69delAG', origin: 'Germline', interpretation: 'Pathogenic', genome_assembly: 'GRCh38', variant_description: 'Complete source report', allelic_frequency: 0, allelic_frequency_unit: '%' };
const url = '/host/v1/patient-records/42/genomics/';

beforeEach(() => {
  vi.clearAllMocks(); mocks.writable = true;
  mocks.historyGet.mockResolvedValue({ data: { results: [], next_cursor: null } });
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
    expect(screen.getByLabelText('Genomic feature *')).toHaveValue('BRCA1');
    expect(screen.getByLabelText('Specimen ID')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Original variant text'), { target: { value: 'c.68_69delAG' } });
    mocks.patch.mockResolvedValueOnce({ data: { genetic_mutations: [{ ...saved, marker_key: 'brca1' }] } });
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
    await screen.findByText('Genomic finding saved.');
    expect(mocks.patch).toHaveBeenCalledWith('/host/v1/patient-records/42/', {
      genomics_brca1: [expect.objectContaining({ genomic_feature: 'BRCA1', marker_key: 'brca1', variant: 'c.68_69delAG' })],
    });
  });

  it('keeps repeated variants and restores a priority placeholder after deletion', async () => {
    mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: priority } : [{ ...saved, marker_key: 'brca1' }] }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'BC' }} />);
    await screen.findByText('c.68_69delAG');
    expect(screen.getAllByText('BRCA1')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await screen.findByText('Genomic finding deleted.');
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
    fireEvent.click(await screen.findByRole('button', { name: 'Add genomic finding' }));
    fireEvent.change(screen.getByLabelText('Genomic feature *'), { target: { value: 'NTRK3' } });
    fireEvent.change(screen.getByLabelText('Finding / variant name'), { target: { value: 'ETV6-NTRK3 fusion' } });
    fireEvent.change(screen.getByLabelText('Feature type'), { target: { value: 'Gene' } });
    expect(mocks.post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
    expect(await screen.findByText('Genomic finding saved.')).toBeInTheDocument();
    expect(mocks.post).toHaveBeenCalledWith(url, expect.objectContaining({ genomic_feature: 'NTRK3', variant_name: 'ETV6-NTRK3 fusion' }));
    expect(screen.getByText('NTRK3')).toBeInTheDocument();
  });

  it('updates the specific record, preserves other fields and cancels without saving', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Original variant text')).toHaveValue('c.68_69delAG');
    fireEvent.change(screen.getByLabelText('Interpretation'), { target: { value: 'Likely pathogenic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
    await waitFor(() => expect(mocks.patch).toHaveBeenCalled());
    expect(mocks.patch).toHaveBeenCalledWith(`${url}123/`, expect.objectContaining({ id: 123, provenance: 'asserted', genome_assembly: 'GRCh38', interpretation: 'Likely pathogenic' }));
    expect(await screen.findByText('Genomic finding saved.')).toBeInTheDocument();
    expect(screen.getByText('Likely pathogenic')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Genomic feature *'), { target: { value: 'Changed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mocks.patch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Changed')).not.toBeInTheDocument();
  });

  it('requires confirmation and removes the row only when DELETE succeeds', async () => {
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(mocks.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    expect(await screen.findByText('Genomic finding deleted.')).toBeInTheDocument();
    expect(mocks.delete).toHaveBeenCalledWith(`${url}123/`);
    expect(screen.getByText(/No genomic findings recorded/)).toBeInTheDocument();
  });

  it('keeps failed edits and reports server validation errors', async () => {
    mocks.patch.mockRejectedValue({ response: { data: { test_date: 'Use a valid date.' } } });
    render(<GenomicsTab formData={{ person_id: 42 }} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Use a valid date.');
    expect(screen.getByLabelText('Genomic feature *')).toHaveValue('BRCA1');
  });

  it.each([true, false])('respects host readOnly=%s and caller permissions', async readOnly => {
    mocks.writable = readOnly;
    render(<GenomicsTab formData={{ person_id: 42 }} readOnly={readOnly} />);
    await screen.findByText('BRCA1');
    expect(screen.queryByRole('button', { name: 'Add genomic finding' })).not.toBeInTheDocument();
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
    for (const field of ['Origin', 'Interpretation', 'Genome assembly', 'Finding category', 'Genomic source class', 'Variant analysis method type', 'Finding status', 'Zygosity', 'Chromosome']) {
      const el = screen.getByLabelText(field);
      expect(el.tagName).toBe('SELECT');
    }
    // Free-text fields remain inputs
    for (const field of ['Finding / variant name', 'Original variant text', 'Transcript reference sequence ID']) {
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
      key: 'del17p', field_name: 'genomics_del17p', gene: 'TP53', genomic_feature: '17p', feature_type: 'Chromosome arm/region', finding_category: 'Deletion',
      label: 'del(17p)', kind: 'abnormality', aliases: ['del(17p)', 'del(17p13)', 'del17p13'],
      writable: true, expert_review: '',
    };
    mocks.get.mockImplementation(async path => ({
      data: path.includes('genomics-catalog') ? { markers: [abnormality] } : [],
    }));
    render(<GenomicsTab formData={{ person_id: 42, disease: 'CLL' }} />);
    await screen.findByText('17p');
    fireEvent.click(screen.getByRole('row', { name: '17p del(17p)' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit result' }));
    const variantInput = screen.getByLabelText('Finding / variant name') as HTMLInputElement;
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

it('keeps the new DNA, protein, depth and fraction controls independent on save', async () => {
  render(<GenomicsTab formData={{ person_id: 42 }} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  for (const [label, value] of [
    ['Transcript DNA change (c.HGVS)', 'c.123A>G'], ['Genomic DNA change (g.HGVS)', 'g.321A>G'],
    ['Protein / amino acid change (p.HGVS)', 'p.Arg41Gly'], ['Amino acid change type', 'missense'],
    ['Coverage depth', '250'], ['Clone fraction', '0.4'],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.change(screen.getByLabelText('Clone fraction unit'), { target: { value: '1' } });
  expect(screen.getByLabelText('Allelic frequency unit')).toHaveValue('%');
  expect(screen.getByLabelText('Original variant text')).toHaveValue(saved.variant);
  fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
  await screen.findByText('Genomic finding saved.');
  expect(mocks.patch).toHaveBeenCalledWith(`${url}123/`, expect.objectContaining({
    variant: saved.variant, transcript_dna_change: 'c.123A>G', genomic_dna_change: 'g.321A>G',
    amino_acid_change: 'p.Arg41Gly', amino_acid_change_type: 'missense', coverage_depth: '250',
    allelic_frequency: 0, allelic_frequency_unit: '%', clone_fraction: '0.4', clone_fraction_unit: '1',
  }));
});

it('shows unknown placeholders separately from absent and indeterminate results', async () => {
  mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog')
    ? { markers: [{ key: 'tp53', field_name: 'genomics_tp53', gene: 'TP53', kind: 'gene', label: 'TP53' }] }
    : [{ ...saved, status: 'absent' }, { ...saved, id: 124, gene: 'BRCA2', status: 'indeterminate', assessment: 'no_call' }] }));
  render(<GenomicsTab formData={{ person_id: 42 }} />);
  expect(await screen.findByText('Absent')).toBeInTheDocument();
  expect(screen.getByText('Indeterminate')).toBeInTheDocument();
  expect(screen.getByText('Unknown')).toBeInTheDocument();
  expect(mocks.post).not.toHaveBeenCalled();
});

it('uses one state control and clears incompatible inherited values on an absent edit', async () => {
  mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: [] }
    : [{ ...saved, status: 'present', assessment: 'present', transcript_dna_change: 'c.123A>G',
      amino_acid_change_type: 'missense', zygosity: 'Heterozygous', coverage_depth: 200 }] }));
  render(<GenomicsTab formData={{ person_id: 42 }} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  expect(screen.queryByLabelText('Result assessment')).not.toBeInTheDocument();
  expect(screen.getByText('Source result assessment: present')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Finding status'), { target: { value: 'absent' } });
  expect(screen.getByLabelText('Transcript DNA change (c.HGVS)')).toHaveValue('');
  expect(screen.getByLabelText('Transcript DNA change (c.HGVS)')).toBeDisabled();
  expect(screen.getByLabelText('Amino acid change type')).toBeDisabled();
  expect(screen.getByLabelText('Coverage depth')).toHaveValue(200);
  fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
  await screen.findByText('Genomic finding saved.');
  const payload = mocks.patch.mock.calls[0][1];
  expect(payload.status).toBe('absent');
  expect(payload.assessment).toBeUndefined();
  expect(payload.transcript_dna_change).toBe('');
  expect(payload.coverage_depth).toBe(200);
  expect(screen.getByText('Absent')).toBeInTheDocument();
});

it('separates TP53 sequence findings from 17p deletions and preserves source details', async () => {
  const deletion = { ...saved, id: 125, gene: 'TP53', genomic_feature: '17p', feature_type: 'Chromosome arm/region',
    variant: 'Original FISH narrative', variant_name: 'del17p', finding_category: 'Deletion', variant_category: 'Structural variant', marker_key: 'del17p' };
  mocks.get.mockImplementation(async path => ({ data: path.includes('genomics-catalog') ? { markers: [] }
    : [{ ...saved, gene: 'TP53', genomic_feature: 'TP53', feature_type: 'Gene', variant_name: 'p.R175H', finding_category: 'Sequence variant' }, deletion] }));
  render(<GenomicsTab formData={{ person_id: 42 }} />);
  expect(await screen.findByText('2 genomic finding records')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Genomic feature' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Finding / variant' })).toBeInTheDocument();
  expect(screen.getByRole('row', { name: 'TP53 p.R175H' })).toBeInTheDocument();
  const row = screen.getByRole('row', { name: '17p del17p' });
  fireEvent.click(within(row).getByRole('button', { name: 'View' }));
  expect(screen.getByRole('dialog', { name: 'Genomic finding details' })).toBeInTheDocument();
  expect(screen.getByText('Chromosome arm/region')).toBeInTheDocument();
  expect(screen.getByText('Deletion')).toBeInTheDocument();
  expect(screen.getByText('Structural variant')).toBeInTheDocument();
  expect(screen.getByText('Original FISH narrative')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Edit result' }));
  expect(screen.getByLabelText('Genomic feature *')).toHaveValue('17p');
  expect(screen.getByLabelText('Feature type')).toBeDisabled();
  expect(screen.getByLabelText('Finding category')).toHaveValue('Deletion');
  mocks.patch.mockResolvedValueOnce({ data: { ...deletion, laboratory: 'Reviewed lab' } });
  fireEvent.change(screen.getByLabelText('Laboratory'), { target: { value: 'Reviewed lab' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
  await screen.findByText('Genomic finding saved.');
  expect(mocks.patch).toHaveBeenCalledWith(`${url}125/`, expect.objectContaining({ genomic_feature: '17p', gene: 'TP53', variant: 'Original FISH narrative', finding_category: 'Deletion' }));
});

it('creates chromosome findings without a fabricated gene and offers every requested category', async () => {
  render(<GenomicsTab formData={{ person_id: 42 }} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Add genomic finding' }));
  fireEvent.change(screen.getByLabelText('Genomic feature *'), { target: { value: 'Chromosome 12' } });
  fireEvent.change(screen.getByLabelText('Feature type'), { target: { value: 'Chromosome(s)' } });
  const category = screen.getByLabelText('Finding category');
  for (const name of ['Sequence variant', 'Deletion', 'Gain', 'Translocation', 'Aneuploidy', 'Ploidy abnormality', 'Complex structural rearrangement']) {
    expect(within(category).getByRole('option', { name })).toBeInTheDocument();
  }
  fireEvent.change(category, { target: { value: 'Aneuploidy' } });
  fireEvent.change(screen.getByLabelText('Finding / variant name'), { target: { value: 'Trisomy 12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save genomic finding' }));
  await screen.findByText('Genomic finding saved.');
  const payload = mocks.post.mock.calls[0][1];
  expect(payload.gene).toBeUndefined();
  expect(payload).toMatchObject({ genomic_feature: 'Chromosome 12', feature_type: 'Chromosome(s)', finding_category: 'Aneuploidy' });
});
