import PageTitle from '@/components/Branding/PageTitle';
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, ArrowLeft } from "lucide-react";
import api from "@/api/axios";
import UploadOrganization from "./UploadOrganization";
import { useUploadOrganization } from "./useUploadOrganization";

export default function UploadFHIR() {
  const navigate = useNavigate();
  const organization = useUploadOrganization();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<{ created_count: number; updated_count: number; errors: (string | { patient: string; error: string })[] } | null>(null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    setSuccess(null);
    const selectedFile = event.target.files?.[0];
    if (selectedFile) {
      if (!selectedFile.name.toLowerCase().endsWith(".json")) {
        setError("Please select a JSON file");
        setFile(null);
        return;
      }
      setFile(selectedFile);
      setError(null);
      setSuccess(null);
    }
  };

  const handleUpload = async () => {
    if (!file || !organization.ready) {
      setError("Please select a file");
      return;
    }

    try {
      setUploading(true);
      setError(null);
      setSuccess(null);

      const formData = new FormData();
      formData.append("file", file);
      if (organization.organization) formData.append("organization", organization.organization);

      const response = await api.post("/patient-info/upload_fhir/", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });

      setSuccess(response.data);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
    } catch (err) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { error?: string; detail?: string } } }).response?.data
          : undefined;
      setError(msg?.error || msg?.detail || "Failed to upload file");
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center gap-4">
        <button onClick={() => navigate("/")} className="inline-flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground">
          <ArrowLeft size={16} /> Back to Patient List
        </button>
        <PageTitle className="text-2xl font-bold">Upload FHIR Bundle</PageTitle>
      </div>

      <div className="max-w-xl rounded-lg border border-border bg-background p-6 shadow-sm">
        <p className="mb-4 text-sm text-muted-foreground">
          Upload a FHIR Bundle (JSON format) containing patient data. The bundle should include
          a Patient resource for each patient. Clinical resources may appear in any order.
        </p>

        <UploadOrganization state={organization} disabled={uploading} />

        <div className="mt-4">
          <input ref={fileInput} disabled={uploading} id="fhir-file-input" type="file" accept=".json" onChange={handleFileChange} className="hidden" />
          <label htmlFor="fhir-file-input">
            <span className="inline-flex w-full cursor-pointer items-center justify-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium hover:bg-accent">
              <Upload size={16} /> Select FHIR JSON File
            </span>
          </label>

          {file && (
            <p className="mt-2 text-sm text-muted-foreground">
              Selected: {file.name} ({(file.size / 1024).toFixed(2)} KB)
            </p>
          )}
        </div>

        {error && (
          <div role="alert" className="mt-4 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{error}</div>
        )}

        {success && (
          <div className="mt-4 rounded-md bg-emerald-50 p-3 text-sm text-emerald-800">
            <p>
              Imported {success.created_count + (success.updated_count ?? 0)} patient(s)
              {success.updated_count > 0 && ` (${success.created_count} new, ${success.updated_count} updated)`}
            </p>
            {success.errors.length > 0 && (
              <div className="mt-2">
                <p className="font-semibold">Errors:</p>
                <ul className="list-inside list-disc">
                  {success.errors.map((err, idx) => <li key={idx}>{typeof err === "string" ? err : `${err.patient}: ${err.error}`}</li>)}
                </ul>
              </div>
            )}
            <button onClick={() => navigate("/")} className="mt-3 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
              Go to Patient List
            </button>
          </div>
        )}

        <button
          onClick={handleUpload}
          disabled={!file || uploading || !organization.ready}
          className="mt-4 flex w-full items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {uploading ? "Uploading..." : "Upload FHIR Bundle"}
        </button>

        <div className="mt-6">
          <h3 className="mb-2 text-sm font-semibold">Expected FHIR Structure:</h3>
          <pre className="overflow-auto rounded-md bg-muted p-3 text-xs">
{`{
  "resourceType": "Bundle",
  "entry": [
    {
      "resource": {
        "resourceType": "Patient",
        "id": "patient-1",
        "name": [{"given": ["John"], "family": "Doe"}],
        "birthDate": "1970-01-01"
      }
    },
    {
      "resource": {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/patient-1"},
        "code": {"text": "Breast Cancer"},
        "onsetDateTime": "2020-06-15"
      }
    }
  ]
}`}
          </pre>
        </div>
      </div>
    </div>
  );
}
