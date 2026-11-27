import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import axios from "axios";
import api from "@/api/axios";

/** What depends on a created field, as `GET /v1/custom-patient-fields/<name>/` reports it. */
export interface FieldUsage {
  field_name: string;
  retired: boolean;
  records_with_value: number;
  /** Records holding an entry of their own (a pending edit); values read from patients' results do not count. */
  records_with_entered_value?: number;
  has_facts: boolean;
  answers: number;
  can_delete: boolean;
}

export type RemovalOutcome = "retired" | "deleted";

interface Props {
  fieldName: string;
  displayName?: string;
  onClose: () => void;
  /** The server's answer: the field was retired or deleted. */
  onDone: (outcome: RemovalOutcome, fieldName: string) => void;
}

const statusOf = (error: unknown): number | undefined =>
  axios.isAxiosError(error) ? error.response?.status : undefined;

const errorText = (error: unknown, fallback: string): string => {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
};

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/**
 * Confirms removing a created field (staff and org admins only; the page does not
 * offer it to anyone else). It reads how many records hold a value and says what
 * will happen: removing retires the field (hidden everywhere, every stored value
 * kept, restorable), and deleting for good is offered only when nothing depends on
 * it. The outcome is the server's: the dialog reports what it answered.
 */
export function RemoveFieldDialog({ fieldName, displayName, onClose, onDone }: Props) {
  const [usage, setUsage] = useState<FieldUsage | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const busyRef = useRef(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  const path = `/v1/custom-patient-fields/${encodeURIComponent(fieldName)}/`;

  const load = async () => {
    try {
      const response = await api.get(path);
      setUsage(response.data as FieldUsage);
      setLoadError("");
    } catch (failure) {
      setLoadError(errorText(failure, "Could not read what depends on this field."));
    }
  };

  useEffect(() => {
    (async () => {
      await load();
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldName]);

  // A modal takes focus when it opens and closes on Escape.
  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const remove = async (permanent: boolean) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const response = await api.delete(path, permanent ? { params: { permanent: "true" } } : undefined);
      const state = (response.data as { state?: string } | undefined)?.state;
      onDone(state === "deleted" ? "deleted" : "retired", fieldName);
    } catch (failure) {
      if (statusOf(failure) === 404) {
        // Another administrator deleted it meanwhile: nothing left to remove; refresh the page.
        onDone("deleted", fieldName);
        return;
      }
      setError(errorText(failure, "Could not remove the field."));
      // The counts may have changed since the dialog opened; show the current ones.
      await load();
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const label = displayName ? `${displayName} (${fieldName})` : fieldName;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-field-title"
        className="relative w-full max-w-lg rounded-lg border bg-white shadow-xl"
      >
        <div className="border-b border-slate-200 px-5 py-4">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="absolute right-3 top-3 rounded p-1 text-slate-400 hover:text-slate-600"
          >
            <X size={16} />
          </button>
          <h2 id="remove-field-title" className="text-lg font-semibold text-slate-900">
            {usage?.retired ? "Delete removed field" : "Remove field"}
          </h2>
          <p className="mt-1 break-all text-sm text-slate-500">{label}</p>
        </div>

        <div className="space-y-3 px-5 py-4 text-sm text-slate-700">
          {!usage && !loadError && <p role="status">Checking what depends on this field…</p>}
          {loadError && <p role="alert" className="text-red-700">{loadError}</p>}

          {usage && (
            <>
              <p>
                Fields are shared by every organization on this instance, so removing one removes it for all
                of them.
              </p>
              <p data-testid="usage-summary">
                {usage.records_with_value > 0
                  ? `${plural(usage.records_with_value, "record")} hold a value for this field.`
                  : "No record holds a value for this field."}
                {usage.has_facts && " OMOP data is written under it."}
                {usage.answers > 0 && ` It has ${plural(usage.answers, "answer")}.`}
              </p>

              {!usage.retired && (
                <p>
                  <strong>Remove</strong> hides the field from patient tabs and mappings. Every value already
                  stored is kept (it stays in the record data, no longer shown), and you can restore the field
                  later.
                </p>
              )}
              {usage.can_delete ? (
                <p>
                  Nothing depends on it, so it can also be <strong>deleted permanently</strong>: its mapping and
                  answers are deleted with it, and this cannot be undone.
                  {usage.records_with_value > 0 &&
                    " The values it shows are read from patients' own results; they stop showing on the records, and the results themselves are kept."}
                </p>
              ) : (
                <p>
                  It cannot be deleted permanently while a record holds an entry of its own for it or OMOP data is written under it.
                  {usage.retired && " Restore it to use it again."}
                </p>
              )}
            </>
          )}

          {error && <p role="alert" className="text-red-700">{error}</p>}
        </div>

        <div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 px-5 py-3">
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
          {usage && usage.can_delete && (
            <button
              type="button"
              disabled={busy}
              onClick={() => remove(true)}
              className="rounded border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              Delete permanently
            </button>
          )}
          {usage && !usage.retired && (
            <button
              type="button"
              disabled={busy}
              onClick={() => remove(false)}
              className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
            >
              Remove field
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
