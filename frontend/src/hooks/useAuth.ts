import { useState, useEffect, useCallback } from "react";
import api from "@/api/axios";

interface OrgAccess {
  org_name: string;
  org_slug: string;
  role: string | null;
  expires_at: string | null;
  access_via?: Array<"invitation" | "invitation_pending" | "trusted_domain">;
}

export interface User {
  id: number;
  sub: string;
  email: string;
  name: string;
  is_staff?: boolean;
  is_superuser?: boolean;
  is_org_admin?: boolean;
  org_accesses?: OrgAccess[];
  // PHR Account Holder (patient) role — see PHR-S FM PH.1. When is_patient is
  // true, person_id is the patient's own record and the UI runs in patient mode.
  is_patient?: boolean;
  person_id?: number | null;
  // Force-change-at-next-login (PHR-S FM TI.1.1#09). When true, the backend
  // refuses every /api/ request except change-password until the password is
  // reset, and the SPA shows a blocking change-password screen.
  must_change_password?: boolean;
}

export const useAuth = () => {
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchCurrentUser = useCallback(async () => {
    try {
      setLoading(true);
      const response = await api.get("/user/");
      const userData = response.data.user || response.data;
      setCurrentUser(userData);
      return userData;
    } catch (error) {
      if (
        error &&
        typeof error === "object" &&
        "response" in error &&
        (error as { response?: { status?: number } }).response?.status === 401
      ) {
        setCurrentUser(null);
      }
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    fetchCurrentUser();
  }, [fetchCurrentUser]);

  const login = async (username: string, password: string) => {
    try {
      const response = await api.post("/auth/login/", { username, password });
      const userData = response.data.user;
      setCurrentUser(userData);
      return { success: true as const, user: userData };
    } catch (error) {
      let errorMessage = "Login failed";
      if (
        error &&
        typeof error === "object" &&
        "response" in error
      ) {
        const serverMsg = (error as { response?: { data?: { error?: string } } }).response?.data?.error;
        if (serverMsg) errorMessage = serverMsg;
      }
      return { success: false as const, error: errorMessage };
    }
  };

  const logout = async () => {
    try {
      await api.post("/auth/logout/");
    } finally {
      setCurrentUser(null);
      const orgMatch = window.location.pathname.match(/^\/org\/([^/]+)/);
      window.location.href = orgMatch ? `/org/${orgMatch[1]}/login` : "/login";
    }
  };

  const refresh = useCallback(async () => {
    return fetchCurrentUser();
  }, [fetchCurrentUser]);

  return { currentUser, loading, login, logout, refresh };
};
