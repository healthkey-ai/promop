import { useState, type FormEvent } from "react";
import { Upload, X } from "lucide-react";
import api from "@/api/axios";
import { INPUT_CLASS } from "@/components/UI/MappingFormPrimitives";
import { formatUploadError } from "./codeMappingUploadError";

export interface CodeMappingUploadResult {
  upload_id: number;
  duplicate: boolean;
  total: number;
  inserted: number;
  updated: number;
  unchanged: number;
}

interface Props {
  defaultProvenance: string;
  onClose: () => void;
  onUploaded: (result: CodeMappingUploadResult) => void | Promise<void>;
}

export default function CodeMappingUploadDialog({ defaultProvenance, onClose, onUploaded }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [vocabulary, setVocabulary] = useState("");
  const [provenance, setProvenance] = useState(defaultProvenance);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!file) {
      setError("Choose a CSV file.");
      return;
    }
    setSubmitting(true);
    setError("");
    const body = new FormData();
    body.append("file", file);
    body.append("source_vocabulary_id", vocabulary.trim());
    body.append("provenance", provenance.trim());
    // The shared api instance declares 'Content-Type: application/json' as a
    // default. axios reads that header before deciding how to encode the body,
    // and for FormData the JSON branch runs: it stringifies the entries and
    // every File collapses to {}. The file is then absent from the request and
    // the server answers "CSV file is required." no matter which file was
    // chosen. Clearing the header for this one request lets the browser set
    // multipart/form-data with its own boundary.
    void api.post<CodeMappingUploadResult>("/v1/code-mappings/upload/", body,
      { headers: { "Content-Type": undefined } })
      .then(({ data }) => onUploaded(data), (caught: unknown) => {
        setError(formatUploadError(caught));
      })
      .finally(() => setSubmitting(false));
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-950/40 p-4">
      <form onSubmit={submit} role="dialog" aria-modal="true" aria-label="Upload source codes"
        className="w-full max-w-lg rounded-md bg-white shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
          <h2 className="text-lg font-semibold text-slate-950">Upload source codes</h2>
          <button type="button" onClick={onClose} aria-label="Close upload"
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100">
            <X size={16} />
          </button>
        </div>
        <div className="space-y-4 px-5 py-5">
          <p className="text-sm text-slate-600">
            CSV headers: <code>source code</code>, <code>source description</code>, optional <code>seen count</code>,
            optional <code>destination concept ID</code>, and optional <code>state</code>. State accepts <code>Proposed</code> or
            {" "}<code>Approved</code>; blank defaults to Proposed.
          </p>
          {error && <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
          <div className="block text-sm font-medium text-slate-800">
            <span>CSV file</span>
            <div className="mt-1 flex min-h-10 items-center gap-3 rounded-md border border-slate-300 px-3 py-1.5">
              <label className="inline-flex shrink-0 cursor-pointer items-center gap-2 rounded-md bg-slate-950 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 focus-within:ring-2 focus-within:ring-slate-400 focus-within:ring-offset-2">
                <Upload size={15} aria-hidden="true" />
                Choose file
                <input aria-label="CSV file" type="file" accept=".csv,text/csv" required
                  onChange={(event) => setFile(event.target.files?.[0] || null)}
                  className="sr-only" />
              </label>
              <span className="min-w-0 truncate text-sm font-normal text-slate-600">
                {file?.name || "No file selected"}
              </span>
            </div>
          </div>
          <label className="block text-sm font-medium text-slate-800">
            Source vocabulary
            <input aria-label="Source vocabulary" value={vocabulary} maxLength={255} required
              onChange={(event) => setVocabulary(event.target.value)} className={`${INPUT_CLASS} mt-1`} />
          </label>
          <label className="block text-sm font-medium text-slate-800">
            Provenance
            <input aria-label="Provenance" value={provenance} maxLength={50} required
              onChange={(event) => setProvenance(event.target.value)} className={`${INPUT_CLASS} mt-1`} />
          </label>
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-4">
          <button type="button" onClick={onClose} disabled={submitting}
            className="rounded-md border border-slate-300 px-4 py-2 text-sm">Cancel</button>
          <button type="submit" disabled={submitting || !file || !vocabulary.trim() || !provenance.trim()}
            className="rounded-md bg-slate-950 px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
            {submitting ? "Uploading…" : "Upload"}
          </button>
        </div>
      </form>
    </div>
  );
}
