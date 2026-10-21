import { clinicalClient, clinicalUrl } from '@/api/clinicalTransport';
import { buildPatientReport, loadReportExtras, type DiseaseType } from './patientReport';
import { renderReportPdf } from './renderReportPdf';

export function saveFile(data: BlobPart, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([data], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** `patient-123-record`, or the patient's own wording when it is their copy. */
export const baseFilename = (personId: number, patientMode: boolean) =>
  patientMode ? 'my-health-record' : `patient-${personId}-record`;

/**
 * Save the signed FHIR export byte for byte. Parsing and re-serialising it, as
 * this used to, re-indents the JSON and so voids the digest and signature the
 * server computed over exactly the bytes it sent.
 */
export async function downloadFhir(personId: number, patientMode: boolean) {
  const response = await clinicalClient().get(
    clinicalUrl(`/v1/patient-records/${personId}/export-fhir/`),
    { responseType: 'blob' },
  );
  saveFile(response.data as Blob, `${baseFilename(personId, patientMode)}.fhir.json`, 'application/fhir+json');
}

export async function downloadPdf(input: {
  record: Record<string, unknown>;
  name: string;
  personId: number;
  diseaseType: DiseaseType;
  diseaseLabel?: string;
  patientMode: boolean;
}) {
  const extras = await loadReportExtras(input.personId, input.patientMode);
  const bytes = renderReportPdf(buildPatientReport({ ...input, extras }));
  saveFile(bytes as BlobPart, `${baseFilename(input.personId, input.patientMode)}.pdf`, 'application/pdf');
}
