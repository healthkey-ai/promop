import { useState, useEffect, useCallback } from "react";
import api from "@/api/axios";

interface User {
  id: number;
  sub: string;
  email: string;
  name: string;
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
      const msg =
        error &&
        typeof error === "object" &&
        "response" in error &&
        (error as { response?: { data?: { error?: string } } }).response?.data?.error;
      return { success: false as const, error: msg || "Login failed" };
    }
  };

  const logout = async () => {
    try {
      await api.post("/auth/logout/");
    } finally {
      setCurrentUser(null);
      window.location.href = "/login";
    }
  };

  const refresh = useCallback(async () => {
    return fetchCurrentUser();
  }, [fetchCurrentUser]);

  return { currentUser, loading, login, logout, refresh };
};
