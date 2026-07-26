import { useState } from "react";
import { AlertCircle, Download } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import PatientDetail from "@/components/Patient/PatientDetail";
import PatientConsents from "@/components/Patient/PatientConsents";
import PatientSurveys from "@/components/Patient/PatientSurveys";
import PatientMessages from "@/components/Patient/PatientMessages";
import AdvanceDirectives from "@/components/Patient/AdvanceDirectives";
import ImmunizationList from "@/components/Patient/ImmunizationList";
import AllergyList from "@/components/Patient/AllergyList";
import api from "@/api/axios";

/**
 * Patient (PHR Account Holder) landing view — PHR-S FM PH.1 / PH.2.
 *
 * Renders the signed-in patient's OWN record in patient mode by reusing
 * PatientDetail with a fixed person_id. Providers never reach this component;
 * routing in App.tsx sends them to the provider console instead.
 */
export default function PatientHome({
  user,
  onLogout,
}: {
  user: User | null;
  onLogout: () => void;
}) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  if (!user || user.person_id == null) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f5f7fa] p-6">
        <div className="w-full max-w-sm rounded-2xl bg-background p-8 text-center shadow">
          <AlertCircle className="mx-auto mb-3 h-10 w-10 text-amber-400" />
          <p className="mb-6 text-sm text-portal-text-secondary">
            No health record is linked to your account yet. Please contact your care team.
          </p>
          <button
            onClick={onLogout}
            className="inline-flex items-center gap-2 text-sm font-medium text-portal-brand hover:underline"
          >
            Sign out
          </button>
        </div>
      </div>
    );
  }

  const handleDownloadFhir = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await api.get(`/v1/patient-records/${user.person_id}/export-fhir/`);
      const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: "application/fhir+json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "my-health-record.fhir.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setDownloadError("Failed to download your health record. Please try again.");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div>
      <div className="flex justify-end px-6 pt-4">
        <button
          onClick={handleDownloadFhir}
          disabled={downloading}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Download className="h-4 w-4" />
          {downloading ? "Downloading..." : "Download my record (FHIR)"}
        </button>
      </div>
      {downloadError && (
        <div className="px-6 pt-2">
          <p className="text-sm text-red-600">{downloadError}</p>
        </div>
      )}
      <div className="mx-auto max-w-5xl px-6 py-4">
        <PatientConsents user={user} />
      </div>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <PatientSurveys user={user} />
      </div>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <PatientMessages user={user} />
      </div>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <AllergyList user={user} />
      </div>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <ImmunizationList user={user} />
      </div>
      <div className="mx-auto max-w-5xl px-6 py-4">
        <AdvanceDirectives user={user} />
      </div>
      <PatientDetail
        personIdOverride={String(user.person_id)}
        patientMode
        onLogout={onLogout}
      />
    </div>
  );
}
