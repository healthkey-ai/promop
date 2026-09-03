import { useState, useEffect, useCallback } from "react";
import { AlertCircle } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";

/**
 * The Surveys tab.
 *
 * Surveys are answered in the PROlog runner — its own application, served at
 * /s/<slug> from this origin. This tab is the index: what is open, and where
 * this patient stands in each.
 *
 * Starting or continuing one navigates out of the portal into the runner, so
 * these are links rather than buttons: middle-click and "open in a new tab"
 * behave, and the runner keeps a respondent's place itself rather than this
 * component tracking answers it no longer owns.
 *
 * PatientDetail draws the tab's card, its "Surveys" heading and its
 * description, so this renders only the list — a second heading inside a
 * second card read as "Surveys inside Surveys".
 */

interface PrologSurvey {
  slug: string;
  version: string;
  title: string;
  url: string;
  status: "not_started" | "in_progress" | "completed";
  started_at: string | null;
  completed_at: string | null;
}

const STATUS: Record<PrologSurvey["status"], { label: string; className: string }> = {
  not_started: { label: "Not started", className: "bg-gray-100 text-gray-600" },
  in_progress: { label: "In progress", className: "bg-blue-100 text-blue-700" },
  completed: { label: "Completed", className: "bg-green-100 text-green-700" },
};

const ACTION: Record<PrologSurvey["status"], string> = {
  not_started: "Start",
  in_progress: "Continue",
  completed: "View",
};

export default function PatientSurveys({ user }: { user: User | null }) {
  const [surveys, setSurveys] = useState<PrologSurvey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSurveys = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/prolog-surveys/");
      setSurveys(Array.isArray(res.data) ? res.data : (res.data.results ?? []));
    } catch {
      setError("Failed to load surveys. Please try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // No synchronous setState in the effect body (react-hooks/set-state-in-effect)
    // — both branches live in the async IIFE, matching the #269 pattern.
    (async () => {
      if (user && user.person_id != null) {
        await fetchSurveys();
      } else {
        setLoading(false);
      }
    })();
  }, [user, fetchSurveys]);

  if (!user || user.person_id == null) {
    return (
      <div className="py-6 text-center">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-amber-400" />
        <p className="text-sm text-portal-text-secondary">
          No health record is linked to your account. Surveys will be available
          once your record is set up.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-4">
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="flex items-center justify-between rounded-lg border border-border p-4"
          >
            <div className="space-y-1.5">
              <div className="h-4 w-48 animate-pulse rounded bg-muted" />
              <div className="h-3 w-20 animate-pulse rounded bg-muted" />
            </div>
            <div className="h-8 w-20 animate-pulse rounded bg-muted" />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div>
      {error && (
        <div
          role="alert"
          className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3"
        >
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
          <button
            type="button"
            onClick={fetchSurveys}
            className="ml-auto text-sm text-red-700 underline"
          >
            Try again
          </button>
        </div>
      )}

      {surveys.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">No surveys available at this time.</p>
      )}

      {/* A list, so the rows are announced as a set and each link has its own
          context; the action links below are otherwise all named "Start". */}
      <ul className="space-y-3 list-none p-0 m-0">
        {surveys.map((survey) => (
          <li
            key={survey.slug}
            data-testid={`survey-${survey.slug}`}
            className="flex items-center justify-between rounded-lg border border-border px-5 py-4 transition-colors hover:bg-muted/30"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-foreground">{survey.title}</p>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs ${STATUS[survey.status].className}`}
                >
                  {STATUS[survey.status].label}
                </span>
              </div>
            </div>
            {/* A link, not a button: the runner is a separate application. */}
            <a
              href={survey.url}
              aria-label={`${ACTION[survey.status]} ${survey.title}`}
              className="ml-4 shrink-0 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              {ACTION[survey.status]}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
