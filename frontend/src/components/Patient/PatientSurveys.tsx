import { useState, useEffect, useCallback } from "react";
import { AlertCircle, ClipboardList } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";
import SurveyForm from "@/components/Patient/SurveyForm";

interface SurveyInput {
  name: string;
  label: string;
  type: string;
  data?: {
    maxRating?: number;
    options?: string[];
  };
}

interface SurveyPage {
  name: string;
  title: string;
  inputs: SurveyInput[];
}

export interface Survey {
  id: number;
  name: string;
  title: string;
  description: string;
  status: string;
  disease: string;
  pages: SurveyPage[];
  estimated_minutes: number | null;
}

export interface SurveyResponse {
  id: number;
  person: number;
  survey: number;
  survey_title: string;
  survey_name: string;
  values: Record<string, unknown>;
  values_dates: Record<string, string>;
  percent_complete: number;
  started_at: string | null;
  completed_at: string | null;
  consent_date: string | null;
  consent_signature: string | null;
  created_at: string;
  updated_at: string;
}

function statusBadge(response: SurveyResponse | undefined) {
  if (!response) {
    return (
      <span className="inline-flex items-center rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-600">
        Not started
      </span>
    );
  }
  if (response.completed_at) {
    return (
      <span className="inline-flex items-center rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-700">
        Completed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-medium text-blue-700">
      In progress ({response.percent_complete}%)
    </span>
  );
}

function actionLabel(response: SurveyResponse | undefined): string {
  if (!response) return "Start";
  if (response.completed_at) return "View";
  return "Continue";
}

export default function PatientSurveys({ user }: { user: User | null }) {
  const [surveys, setSurveys] = useState<Survey[]>([]);
  const [responses, setResponses] = useState<SurveyResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeSurvey, setActiveSurvey] = useState<Survey | null>(null);
  const [activeResponse, setActiveResponse] = useState<SurveyResponse | null>(null);
  const [starting, setStarting] = useState<number | null>(null);

  const fetchData = useCallback(async () => {
    if (!user || user.person_id == null) return;
    setLoading(true);
    setError(null);
    try {
      const [surveysRes, responsesRes] = await Promise.all([
        api.get("/v1/surveys/?status=ACTIVE"),
        api.get(`/v1/survey-responses/?person_id=${user.person_id}`),
      ]);
      setSurveys(
        Array.isArray(surveysRes.data) ? surveysRes.data : surveysRes.data.results ?? []
      );
      setResponses(
        Array.isArray(responsesRes.data) ? responsesRes.data : responsesRes.data.results ?? []
      );
    } catch {
      setError("Failed to load surveys. Please try again.");
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    // No synchronous setState in the effect body (react-hooks/set-state-in-effect)
    // — both branches live in the async IIFE, matching the #269 pattern.
    (async () => {
      if (user && user.person_id != null) {
        await fetchData();
      } else {
        setLoading(false);
      }
    })();
  }, [user, fetchData]);

  const getResponseForSurvey = (surveyId: number): SurveyResponse | undefined =>
    responses.find((r) => r.survey === surveyId);

  const handleStart = async (survey: Survey) => {
    if (!user || user.person_id == null) return;
    setStarting(survey.id);
    setError(null);
    try {
      const res = await api.post("/v1/survey-responses/", {
        person: user.person_id,
        survey: survey.id,
        started_at: new Date().toISOString(),
      });
      const newResponse: SurveyResponse = res.data;
      setResponses((prev) => [...prev, newResponse]);
      setActiveSurvey(survey);
      setActiveResponse(newResponse);
    } catch {
      setError("Failed to start survey. Please try again.");
    } finally {
      setStarting(null);
    }
  };

  const handleAction = (survey: Survey) => {
    const response = getResponseForSurvey(survey.id);
    if (!response) {
      handleStart(survey);
      return;
    }
    setActiveSurvey(survey);
    setActiveResponse(response);
  };

  const handleSave = async (values: Record<string, unknown>, percentComplete: number) => {
    if (!activeResponse) return;
    const res = await api.patch(`/v1/survey-responses/${activeResponse.id}/`, {
      values,
      percent_complete: percentComplete,
    });
    const updated: SurveyResponse = res.data;
    setActiveResponse(updated);
    setResponses((prev) =>
      prev.map((r) => (r.id === updated.id ? updated : r))
    );
  };

  const handleComplete = async () => {
    if (!activeResponse) return;
    try {
      const res = await api.patch(`/v1/survey-responses/${activeResponse.id}/`, {
        completed_at: new Date().toISOString(),
        percent_complete: 100,
      });
      const updated: SurveyResponse = res.data;
      setActiveResponse(updated);
      setResponses((prev) =>
        prev.map((r) => (r.id === updated.id ? updated : r))
      );
      setActiveSurvey(null);
      setActiveResponse(null);
    } catch {
      setError("Failed to complete survey. Please try again.");
    }
  };

  const handleBack = () => {
    setActiveSurvey(null);
    setActiveResponse(null);
  };

  if (!user || user.person_id == null) {
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
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
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="flex items-center justify-between rounded-lg border border-border p-4"
            >
              <div className="space-y-1.5">
                <div className="h-4 w-48 animate-pulse rounded bg-muted" />
                <div className="h-3 w-64 animate-pulse rounded bg-muted" />
                <div className="h-3 w-20 animate-pulse rounded bg-muted" />
              </div>
              <div className="h-8 w-20 animate-pulse rounded bg-muted" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (activeSurvey && activeResponse) {
    return (
      <SurveyForm
        survey={activeSurvey}
        response={activeResponse}
        onSave={handleSave}
        onComplete={handleComplete}
        onBack={handleBack}
        readOnly={!!activeResponse.completed_at}
      />
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center gap-2 mb-1">
        <ClipboardList className="h-5 w-5 text-portal-brand" />
        <h2 className="text-xl font-bold text-foreground">Surveys</h2>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Complete surveys to help your care team understand your health better.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {surveys.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">No surveys available at this time.</p>
      )}

      <div className="space-y-3">
        {surveys.map((survey) => {
          const response = getResponseForSurvey(survey.id);
          const label = actionLabel(response);
          const isStarting = starting === survey.id;

          return (
            <div
              key={survey.id}
              className="flex items-center justify-between rounded-lg border border-border px-5 py-4 transition-colors hover:bg-muted/30"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-foreground">
                    {survey.title}
                  </p>
                  {statusBadge(response)}
                </div>
                {survey.description && (
                  <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
                    {survey.description}
                  </p>
                )}
                {survey.estimated_minutes && (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    ~{survey.estimated_minutes} min
                  </p>
                )}
              </div>
              <button
                onClick={() => handleAction(survey)}
                disabled={isStarting}
                className="ml-4 shrink-0 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isStarting ? "Starting..." : label}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
