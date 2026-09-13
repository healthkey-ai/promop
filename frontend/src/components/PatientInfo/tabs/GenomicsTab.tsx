import { useEffect, useState } from 'react';
import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { Button } from '@/components/shadcn/button';
import { useWritableFields } from '@/hooks/useWritableFields';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui-labs/dialog';

type Variant = { id?: number; gene?: string; variant?: string; [key: string]: string | number | null | undefined };
type Marker = { key: string; field_name: string; gene: string; label: string; kind: string; aliases: string[]; writable: boolean; expert_review: string };

const fields = [
  ['gene', 'Gene'],
  ['variant_name', 'Variant name'],
  ['variant', 'Original variant text'],
  ['transcript_dna_change', 'Transcript DNA change (c.HGVS)'],
  ['origin', 'Origin'],
  ['interpretation', 'Interpretation'],
  ['test_date', 'Test date'],
  ['genome_assembly', 'Genome assembly'],
  ['transcript_reference_sequence_id', 'Transcript reference sequence ID'],
  ['amino_acid_change', 'Protein / amino acid change (p.HGVS)'],
  ['variant_category', 'Variant category'],
  ['variant_analysis_method_type', 'Variant analysis method type'],
  ['genomic_source_class', 'Genomic source class'],
  ['chromosome', 'Chromosome'],
  ['cytogenetic_location', 'Cytogenetic location'],
  ['genomic_dna_change', 'Genomic DNA change (g.HGVS)'],
  ['allelic_frequency', 'Sample variant allele frequency (VAF)'],
  ['clone_fraction', 'Clone fraction'],
  ['coverage_depth', 'Coverage depth'],
  ['amino_acid_change_type', 'Amino acid change type'],
  ['specimen_id', 'Specimen ID'],
  ['specimen_type', 'Specimen / tissue type'],
  ['collection_date', 'Specimen collection date'],
  ['report_id', 'Report ID'],
  ['laboratory', 'Laboratory'],
  ['interpretation_date', 'Interpretation date'],
  ['classification_framework', 'Classification framework / version'],
  ['evidence_source', 'Interpretation evidence / source'],
  ['genomic_reference_sequence_id', 'Genomic reference sequence ID'],
  ['zygosity', 'Zygosity'],
  ['status', 'Finding status'],
] as const;

/** Fields rendered as `<select>` dropdowns with a fixed value set. */
const selectOptions: Record<string, string[]> = {
  origin: ['Germline', 'Somatic', 'Unknown'],
  interpretation: ['Pathogenic', 'Likely pathogenic', 'VUS', 'Likely benign', 'Benign', 'Uncertain'],
  genome_assembly: ['GRCh38', 'GRCh37'],
  variant_category: ['Simple variant', 'Structural variant'],
  genomic_source_class: ['Germline', 'Somatic', 'De novo', 'Unknown'],
  variant_analysis_method_type: ['Sequencing', 'Next generation sequencing', 'Sanger sequencing', 'PCR', 'FISH', 'Microarray'],
  status: ['present', 'absent', 'indeterminate'],
  zygosity: ['Heterozygous', 'Homozygous', 'Hemizygous', 'Unknown'],
  chromosome: ['1','2','3','4','5','6','7','8','9','10','11','12','13','14','15','16','17','18','19','20','21','22','X','Y'],
};

const numericFields = new Set(['allelic_frequency', 'clone_fraction', 'coverage_depth']);
const absentVariantFields = ['amino_acid_change', 'allelic_frequency', 'genomic_dna_change',
  'transcript_reference_sequence_id', 'transcript_dna_change', 'amino_acid_change_type', 'zygosity'];

function findingStatus(finding: Variant): string {
  if (!finding.id) return 'Unknown';
  const state = finding.status || (['no_call', 'not_tested'].includes(String(finding.assessment))
    ? 'indeterminate' : finding.assessment) || 'present';
  return ({ present: 'Present', absent: 'Absent', indeterminate: 'Indeterminate' } as Record<string, string>)[String(state)] || 'Indeterminate';
}

