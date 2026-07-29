import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import axios from "axios";

const publicApi = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "/api",
  headers: { "Content-Type": "application/json" },
});

const MIN_PASSWORD_LENGTH = 8;

export default function OrgSignup() {
  const { slug } = useParams<{ slug: string }>();

  const [orgName, setOrgName] = useState("");
  const [allowsSignup, setAllowsSignup] = useState(false);
  const [orgLoading, setOrgLoading] = useState(true);
  const [orgError, setOrgError] = useState("");

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [givenName, setGivenName] = useState("");
  const [familyName, setFamilyName] = useState("");
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

    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      await publicApi.post(`/v1/orgs/${slug}/patient-signup/`, {
        email,
        password,
        given_name: givenName,
        family_name: familyName,
      });
      // Backend auto-logs in via session cookie — redirect directly to patient home.
      window.location.href = `/org/${slug}/`;
    } catch (err: unknown) {
      let msg = "Signup failed. Please try again.";
      if (err && typeof err === "object" && "response" in err) {
        const resp = (err as { response?: { data?: { error?: string }; status?: number } }).response;
        const rawError = resp?.data?.error;
        if (rawError) msg = Array.isArray(rawError) ? rawError.join(' ') : rawError;
      }
      setError(msg);
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

  if (!allowsSignup) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-muted/40">
        <div className="w-full max-w-md space-y-4 rounded-lg bg-background p-8 shadow-lg text-center">
          <h2 className="text-2xl font-semibold text-foreground">{orgName}</h2>
          <p className="text-sm text-muted-foreground">
            Direct signup is not available for this organization.
          </p>
          <Link
            to={`/org/${slug}/login`}
            className="inline-block text-sm font-medium text-primary hover:text-primary/80"
          >
            Back to sign in
          </Link>
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
            Create your patient account
          </p>
        </div>

        {error && (
          <div className="rounded-md bg-destructive/10 p-4">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        <form className="space-y-6" onSubmit={handleSubmit}>
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label htmlFor="givenName" className="block text-sm font-medium text-foreground">
                  First name
                </label>
                <input
                  id="givenName"
                  type="text"
                  value={givenName}
                  onChange={(e) => setGivenName(e.target.value)}
                  className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                />
              </div>
              <div>
                <label htmlFor="familyName" className="block text-sm font-medium text-foreground">
                  Last name
                </label>
                <input
                  id="familyName"
                  type="text"
                  value={familyName}
                  onChange={(e) => setFamilyName(e.target.value)}
                  className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                />
              </div>
            </div>

            <div>
              <label htmlFor="email" className="block text-sm font-medium text-foreground">
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
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
                autoComplete="new-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="At least 8 characters"
              />
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block text-sm font-medium text-foreground">
                Confirm password
              </label>
              <input
                id="confirmPassword"
                name="confirmPassword"
                type="password"
                autoComplete="new-password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                placeholder="Re-enter your password"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="flex w-full justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Creating account..." : "Create account"}
          </button>
        </form>

        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{" "}
          <Link
            to={`/org/${slug}/login`}
            className="font-medium text-primary hover:text-primary/80"
          >
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
