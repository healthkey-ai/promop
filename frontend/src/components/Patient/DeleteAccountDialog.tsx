import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import api from "@/api/axios";

interface DeleteAccountDialogProps {
  onClose: () => void;
  onDeleted: () => void;
}

export default function DeleteAccountDialog({ onClose, onDeleted }: DeleteAccountDialogProps) {
  const [confirmText, setConfirmText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canConfirm = confirmText === "DELETE";

  async function handleDelete() {
    if (!canConfirm || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.delete("/api/v1/patient-records/me/", { data: { confirm: "DELETE" } });
      onDeleted();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? "Failed to delete account. Please try again.";
      setError(detail);
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="mx-4 w-full max-w-md rounded-2xl bg-background p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-red-100">
            <AlertTriangle className="h-5 w-5 text-red-600" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">Delete your account</h3>
        </div>

        <p className="mb-4 text-sm text-muted-foreground">
          This will <strong>permanently delete</strong> your account and all associated health data.
          This action cannot be undone.
        </p>

        <label className="mb-1 block text-sm font-medium text-foreground" htmlFor="delete-confirm">
          Type <span className="font-mono font-bold">DELETE</span> to confirm
        </label>
        <input
          id="delete-confirm"
          type="text"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          className="mb-4 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-red-400 focus:ring-1 focus:ring-red-400"
          autoComplete="off"
          autoFocus
        />

        {error && <p className="mb-3 text-sm text-red-600">{error}</p>}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-muted"
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            onClick={handleDelete}
            disabled={!canConfirm || submitting}
            className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            {submitting ? "Deleting..." : "Delete my account"}
          </button>
        </div>
      </div>
    </div>
  );
}