function changeStatus(draft: Variant, status: string): Variant {
  const changed: Variant = { ...draft, status };
  if (status !== draft.status) delete changed.assessment;
  if (status === 'absent') for (const key of absentVariantFields) changed[key] = '';
  return changed;
}

function errorMessage(error: unknown): string {
  const data = (error as { response?: { data?: Record<string, unknown> } })?.response?.data;
  return data ? Object.entries(data).map(([key, value]) => `${key}: ${String(value)}`).join('; ')
    : 'Could not save genomics changes. Please try again.';
}

export default function GenomicsTab({ formData, readOnly = false }: {
  formData: Record<string, unknown>;
  readOnly?: boolean;
}) {
  const personId = (formData.person_id ?? formData.person) as number | undefined;
  const { descriptors } = useWritableFields(personId);
  const editable = !readOnly && descriptors.genetic_mutations?.writable === true;
  const [variants, setVariants] = useState<Variant[]>([]);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [reload, setReload] = useState(0);
  const [draft, setDraft] = useState<Variant | null>(null);
  const [viewing, setViewing] = useState<Variant | null>(null);
  const [deleting, setDeleting] = useState<Variant | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const url = clinicalUrl(`/v1/patient-records/${personId}/genomics/`);
  const disease = String(formData.disease ?? formData.disease_slug ?? '');
  const markerFor = (v: Variant | null) => v ? markers.find(m => v.marker_key ? m.key === v.marker_key : m.kind === 'gene' && m.gene.toUpperCase() === v.gene?.toUpperCase()) : undefined;
  const rows: Variant[] = markers.flatMap(m => {
    const existing = variants.filter(v => markerFor(v)?.key === m.key);
    return existing.length ? existing : [{ gene: m.gene, marker_key: m.key }];
  }).concat(variants.filter(v => !markerFor(v)));

  useEffect(() => {
    let current = true;
    setLoading(true);
    setLoadError(false);
    setDraft(null);
    setViewing(null);
    setDeleting(null);
    if (!personId) { setLoading(false); setLoadError(true); return; }
    Promise.all([
      clinicalClient().get<Variant[]>(url),
      clinicalClient().get<{ markers: Marker[] }>(clinicalUrl(`/v1/patient-records/${personId}/genomics-catalog/`), { params: { disease } }),
    ]).then(([result, catalog]) => {
      if (current) { setVariants(result.data); setMarkers(catalog.data.markers); }
    }).catch(() => { if (current) setLoadError(true); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [personId, url, reload, disease]);

  async function save() {
    if (!draft || !editable) return;
    setBusy(true); setError(''); setStatus('');
    try {
      const payload = { ...draft, allelic_frequency: draft.allelic_frequency === '' ? null : draft.allelic_frequency };
      const marker = markerFor(draft);
      if (marker) {
        const findings = variants.filter(v => markerFor(v)?.key === marker.key && v.id !== draft.id);
        const { data } = await clinicalClient().patch<Record<string, Variant[]>>(
          clinicalUrl(`/v1/patient-records/${personId}/`),
          { [marker.field_name]: [...findings, { ...payload, marker_key: marker.key }] },
        );
        setVariants(data.genetic_mutations);
      } else {
        const { data } = draft.id
        ? await clinicalClient().patch<Variant>(`${url}${draft.id}/`, payload)
        : await clinicalClient().post<Variant>(url, payload);
        setVariants(old => draft.id ? old.map(v => v.id === data.id ? data : v) : [data, ...old]);
      }
      setDraft(null); setStatus('Variant saved.');
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!deleting || !editable) return;
    setBusy(true); setError(''); setStatus('');
    try {
      await clinicalClient().delete(`${url}${deleting.id}/`);
      setVariants(old => old.filter(v => v.id !== deleting.id));
      setDeleting(null); setViewing(null); setStatus('Variant deleted.');
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  if (loading) return <p role="status">Loading genomics…</p>;
  if (loadError) return <div role="alert">Could not load genomics. <Button variant="outline" onClick={() => setReload(n => n + 1)}>Retry</Button></div>;

  return <section className="space-y-5" aria-label="Patient genomic variants" data-patient-field="genetic_mutations">
    <div className="flex items-center justify-between gap-3">
      <p className="text-sm text-muted-foreground">{variants.length} gene / variant record{variants.length === 1 ? '' : 's'}</p>
      {editable && <Button disabled={busy || !!draft} onClick={() => {
        setDraft({ gene: '', variant: '', status: 'present', allelic_frequency_unit: '%', clone_fraction_unit: '%' });
        setViewing(null); setDeleting(null); setError(''); setStatus('');
      }}>Add variant</Button>}
    </div>
    {error && !draft && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {status && <p role="status" className="text-sm text-emerald-700">{status}</p>}
    {!variants.length && <p className="text-sm text-muted-foreground">No genomic variants recorded. Priority rows below are unknown until a result is saved.</p>}
    {!!rows.length && <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead><tr className="border-b">{['Gene', 'Mutation', 'Finding status', 'Origin', 'Interpretation', 'Actions'].map(label => <th key={label} className="p-2 font-medium">{label}</th>)}</tr></thead>
        <tbody>{rows.map(v => <tr key={v.id ?? String(v.marker_key)} tabIndex={0}
          aria-label={`${v.gene} ${v.variant || markerFor(v)?.label || ''}`}
          onClick={() => { if (!busy && !draft) { setViewing(v); setDeleting(null); } }}
          onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setViewing(v); } }}
          className="border-b align-top cursor-pointer hover:bg-muted/50">
          <td className="p-2 font-medium">{v.gene || '—'}</td>
          <td className="p-2 break-words max-w-xs">{v.variant || v.variant_name || v.genomic_dna_change || 'Not recorded'}
            {markerFor(v)?.kind === 'abnormality' && <span className="block text-xs text-muted-foreground">{markerFor(v)?.label}</span>}
          </td>
          <td className="p-2">{findingStatus(v)}</td>
          <td className="p-2">{v.origin || v.genomic_source_class || '—'}</td>
          <td className="p-2">{v.interpretation || '—'}</td>
          <td className="p-2" onClick={e => e.stopPropagation()}><div className="flex gap-1">
            <Button size="sm" variant="ghost" disabled={busy || !!draft} onClick={() => { setViewing(v); setDeleting(null); }}>View</Button>
            {editable && markerFor(v)?.writable !== false && <>
              <Button size="sm" variant="ghost" disabled={busy || !!draft} onClick={() => { setDraft({ ...v }); setViewing(null); setDeleting(null); setError(''); }}>Edit</Button>
              {v.id && <Button size="sm" variant="ghost" disabled={busy || !!draft} onClick={() => { setDeleting(v); setError(''); }}>Delete</Button>}
            </>}
          </div></td>
        </tr>)}</tbody>
      </table>
    </div>}
    {deleting && <div role="alertdialog" aria-label="Delete variant" className="rounded-md border p-4 space-y-3">
      <p>Delete {deleting.gene} {deleting.variant_name || deleting.variant} from the active genomics list?</p>
      <div className="flex gap-2"><Button disabled={busy} onClick={remove}>Confirm delete</Button><Button variant="outline" disabled={busy} onClick={() => setDeleting(null)}>Cancel</Button></div>
    </div>}
    <Dialog open={!!viewing || !!draft} onOpenChange={open => { if (!open && !busy) { setViewing(null); setDraft(null); setError(''); } }}>
    <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto" onEscapeKeyDown={e => { if (busy) e.preventDefault(); }} onInteractOutside={e => { if (busy) e.preventDefault(); }}>
      <DialogTitle>{draft ? (draft.id ? 'Edit variant' : 'New variant') : 'Variant details'}</DialogTitle>
      <DialogDescription>Record the laboratory findings and their interpretation. An empty value is unknown, not a negative test.</DialogDescription>
      {markerFor((draft || viewing)!)?.expert_review && <p className="text-sm text-amber-700">{markerFor((draft || viewing)!)?.expert_review}</p>}
    {viewing && <div className="space-y-4">
      {editable && markerFor(viewing)?.writable !== false && <Button onClick={() => { setDraft({ ...viewing }); setViewing(null); }}>Edit result</Button>}
      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-4">{[...fields, ['assessment', 'Source result assessment'], ['variant_description', 'Full variant description']].map(([key, label]) => <div key={key}>
        <dt className="text-sm text-muted-foreground">{label}</dt><dd className="text-sm whitespace-pre-wrap break-words">{key === 'status' ? findingStatus(viewing) : viewing[key] ?? '—'}{['allelic_frequency', 'clone_fraction'].includes(key) && viewing[key] != null ? ` ${viewing[`${key}_unit`] === '1' ? '(fraction)' : '%'}` : ''}</dd>
      </div>)}</dl>
    </div>}
    {draft && <form className="rounded-md border p-4 space-y-4" onSubmit={e => { e.preventDefault(); void save(); }}>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <fieldset disabled={busy} className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {fields.map(([key, label]) => {
          const opts = selectOptions[key];
          const draftMarker = markerFor(draft);
          const aliasList = key === 'variant_name' && draftMarker?.kind === 'abnormality' ? [draftMarker.label, ...draftMarker.aliases] : undefined;
          return <label key={key} className="space-y-1 text-sm">
            <span className="block font-medium">{label}{key === 'gene' ? ' *' : ''}</span>
            {opts ? <select disabled={draft.status === 'absent' && absentVariantFields.includes(key)} className="w-full rounded-md border bg-background px-3 py-2"
              value={draft[key] ?? (key === 'status' ? 'present' : '')} onChange={e => setDraft(key === 'status' ? changeStatus(draft, e.target.value) : { ...draft, [key]: e.target.value })}>
              {key !== 'status' && <option value="">— Select —</option>}
              {opts.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
            : <input className="w-full rounded-md border bg-background px-3 py-2" required={key === 'gene'}
              type={key.endsWith('_date') ? 'date' : numericFields.has(key) ? 'number' : 'text'}
              readOnly={key === 'gene' && !!draft.marker_key}
              min={numericFields.has(key) ? 0 : undefined}
              max={['allelic_frequency', 'clone_fraction'].includes(key) ? (draft[`${key}_unit`] === '1' ? 1 : 100) : undefined}
              step={key === 'coverage_depth' ? 'any' : numericFields.has(key) ? '0.00001' : undefined}
              disabled={draft.status === 'absent' && absentVariantFields.includes(key)}
              maxLength={key === 'gene' ? 50 : 10000}
              list={aliasList ? `genomics-aliases-${key}` : undefined}
              value={draft[key] ?? ''} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />}
            {aliasList && <datalist id={`genomics-aliases-${key}`}>{aliasList.map(v => <option key={v} value={v} />)}</datalist>}
          </label>;
        })}
        <label className="space-y-1 text-sm"><span className="block font-medium">Allelic frequency unit</span>
          <select className="w-full rounded-md border bg-background px-3 py-2" value={draft.allelic_frequency_unit || '%'} onChange={e => setDraft({ ...draft, allelic_frequency_unit: e.target.value })}>
            <option value="%">Percent (0–100)</option><option value="1">Fraction (0–1)</option>
          </select>
        </label>
        <label className="space-y-1 text-sm"><span className="block font-medium">Clone fraction unit</span>
          <select className="w-full rounded-md border bg-background px-3 py-2" value={draft.clone_fraction_unit || '%'} onChange={e => setDraft({ ...draft, clone_fraction_unit: e.target.value })}>
            <option value="%">Percent (0–100)</option><option value="1">Fraction (0–1)</option>
          </select>
        </label>
        {draft.assessment && <p className="text-sm">Source result assessment: {draft.assessment}</p>}
        <label className="space-y-1 text-sm sm:col-span-2"><span className="block font-medium">Full variant description</span>
          <textarea className="w-full rounded-md border bg-background px-3 py-2" rows={4} maxLength={10000} value={draft.variant_description ?? ''} onChange={e => setDraft({ ...draft, variant_description: e.target.value })} />
        </label>
      </fieldset>
      <div className="flex gap-2"><Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save variant'}</Button><Button type="button" variant="outline" disabled={busy} onClick={() => { setDraft(null); setError(''); }}>Cancel</Button></div>
    </form>}
    </DialogContent>
    </Dialog>
  </section>;
}
