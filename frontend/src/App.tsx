import { useEffect } from "react";
import {
  Routes,
  Route,
  Navigate,
  useLocation,
} from "react-router-dom";
import { Login } from "@/components/Auth/Login";
import { AuthCallback } from "@/components/Auth/AuthCallback";
import PatientList from "@/components/Patient/PatientList";
import PatientDetail from "@/components/Patient/PatientDetail";
import UploadFHIR from "@/components/Patient/UploadFHIR";
import UploadCSV from "@/components/Patient/UploadCSV";
import StatsPage from "@/components/Stats/StatsPage";
import OrgAdminPage from "@/components/OrgAdmin/OrgAdminPage";
import UserProfilePage from "@/components/User/UserProfilePage";
import { useAuth } from "@/hooks/useAuth";

function AppRoutes() {
  const { currentUser, loading: authLoading, refresh } = useAuth();
  const location = useLocation();

  useEffect(() => {
    if (location.pathname === "/auth/callback") {
      setTimeout(() => {
        refresh();
      }, 500);
    }
  }, [location.pathname, refresh]);

  if (authLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />

      <Route
        path="/"
        element={
          currentUser ? <PatientList /> : <Navigate to="/login" replace />
        }
      />
      <Route
        path="/patient/:personId"
        element={
          currentUser ? <PatientDetail /> : <Navigate to="/login" replace />
        }
      />
      <Route
        path="/upload-fhir"
        element={
          currentUser ? <UploadFHIR /> : <Navigate to="/login" replace />
        }
      />
      <Route
        path="/upload-csv"
        element={
          currentUser ? <UploadCSV /> : <Navigate to="/login" replace />
        }
      />
      <Route
        path="/stats"
        element={
          currentUser ? <StatsPage /> : <Navigate to="/login" replace />
        }
      />
      <Route
        path="/org-admin"
        element={
          currentUser ? <OrgAdminPage /> : <Navigate to="/login" replace />
        }
      />
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
