import { useState, useEffect, useCallback } from "react";
import { AlertCircle, Shield } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";

interface Consent {
  id: number;
  consent_type: string;
  consent_granted: boolean;
  consent_date: string | null;
}

const CONSENT_LABELS: Record<string, string> = {
  data_sharing: "Data Sharing",
  clinical_trial: "Clinical Trial Participation",
  research: "Research Use",
};

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "Never updated";
  try {
    return new Date(dateStr).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

export default function PatientConsents({ user }: { user: User | null }) {
  const [consents, setConsents] = useState<Consent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<number | null>(null);

  const fetchConsents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/consents/");
      setConsents(Array.isArray(res.data) ? res.data : res.data.results ?? []);
    } catch {
      setError("Failed to load consent preferences. Please try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user && user.person_id != null) {
      fetchConsents();
    } else {
      setLoading(false);
    }
  }, [user, fetchConsents]);

  const handleToggle = async (consent: Consent) => {
    setTogglingId(consent.id);
    try {
      const res = await api.patch(`/v1/consents/${consent.id}/`, {
        consent_granted: !consent.consent_granted,
      });
      setConsents((prev) =>
        prev.map((c) => (c.id === consent.id ? { ...c, ...res.data } : c))
      );
    } catch {
      setError("Failed to update consent. Please try again.");
    } finally {
      setTogglingId(null);
    }
  };

  if (!user || user.person_id == null) {
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-amber-400" />
        <p className="text-sm text-portal-text-secondary">
          No health record is linked to your account. Consent preferences will
          be available once your record is set up.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="flex items-center justify-between rounded-lg border border-border p-4">
              <div className="space-y-1.5">
                <div className="h-4 w-36 animate-pulse rounded bg-muted" />
                <div className="h-3 w-24 animate-pulse rounded bg-muted" />
              </div>
              <div className="h-6 w-11 animate-pulse rounded-full bg-muted" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center gap-2 mb-1">
        <Shield className="h-5 w-5 text-portal-brand" />
        <h2 className="text-xl font-bold text-foreground">Consent Preferences</h2>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Manage how your health data is used and shared.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {consents.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">No consent preferences found.</p>
      )}

      <div className="space-y-3">
        {consents.map((consent) => (
          <div
            key={consent.id}
            className="flex items-center justify-between rounded-lg border border-border px-5 py-4 transition-colors hover:bg-muted/30"
          >
            <div>
              <p className="text-sm font-medium text-foreground">
                {CONSENT_LABELS[consent.consent_type] ?? consent.consent_type}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Last updated: {formatDate(consent.consent_date)}
              </p>
            </div>
            <button
              onClick={() => handleToggle(consent)}
              disabled={togglingId === consent.id}
              aria-label={`Toggle ${CONSENT_LABELS[consent.consent_type] ?? consent.consent_type}`}
              className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
                consent.consent_granted ? "bg-primary" : "bg-gray-200"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  consent.consent_granted ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
