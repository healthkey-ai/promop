import { useId, useRef, useState, type ChangeEvent, type ReactNode } from 'react';

interface Props {
  /** True while an upload, a sync or a delete is running: nothing starts beside it. */
  disabled: boolean;
  /** True while this section's own upload is running. */
  uploading: boolean;
  /** What the last upload from this section said, and what went wrong. */
  result: string | null;
  error: string | null;
  /** The chosen files, already checked to be an Oura export (.zip) or its CSV files. */
  onFiles: (files: File[]) => void;
  /** The linked account (`OuraAccountLink`), above the upload; absent where it is not offered. */
  account?: ReactNode;
  /** The section's uploads (a collapsed `UploadsList`). */
  children?: ReactNode;
}

/** Oura's export is a zip of CSV files; one of those files alone is read as well. */
const OURA_EXTENSIONS = ['.zip', '.csv'];

/**
 * Oura Ring (#2889): uploads the data export Oura's Membership Hub makes, under
 * the linked account (#2900) where this deployment has one. It
 * has its own button because a `.zip` here is Oura's, where the Upload button
 * above reads a `.zip` as Apple Health. Offered only on the patient's own
 * record, as every upload is (#2819).
 */
export default function OuraUpload({ disabled, uploading, result, error, onFiles, account, children }: Props) {
  const headingId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [refused, setRefused] = useState<string | null>(null);

  const choose = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    if (inputRef.current) inputRef.current.value = '';
    if (files.length === 0) return;
    if (!files.every(file => OURA_EXTENSIONS.some(ext => file.name.toLowerCase().endsWith(ext)))) {
      setRefused('Please choose the Oura export (.zip) or one of its .csv files.');
      return;
    }
    setRefused(null);
    onFiles(files);
  };

  const idle = !disabled && !uploading;
  const problem = refused ?? error;

  return (
    <section aria-labelledby={headingId} className="mb-4 rounded-md border border-gray-200 px-4 pt-3 pb-2">
      <h3 id={headingId} className="text-sm font-medium text-portal-text-primary">Oura Ring</h3>
      <div className="divide-y divide-gray-200">
        {account}
        <div className="py-3 last:pb-1">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Upload your Oura data</h4>
          <p className="mt-1 text-xs text-gray-500">
            Request an export in Oura&rsquo;s Membership Hub (Export data), then upload the file Oura
            sends, or any of the .csv files in it. Steps, active minutes, sleep, heart rate variability,
            resting heart rate, breathing rate, blood oxygen and VO&#8322; max are read.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              aria-disabled={!idle}
              disabled={!idle}
              className="inline-flex items-center rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {uploading ? 'Sending...' : 'Choose Oura export'}
            </button>
            <input
              ref={inputRef}
              type="file"
              accept={OURA_EXTENSIONS.join(',')}
              multiple
              className="hidden"
              aria-label="Oura export files"
              onChange={choose}
            />
            {result && !problem && <span role="status" className="text-sm text-green-600">{result}</span>}
            {problem && <span role="alert" className="text-sm text-red-600">{problem}</span>}
          </div>
        </div>
        {children}
      </div>
    </section>
  );
}
