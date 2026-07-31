import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { publicApi } from "@/api/publicAxios";

export default function ForgotPassword() {
  const { slug } = useParams<{ slug: string }>();

  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!email.trim()) {
      setError("Please enter your email address.");
      return;
    }
    setLoading(true);
    try {
      await publicApi.post("/v1/auth/request-reset/", { email });
      setSubmitted(true);
    } catch {
      setError("Something went wrong. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const loginPath = slug ? `/org/${slug}/login` : "/login";

  if (submitted) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-muted/40">
        <div className="w-full max-w-md space-y-6 rounded-lg bg-background p-8 shadow-lg text-center">
          <h2 className="text-2xl font-semibold text-foreground">Check your email</h2>
          <p className="text-sm text-muted-foreground">
            If an account exists with that email, we've sent a link to reset your password.
          </p>
          <Link
            to={loginPath}
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
            Forgot password?
          </h2>
          <p className="mt-2 text-center text-sm text-muted-foreground">
            Enter your email and we'll send you a reset link.
          </p>
        </div>

        {error && (
          <div className="rounded-md bg-destructive/10 p-4">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        <form className="space-y-6" onSubmit={handleSubmit}>
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

          <button
            type="submit"
            disabled={loading}
            className="flex w-full justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-sm hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? "Sending..." : "Send reset link"}
          </button>
        </form>

        <p className="text-center text-sm text-muted-foreground">
          <Link
            to={loginPath}
            className="font-medium text-primary hover:text-primary/80"
          >
            Back to sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
