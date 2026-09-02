import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "@/api/axios";
import { publicApi } from "@/api/publicAxios";
import { getActiveBranding } from "@/config/branding";

const MIN_PASSWORD_LENGTH = 12;

type Mode = "signin" | "signup";

interface SignupOrg {
  name: string;
  slug: string;
}

export function Login() {
  const branding = getActiveBranding();
  const [mode, setMode] = useState<Mode>("signin");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Orgs accepting patient self-signup. Empty (or an unreachable endpoint)
  // means this deployment has none, and the Sign Up tab stays hidden.
  const [signupOrgs, setSignupOrgs] = useState<SignupOrg[]>([]);
  const [eligibleSignupOrgs, setEligibleSignupOrgs] = useState<SignupOrg[]>([]);
  const [orgSlug, setOrgSlug] = useState("");
  const [signupEmail, setSignupEmail] = useState("");
  const [signupPassword, setSignupPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [givenName, setGivenName] = useState("");
  const [familyName, setFamilyName] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const res = await publicApi.get("/v1/orgs/signup-directory/");
        const orgs: SignupOrg[] = Array.isArray(res.data) ? res.data : [];
        setSignupOrgs(orgs);
        if (orgs.length === 1) setOrgSlug(orgs[0].slug);
      } catch {
        setSignupOrgs([]);
      }
    })();
  }, []);

  useEffect(() => {
    const normalizedEmail = signupEmail.trim().toLowerCase();
    if (!normalizedEmail.includes('@')) {
      setEligibleSignupOrgs([]);
      setOrgSlug('');
      return;
    }
    (async () => {
      try {
        const res = await publicApi.get(`/v1/orgs/signup-directory/?email=${encodeURIComponent(normalizedEmail)}`);
        const orgs: SignupOrg[] = Array.isArray(res.data) ? res.data : [];
        setEligibleSignupOrgs(orgs);
        setOrgSlug((current) => orgs.some((org) => org.slug === current)
          ? current
          : (orgs.length === 1 ? orgs[0].slug : ''));
      } catch {
        setEligibleSignupOrgs([]);
        setOrgSlug('');
      }
    })();
  }, [signupEmail]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const response = await api.post("/auth/login/", { username, password });
      if (response.data.user) {
        window.location.href = "/";
      }
    } catch (err: unknown) {
      let msg = "Login failed. Please try again.";
      if (err && typeof err === "object" && "response" in err) {
        const resp = (err as { response?: { data?: { error?: string } } }).response;
        if (resp?.data?.error) msg = resp.data.error;
      }
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleSignup = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (!orgSlug) {
      setError("Please choose an organization.");
      return;
    }
    if (signupPassword.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (signupPassword !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setLoading(true);
    try {
      await publicApi.post(`/v1/orgs/${orgSlug}/patient-signup/`, {
        email: signupEmail,
        password: signupPassword,
        given_name: givenName,
        family_name: familyName,
      });
      // Backend auto-logs in via session cookie — go straight to patient home.
      window.location.href = `/org/${orgSlug}/`;
    } catch (err: unknown) {
      let msg = "Signup failed. Please try again.";
      if (err && typeof err === "object" && "response" in err) {
        const resp = (err as {
          response?: { data?: { error?: string | string[]; errors?: Record<string, string[]> } };
        }).response;
        const fieldErrors = resp?.data?.errors;
        if (fieldErrors && typeof fieldErrors === "object") {
          const messages = Object.values(fieldErrors).flat();
          if (messages.length > 0) msg = messages.join(" ");
        } else {
          const rawError = resp?.data?.error;
          if (rawError) msg = Array.isArray(rawError) ? rawError.join(" ") : rawError;
        }
      }
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "mt-1 block w-full rounded-md border border-input px-3 py-2 shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary";

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40">
      <div className="w-full max-w-md space-y-8 rounded-lg bg-background p-8 shadow-lg">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-foreground">
            {branding.appName || "PRomop"}
          </h2>
          <p className="mt-2 text-center text-sm text-muted-foreground">
            {branding.tagline || "An Open Source Personal Health Record from HealthKey.ai"}
          </p>
        </div>

        {signupOrgs.length > 0 && (
          <div
            role="tablist"
            aria-label="Sign in or sign up"
            className="flex rounded-lg border border-input p-0.5"
          >
            {([
              ["signin", "Sign In"],
              ["signup", "Sign Up"],
            ] as const).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={mode === value}
                onClick={() => switchMode(value)}
                className={`flex-1 rounded-md py-2 text-sm font-medium transition-colors ${
                  mode === value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}

        {error && (
          <div className="rounded-md bg-destructive/10 p-4">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        {mode === "signin" ? (
          <form className="space-y-6" onSubmit={handleSubmit}>
            <div className="space-y-4">
              <div>
                <label htmlFor="username" className="block text-sm font-medium text-foreground">
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  required
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className={inputClass}
                  placeholder="Enter your username"
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
                  className={inputClass}
                  placeholder="Enter your password"
                />
              </div>
            </div>

            <div className="flex items-center justify-end">
              <Link
                to="/forgot-password"
                className="text-sm font-medium text-primary hover:text-primary/80"
              >
                Forgot password?
              </Link>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="flex w-full justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Signing in..." : "Sign in"}
            </button>
          </form>
        ) : (
          <form className="space-y-6" onSubmit={handleSignup}>
            <div className="space-y-4">
              <div>
                <label htmlFor="signupEmail" className="block text-sm font-medium text-foreground">
                  Email
                </label>
                <input
                  id="signupEmail"
                  name="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={signupEmail}
                  onChange={(e) => setSignupEmail(e.target.value)}
                  className={inputClass}
                  placeholder="Enter your email"
                />
              </div>

              <div>
                <label htmlFor="signupOrg" className="block text-sm font-medium text-foreground">
                  Organization
                </label>
                {/* Deliberately not `required`: native constraint validation would
                    surface a browser tooltip instead of the in-card message every
                    other field on this form uses. handleSignup checks it. */}
                <select
                  id="signupOrg"
                  value={orgSlug}
                  onChange={(e) => setOrgSlug(e.target.value)}
                  className={inputClass}
                  disabled={!signupEmail.includes('@')}
                >
                  <option value="">{signupEmail.includes('@') ? 'Select your organization' : 'Enter your email first'}</option>
                  {eligibleSignupOrgs.map((org) => (
                    <option key={org.slug} value={org.slug}>
                      {org.name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="givenName" className="block text-sm font-medium text-foreground">
                    First name <span className="font-normal text-muted-foreground">(optional)</span>
                  </label>
                  <input
                    id="givenName"
                    type="text"
                    value={givenName}
                    onChange={(e) => setGivenName(e.target.value)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label htmlFor="familyName" className="block text-sm font-medium text-foreground">
                    Last name <span className="font-normal text-muted-foreground">(optional)</span>
                  </label>
                  <input
                    id="familyName"
                    type="text"
                    value={familyName}
                    onChange={(e) => setFamilyName(e.target.value)}
                    className={inputClass}
                  />
                </div>
              </div>

              <div>
                <label htmlFor="signupPassword" className="block text-sm font-medium text-foreground">
                  Password
                </label>
                <input
                  id="signupPassword"
                  name="new-password"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={signupPassword}
                  onChange={(e) => setSignupPassword(e.target.value)}
                  className={inputClass}
                  placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Must be at least {MIN_PASSWORD_LENGTH} characters, not a common password, and not
                  entirely numeric.
                </p>
              </div>

              <div>
                <label
                  htmlFor="confirmPassword"
                  className="block text-sm font-medium text-foreground"
                >
                  Confirm password
                </label>
                <input
                  id="confirmPassword"
                  name="confirm-password"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className={inputClass}
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
        )}
      </div>
    </div>
  );
}
