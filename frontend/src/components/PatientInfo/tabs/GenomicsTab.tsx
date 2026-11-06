import { usePatientFormStore } from '@/federation/PatientFormContext';
import PatientFieldEditor from '@/federation/PatientFieldEditor';
import GenomicsFindings from '../GenomicsFindings';

export default function GenomicsTab({ formData, readOnly = false }: {
  formData: Record<string, unknown>;
  readOnly?: boolean;
}) {
  const personId = (formData.person_id ?? formData.person) as number | undefined;
  const store = usePatientFormStore();
  const disease = String(formData.disease ?? formData.disease_slug ?? '');

  return <section className="space-y-5" aria-label="Patient genomic findings" data-patient-field="genetic_mutations">
    {store && <div className="rounded-md border p-4">
      <PatientFieldEditor store={store} name="tp53_disruption" label="TP53 Disruption" />
    </div>}
    <GenomicsFindings personId={personId} disease={disease} readOnly={readOnly} />
  </section>;
}
