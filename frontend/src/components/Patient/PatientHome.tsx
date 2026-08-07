import { AlertCircle } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import PatientDetail from "@/components/Patient/PatientDetail";

/**
 * Patient (PHR Account Holder) landing view — PHR-S FM PH.1 / PH.2.
 *
 * Renders the signed-in patient's OWN record in patient mode by reusing
 * PatientDetail with a fixed person_id. Providers never reach this component;
 * routing in App.tsx sends them to the provider console instead.
 *
 * Layout:
 *   PatientDetail — "My Health Record" banner + clinical tabs
 *   (includes Allergies, Immunizations, Surveys tabs and Download button;
 *    Settings and Messages accessible via Account dropdown)
 */
export default function PatientHome({
  user,
  onLogout,
}: {
  user: User | null;
  onLogout: () => void;
}) {
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

  return (
    <PatientDetail
      personIdOverride={String(user.person_id)}
      patientMode
      onLogout={onLogout}
      user={user}
    />
  );
}
