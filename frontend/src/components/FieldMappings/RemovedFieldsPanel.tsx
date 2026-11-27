import { useEffect, useState } from "react";
import axios from "axios";
import api from "@/api/axios";
import { RemoveFieldDialog, type RemovalOutcome } from "./RemoveFieldDialog";

interface RemovedField {
  field_name: string;
  display_name: string;
  tab: string;
  field_type: string;
  retired_at: string | null;
}

interface Props {
  /** Bumped by the page when a field is removed, so the list is read again. */
  revision: number;
  /** A field was restored or deleted for good: the page reloads its mappings. */
  onChanged: (outcome: RemovalOutcome | "restored", fieldName: string) => void;
}

/**
 * The created fields an administrator removed, with Restore and, where nothing
 * depends on one, a permanent delete. Offered to staff and org admins only
 * (the page renders it for no one else; the server refuses anyone else too).
 */
export function RemovedFieldsPanel({ revision, onChanged }: Props) {
  const [fields, setFields] = useState<RemovedField[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<RemovedField | null>(null);

  const load = async () => {
    try {
      const response = await api.get("/v1/custom-patient-fields/", { params: { state: "retired" } });
      setFields(Array.isArray(response.data) ? response.data : []);
      setError("");
    } catch {
      setError("Could not read the removed fields.");
    }
  };

  useEffect(() => {
    (async () => {
      await load();
    })();
  }, [revision]);

  const restore = async (field: RemovedField) => {
    setBusy(field.field_name);
    setError("");
    try {
      await api.post(`/v1/custom-patient-fields/${encodeURIComponent(field.field_name)}/restore/`);
      onChanged("restored", field.field_name);
      await load();
    } catch (failure) {
      const detail = axios.isAxiosError(failure) ? (failure.response?.data as { detail?: unknown })?.detail : null;
      setError(typeof detail === "string" && detail ? detail : "Could not restore the field.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-label="Removed fields" className="mb-4 rounded border border-gray-200 bg-gray-50 p-3 text-sm">
      <h2 className="mb-2 font-medium text-gray-800">Removed fields</h2>
      <p className="mb-2 text-xs text-gray-500">
        A removed field is hidden everywhere and its stored values are kept. Restoring it brings back its
        mapping, answers and scope.
      </p>
      {error && <p role="alert" className="mb-2 text-red-700">{error}</p>}
      {fields && fields.length === 0 && <p className="text-gray-500">No removed fields.</p>}
      {fields && fields.length > 0 && (
        <ul className="divide-y divide-gray-200">
          {fields.map((field) => (
            <li key={field.field_name} className="flex items-center gap-3 py-1.5">
              <span className="min-w-0 flex-1">
                <span className="font-medium text-gray-800">{field.display_name}</span>{" "}
                <span className="font-mono text-xs text-gray-500">{field.field_name}</span>
                {field.retired_at && (
                  <span className="ml-2 text-xs text-gray-400">
                    removed {new Date(field.retired_at).toLocaleDateString()}
                  </span>
                )}
              </span>
              <button
                type="button"
                disabled={busy === field.field_name}
                onClick={() => restore(field)}
                className="rounded border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
              >
                Restore
              </button>
              <button
                type="button"
                onClick={() => setDeleting(field)}
                className="rounded border border-red-200 bg-white px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
              >
                Delete…
              </button>
            </li>
          ))}
        </ul>
      )}
      {deleting && (
        <RemoveFieldDialog
          fieldName={deleting.field_name}
          displayName={deleting.display_name}
          onClose={() => setDeleting(null)}
          onDone={(outcome, name) => {
            setDeleting(null);
            onChanged(outcome, name);
            (async () => {
              await load();
            })();
          }}
        />
      )}
    </section>
  );
}
