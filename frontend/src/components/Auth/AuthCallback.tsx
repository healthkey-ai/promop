import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { exchangeCodeForToken } from "@/utils/oauth";

export function AuthCallback() {
  const navigate = useNavigate();
  const location = useLocation();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleCallback = async () => {
      const urlParams = new URLSearchParams(location.search);
      const code = urlParams.get("code");
      const state = urlParams.get("state");
      const oauthError = urlParams.get("error");

      if (oauthError) {
        const desc = urlParams.get("error_description") ?? oauthError;
        setError(`Authorization denied: ${desc}`);
        setTimeout(() => navigate("/login"), 3000);
        return;
      }

      if (!code) {
        setError("No authorization code received");
        setTimeout(() => navigate("/login"), 2000);
        return;
      }

      try {
        await exchangeCodeForToken(code, state ?? "");
        navigate("/", { replace: true });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Authentication failed");
        setTimeout(() => navigate("/login"), 3000);
      }
    };

    handleCallback();
  }, [navigate, location]);

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center">
        <p className="text-lg font-semibold text-destructive">{error}</p>
        <p className="mt-2 text-sm text-muted-foreground">Redirecting to login…</p>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center">
      <div className="h-12 w-12 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      <p className="mt-4 text-lg font-semibold">Completing authentication…</p>
    </div>
  );
}
