import { Link } from 'react-router-dom';

export default function UploadPage() {
  return (
    <div className="p-6">
      <Link to="/" className="text-sm text-muted-foreground hover:text-foreground">Back to Patient List</Link>
      <h1 className="mt-4 text-2xl font-bold">Upload patient data</h1>
      <p className="mt-2 text-muted-foreground">Choose the format of your file.</p>
      <div className="mt-6 flex flex-wrap gap-4">
        <Link to="/upload-fhir" className="rounded-lg border border-input p-6 hover:bg-accent">
          <h2 className="font-semibold">FHIR</h2>
          <p className="mt-2 text-sm text-muted-foreground">Import a FHIR JSON bundle with patient and clinical records.</p>
        </Link>
        <Link to="/upload-csv" className="rounded-lg border border-input p-6 hover:bg-accent">
          <h2 className="font-semibold">CSV</h2>
          <p className="mt-2 text-sm text-muted-foreground">Import patient demographics and dated diagnoses.</p>
        </Link>
      </div>
    </div>
  );
}
