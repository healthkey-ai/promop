import { useState, useCallback, useRef } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, ChevronLeft } from "lucide-react";
import type { Survey, SurveyResponse } from "@/components/Patient/PatientSurveys";

interface SurveyFormProps {
  survey: Survey;
  response: SurveyResponse;
  onSave: (values: Record<string, unknown>, percentComplete: number) => Promise<void>;
  onComplete: () => void;
  onBack: () => void;
  readOnly?: boolean;
}

function computePercentComplete(
  values: Record<string, unknown>,
  survey: Survey
): number {
  const allInputs = survey.pages.flatMap((p) => p.inputs);
  if (allInputs.length === 0) return 100;
  const filled = allInputs.filter((input) => {
    const val = values[input.name];
    return val !== undefined && val !== null && val !== "";
  }).length;
  return Math.round((filled / allInputs.length) * 100);
}

export default function SurveyForm({
  survey,
  response,
  onSave,
  onComplete,
  onBack,
  readOnly = false,
}: SurveyFormProps) {
  const [currentPage, setCurrentPage] = useState(0);
  const [values, setValues] = useState<Record<string, unknown>>(
    response.values ?? {}
  );
  const [saving, setSaving] = useState(false);
  const [completing, setCompleting] = useState(false);
  const valuesRef = useRef(values);
  valuesRef.current = values;

  const pages = survey.pages ?? [];
  const page = pages[currentPage];
  const isLastPage = currentPage === pages.length - 1;
  const isFirstPage = currentPage === 0;

  const handleFieldChange = useCallback(
    (name: string, value: unknown) => {
      setValues((prev) => {
        const next = { ...prev, [name]: value };
        valuesRef.current = next;
        return next;
      });
    },
    []
  );

  const doSave = useCallback(async () => {
    if (readOnly) return;
    setSaving(true);
    try {
      const pct = computePercentComplete(valuesRef.current, survey);
      await onSave(valuesRef.current, pct);
    } finally {
      setSaving(false);
    }
  }, [readOnly, onSave, survey]);

  const handlePrev = () => {
    if (isFirstPage) return;
    doSave();
    setCurrentPage((p) => p - 1);
  };

  const handleNext = () => {
    if (isLastPage) return;
    doSave();
    setCurrentPage((p) => p + 1);
  };

  const handleComplete = async () => {
    if (readOnly) return;
    setCompleting(true);
    try {
      const pct = computePercentComplete(valuesRef.current, survey);
      await onSave(valuesRef.current, pct);
      await onComplete();
    } finally {
      setCompleting(false);
    }
  };

  if (!page) {
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <p className="text-sm text-muted-foreground">
          This survey has no pages.
        </p>
        <button
          onClick={onBack}
          className="mt-4 text-sm font-medium text-portal-brand hover:underline"
        >
          Back to surveys
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      {/* Header */}
      <div className="mb-6">
        <button
          onClick={onBack}
          className="mb-3 inline-flex items-center gap-1 text-sm font-medium text-portal-brand hover:underline"
        >
          <ChevronLeft className="h-4 w-4" />
          Back to surveys
        </button>
        <h2 className="text-xl font-bold text-foreground">{survey.title}</h2>
        {readOnly && (
          <p className="mt-1 text-xs font-medium text-green-600">
            Completed — read only
          </p>
        )}
        {/* Progress bar */}
        <div className="mt-3 flex items-center gap-3">
          <div className="h-2 flex-1 rounded-full bg-muted">
            <div
              className="h-2 rounded-full bg-primary transition-all"
              style={{
                width: `${computePercentComplete(values, survey)}%`,
              }}
            />
          </div>
          <span className="text-xs text-muted-foreground">
            {computePercentComplete(values, survey)}%
          </span>
        </div>
      </div>

      {/* Page indicator */}
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-foreground">{page.title}</h3>
        <span className="text-xs text-muted-foreground">
          Page {currentPage + 1} of {pages.length}
        </span>
      </div>

      {/* Inputs */}
      <div className="space-y-5">
        {page.inputs.map((input) => (
          <div key={input.name}>
            <label
              htmlFor={input.name}
              className="mb-1.5 block text-sm font-medium text-foreground"
            >
              {input.label}
            </label>
            {renderInput(input, values[input.name], handleFieldChange, readOnly)}
          </div>
        ))}
      </div>

      {/* Navigation */}
      <div className="mt-8 flex items-center justify-between">
        <button
          onClick={handlePrev}
          disabled={isFirstPage || saving}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground hover:bg-muted/50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ArrowLeft className="h-4 w-4" />
          Previous
        </button>

        <div className="flex items-center gap-2">
          {saving && (
            <span className="text-xs text-muted-foreground">Saving...</span>
          )}

          {isLastPage && !readOnly ? (
            <button
              onClick={handleComplete}
              disabled={completing}
              className="inline-flex items-center gap-1.5 rounded-md bg-green-600 px-4 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <CheckCircle2 className="h-4 w-4" />
              {completing ? "Completing..." : "Complete Survey"}
            </button>
          ) : (
            <button
              onClick={handleNext}
              disabled={isLastPage || saving}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Next
              <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Input renderers                                                    */
/* ------------------------------------------------------------------ */

function renderInput(
  input: { name: string; type: string; data?: { maxRating?: number; options?: string[] } },
  value: unknown,
  onChange: (name: string, value: unknown) => void,
  readOnly: boolean
) {
  switch (input.type) {
    case "rating":
      return renderRating(input, value, onChange, readOnly);
    case "textarea":
      return renderTextarea(input, value, onChange, readOnly);
    case "select":
      return renderSelect(input, value, onChange, readOnly);
    case "text":
    default:
      return renderText(input, value, onChange, readOnly);
  }
}

function renderRating(
  input: { name: string; data?: { maxRating?: number } },
  value: unknown,
  onChange: (name: string, value: unknown) => void,
  readOnly: boolean
) {
  const max = input.data?.maxRating ?? 5;
  const selected = typeof value === "number" ? value : null;

  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={input.name}>
      {Array.from({ length: max }, (_, i) => i + 1).map((n) => (
        <button
          key={n}
          type="button"
          disabled={readOnly}
          onClick={() => onChange(input.name, n)}
          aria-label={`${n}`}
          className={`flex h-9 w-9 items-center justify-center rounded-md border text-sm font-medium transition-colors ${
            selected === n
              ? "border-primary bg-primary text-primary-foreground"
              : "border-border bg-background text-foreground hover:bg-muted/50"
          } disabled:cursor-not-allowed disabled:opacity-70`}
        >
          {n}
        </button>
      ))}
    </div>
  );
}

function renderTextarea(
  input: { name: string },
  value: unknown,
  onChange: (name: string, value: unknown) => void,
  readOnly: boolean
) {
  return (
    <textarea
      id={input.name}
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(input.name, e.target.value)}
      disabled={readOnly}
      rows={4}
      className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-70"
    />
  );
}

function renderSelect(
  input: { name: string; data?: { options?: string[] } },
  value: unknown,
  onChange: (name: string, value: unknown) => void,
  readOnly: boolean
) {
  const options = input.data?.options ?? [];

  return (
    <select
      id={input.name}
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(input.name, e.target.value)}
      disabled={readOnly}
      className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-70"
    >
      <option value="">Select...</option>
      {options.map((opt) => (
        <option key={opt} value={opt}>
          {opt}
        </option>
      ))}
    </select>
  );
}

function renderText(
  input: { name: string },
  value: unknown,
  onChange: (name: string, value: unknown) => void,
  readOnly: boolean
) {
  return (
    <input
      id={input.name}
      type="text"
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(input.name, e.target.value)}
      disabled={readOnly}
      className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-70"
    />
  );
}
