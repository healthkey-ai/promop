import { useState } from 'react';
import { Download, FileJson, FileText } from 'lucide-react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/shadcn/popover';
import type { DiseaseType } from './patientReport';
import { downloadFhir, downloadPdf } from './downloadRecord';

interface Props {
  personId: number;
  patientMode: boolean;
  /** The record as the page holds it, unsaved edits included. */
  record: Record<string, unknown>;
  name: string;
  diseaseType: DiseaseType;
  diseaseLabel?: string;
}

type Format = 'fhir' | 'pdf';

/**
 * The patient record's Download button: FHIR for systems, PDF for people.
 * Shown to every role that can open the record — a patient downloads their own,
 * staff download the one they are viewing; the server's export permission
 * decides the rest.
 */
export default function DownloadRecordMenu({ personId, patientMode, record, name, diseaseType, diseaseLabel }: Props) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Format | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (format: Format) => {
    setOpen(false);
    setBusy(format);
    setError(null);
    try {
      if (format === 'fhir') await downloadFhir(personId, patientMode);
      else await downloadPdf({ record, name, personId, diseaseType, diseaseLabel, patientMode });
    } catch {
      setError(format === 'fhir'
        ? 'Could not download the FHIR record. Please try again.'
        : 'Could not create the PDF. Please try again.');
    } finally {
      setBusy(null);
    }
  };

  const option = (format: Format, Icon: typeof FileText, title: string, detail: string) => (
    <button
      type="button"
      onClick={() => { void run(format); }}
      className="flex w-full items-start gap-2.5 rounded-md px-2.5 py-2 text-left hover:bg-[#eef0f4]"
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
      <span>
        <span className="block text-sm font-medium text-foreground">{title}</span>
        <span className="block text-xs text-muted-foreground">{detail}</span>
      </span>
    </button>
  );

  return (
    <div className="flex shrink-0 items-center gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            disabled={busy !== null}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            {busy ? 'Preparing…' : 'Download'}
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-64 p-1.5" aria-label="Download format">
          {option('pdf', FileText, 'PDF report', 'A formatted copy of the record tabs, to read or print.')}
          {option('fhir', FileJson, 'FHIR R4 bundle (JSON)', 'Machine-readable, for another health system or app.')}
        </PopoverContent>
      </Popover>
      {error && <span role="alert" className="text-[11px] text-red-600">{error}</span>}
    </div>
  );
}
