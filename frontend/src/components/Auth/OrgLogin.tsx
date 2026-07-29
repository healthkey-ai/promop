import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { publicApi } from "@/api/publicAxios";
import { useAuth } from "@/hooks/useAuth";

export default function OrgLogin() {
  const { slug } = useParams<{ slug: string }>();
  const { login } = useAuth();

  const [orgName, setOrgName] = useState("");
  const [allowsSignup, setAllowsSignup] = useState(false);
  const [orgLoading, setOrgLoading] = useState(true);
  const [orgError, setOrgError] = useState("");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const res = await publicApi.get(`/v1/orgs/${slug}/public/`);
        setOrgName(res.data.name);
        setAllowsSignup(res.data.allows_patient_signup);
      } catch {
        setOrgError("Organization not found.");
      } finally {
        setOrgLoading(false);
      }
    })();
  }, [slug]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await login(email, password);
      if (result.success) {
        if (!result.user?.is_patient) {
          setError("This login page is for patients. Staff members should use the main login.");
        } else {
          // Full page reload to re-fetch auth state from the server.
          window.location.href = `/org/${slug}/`;
        }
      } else {
        setError(result.error);
      }
    } finally {
      setLoading(false);
    }
  };

  if (orgLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-muted/40">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (orgError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-muted/40">
        <div className="w-full max-w-md space-y-4 rounded-lg bg-background p-8 shadow-lg text-center">
          <p className="text-sm text-destructive">{orgError}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40">
      <div className="w-full max-w-md space-y-8 rounded-lg bg-background p-8 shadow-lg">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-foreground">
            {orgName}
          </h2>
          <p className="mt-2 text-center text-sm text-muted-foreground">
            Sign in to your account
          </p>
        </div>

        {error && (
          <div className="rounded-md bg-destructive/10 p-4">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        <form className="space-y-6" onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-foreground">
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="Enter your email"
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-foreground">
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="Enter your password"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="flex w-full justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>

        {allowsSignup && (
          <p className="text-center text-sm text-muted-foreground">
            Don&apos;t have an account?{" "}
            <Link
              to={`/org/${slug}/signup`}
              className="font-medium text-primary hover:text-primary/80"
            >
              Sign up
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
