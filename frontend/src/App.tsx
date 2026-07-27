import { useEffect, type ReactNode } from "react";
import {
  Routes,
  Route,
  Navigate,
  useLocation,
} from "react-router-dom";
import { Login } from "@/components/Auth/Login";
import { AuthCallback } from "@/components/Auth/AuthCallback";
import AcceptInvite from "@/components/Auth/AcceptInvite";
import AcceptPatientInvite from "@/components/Auth/AcceptPatientInvite";
import ResetPassword from "@/components/Auth/ResetPassword";
import ChangePassword from "@/components/Auth/ChangePassword";
import PatientList from "@/components/Patient/PatientList";
import PatientDetail from "@/components/Patient/PatientDetail";
import PatientHome from "@/components/Patient/PatientHome";
import UploadFHIR from "@/components/Patient/UploadFHIR";
import UploadCSV from "@/components/Patient/UploadCSV";
import OrgAdminPage from "@/components/OrgAdmin/OrgAdminPage";
import UserProfilePage from "@/components/User/UserProfilePage";
import { useAuth } from "@/hooks/useAuth";

function AppRoutes() {
  const { currentUser, loading: authLoading, refresh, logout } = useAuth();
  const location = useLocation();

  useEffect(() => {
    if (location.pathname === "/auth/callback") {
      setTimeout(() => {
        refresh();
      }, 500);
    }
  }, [location.pathname, refresh]);

  const publicPaths = ['/accept-invite', '/accept-patient-invite', '/reset-password', '/login', '/auth/callback'];
  if (authLoading && !publicPaths.includes(location.pathname)) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  // Force-change gate (PHR-S FM TI.1.1#09): a signed-in account flagged for a
  // password change is held on a blocking screen until it sets a new one. The
  // backend independently refuses every other /api/ request meanwhile, so this
  // is a UX affordance over a server-enforced rule. Public auth pages (reset
  // link, invite acceptance) are exempt so those flows can still complete.
  const forceChangeExemptPaths = ['/reset-password', '/accept-invite', '/accept-patient-invite'];
  if (
    currentUser?.must_change_password &&
    !forceChangeExemptPaths.includes(location.pathname)
  ) {
    return <ChangePassword onChanged={refresh} onLogout={logout} />;
  }

  const isPatient = !!currentUser?.is_patient;

  // Provider-only routes: require a signed-in provider. Signed-in patients are
  // redirected home (their own record); logged-out users go to login.
  const providerRoute = (element: ReactNode) => {
    if (!currentUser) return <Navigate to="/login" replace />;
    if (isPatient) return <Navigate to="/" replace />;
    return element;
  };

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route path="/accept-invite" element={<AcceptInvite />} />
      <Route path="/accept-patient-invite" element={<AcceptPatientInvite />} />
      <Route path="/reset-password" element={<ResetPassword />} />

      <Route
        path="/"
        element={
          currentUser ? (
            isPatient ? (
              <PatientHome user={currentUser} onLogout={logout} />
            ) : (
              <PatientList />
            )
          ) : (
            <Navigate to="/login" replace />
          )
        }
      />
      <Route path="/patient/:personId" element={providerRoute(<PatientDetail />)} />
      <Route path="/upload-fhir" element={providerRoute(<UploadFHIR />)} />
      <Route path="/upload-csv" element={providerRoute(<UploadCSV />)} />
      <Route path="/stats" element={<Navigate to="/org-admin" replace />} />
      <Route path="/org-admin" element={providerRoute(<OrgAdminPage />)} />
      <Route
        path="/profile"
        element={
          currentUser ? <UserProfilePage /> : <Navigate to="/login" replace />
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return <AppRoutes />;
}
