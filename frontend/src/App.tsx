import { useEffect, type ReactNode } from "react";
import {
  Routes,
  Route,
  Navigate,
  useLocation,
  useParams,
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
import FieldMappingPage from "@/components/FieldMappings/FieldMappingPage";
import CodeMappingPage from "@/components/CodeMappings/CodeMappingPage";
import MappingHubPage from "@/components/MappingHub/MappingHubPage";
import TherapyMappingPage from "@/components/TherapyMappings/TherapyMappingPage";
import OrgLogin from "@/components/Auth/OrgLogin";
import OrgSignup from "@/components/Auth/OrgSignup";
import ForgotPassword from "@/components/Auth/ForgotPassword";
import UserProfilePage from "@/components/User/UserProfilePage";
import { useAuth } from "@/hooks/useAuth";

function OrgHome({
  currentUser,
  authLoading,
  logout,
}: {
  currentUser: ReturnType<typeof useAuth>["currentUser"];
  authLoading: boolean;
  logout: () => void;
}) {
  const { slug } = useParams();
  if (authLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }
  if (!currentUser) return <Navigate to={`/org/${slug}/login`} replace />;
  if (!currentUser.is_patient) return <Navigate to="/" replace />;
  // Verify the patient belongs to this org; redirect to their own org if not.
  if (!currentUser.org_accesses?.some(a => a.org_slug === slug)) {
    const myOrg = currentUser.org_accesses?.find(a => a.role === 'patient');
    return myOrg
      ? <Navigate to={`/org/${myOrg.org_slug}/`} replace />
      : <Navigate to="/" replace />;
  }
  return <PatientHome user={currentUser} onLogout={logout} />;
}

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

  const publicPaths = ['/accept-invite', '/accept-patient-invite', '/reset-password', '/forgot-password', '/login', '/auth/callback'];
  const isPublicPath = (path: string) =>
    publicPaths.includes(path) || /^\/org\/[^/]+\/(login|signup|forgot-password|accept-invite)$/.test(path);
  if (authLoading && !isPublicPath(location.pathname)) {
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
  const forceChangeExemptPaths = ['/reset-password', '/forgot-password', '/accept-invite', '/accept-patient-invite'];
  const isForceChangeExempt = (path: string) =>
    forceChangeExemptPaths.includes(path) || /^\/org\/[^/]+\/(login|signup|forgot-password|accept-invite)$/.test(path);
  if (
    currentUser?.must_change_password &&
    !isForceChangeExempt(location.pathname)
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

  // Mapping administration is available to platform staff and organization
  // administrators.  The API independently enforces the same policy.
  const mappingAdminRoute = (element: ReactNode) => {
    if (!currentUser) return <Navigate to="/login" replace />;
    if (!currentUser.is_staff && !currentUser.is_org_admin) return <Navigate to="/" replace />;
    return element;
  };

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route path="/accept-invite" element={<AcceptInvite />} />
      {/* Alias for invitation emails sent before the link was un-nested — the
          token in the query string carries everything; the slug is cosmetic. */}
      <Route path="/org/:slug/accept-invite" element={<AcceptInvite />} />
      <Route path="/accept-patient-invite" element={<AcceptPatientInvite />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />

      <Route path="/org/:slug/login" element={<OrgLogin />} />
      <Route path="/org/:slug/signup" element={<OrgSignup />} />
      <Route path="/org/:slug/forgot-password" element={<ForgotPassword />} />
      <Route path="/org/:slug" element={<OrgHome currentUser={currentUser} authLoading={authLoading} logout={logout} />} />

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
      <Route path="/patient/:personId" element={providerRoute(<PatientDetail user={currentUser} />)} />
      <Route path="/upload-fhir" element={providerRoute(<UploadFHIR />)} />
      <Route path="/upload-csv" element={providerRoute(<UploadCSV />)} />
      <Route path="/stats" element={<Navigate to="/org-admin" replace />} />
      <Route path="/org-admin" element={providerRoute(<OrgAdminPage />)} />
      <Route path="/mappings" element={mappingAdminRoute(<MappingHubPage />)} />
      <Route path="/field-mappings" element={mappingAdminRoute(<FieldMappingPage />)} />
      <Route path="/code-mappings" element={mappingAdminRoute(<CodeMappingPage />)} />
      <Route path="/therapy-mappings" element={mappingAdminRoute(<TherapyMappingPage />)} />
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
