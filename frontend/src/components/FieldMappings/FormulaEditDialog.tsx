import { useEffect, useState } from "react";
import { X } from "lucide-react";
import api from "@/api/axios";

interface Props {
  fieldName: string;
  fieldType: string;
  existingFormula: { id: number; expression: string; is_active: boolean } | null;
  onClose: () => void;
}

export function FormulaEditDialog({ fieldName, fieldType, existingFormula, onClose }: Props) {
  const [expression, setExpression] = useState(existingFormula?.expression || "");
  const [isActive, setIsActive] = useState(existingFormula?.is_active ?? false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const isEditing = !!existingFormula;

  const handleSubmit = async () => {
    if (!expression.trim()) return;
    setSaving(true);
    setError("");
    try {
      const payload = {
        field_name: fieldName,
        formula: expression.trim(),
        is_active: isActive,
      };
      if (isEditing) {
        await api.patch(`/v1/field-formulas/${existingFormula.id}/`, payload);
      } else {
        await api.post("/v1/field-formulas/", payload);
      }
      onClose();
    } catch (err: unknown) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? JSON.stringify((err as { response: { data: unknown } }).response.data)
          : "Failed to save formula.";
      setError(msg);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!existingFormula || !window.confirm("Delete this formula?")) return;
    try {
      await api.delete(`/v1/field-formulas/${existingFormula.id}/`);
      onClose();
    } catch {
      setError("Failed to delete formula.");
    }
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div className="relative w-full max-w-lg rounded-lg border bg-white p-6 shadow-xl">
        <button
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:text-gray-600"
        >
          <X size={16} />
        </button>

        <h2 className="mb-1 text-lg font-semibold">
          {isEditing ? "Edit Formula" : "Add Formula"}
        </h2>
        <p className="mb-4 text-sm text-gray-500">
          Field: <span className="font-mono">{fieldName}</span> ({fieldType})
        </p>

        {/* Formula input */}
        <div className="mb-3">
          <label className="mb-1 block text-xs font-medium text-gray-600">Expression</label>
          <textarea
            value={expression}
            onChange={(e) => setExpression(e.target.value)}
            rows={3}
            className="w-full rounded border border-gray-300 px-3 py-2 font-mono text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            placeholder='e.g. @not(active_infection_status) or weight / (height / 100) ^ 2'
          />
        </div>

        {/* Syntax reference */}
        <div className="mb-4 rounded bg-gray-50 px-3 py-2 text-xs text-gray-500">
          <div className="mb-1 font-medium text-gray-600">Supported syntax:</div>
          <div className="space-y-0.5">
            <div><code>@not(field)</code> — boolean negation</div>
            <div><code>@count(field)</code> — count items (JSON lists)</div>
            <div><code>+</code> <code>-</code> <code>*</code> <code>/</code> <code>^</code> — arithmetic</div>
            <div><code>==</code> <code>!=</code> <code>&lt;</code> <code>&lt;=</code> <code>&gt;</code> <code>&gt;=</code> — comparisons</div>
            <div>Field names reference other PatientRecord fields</div>
          </div>
        </div>

        {/* Active toggle */}
        <div className="mb-4 flex items-center gap-2">
          <input
            type="checkbox"
            id="formula-active"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300"
          />
          <label htmlFor="formula-active" className="text-sm text-gray-600">
            Active (formula drives derivation)
          </label>
        </div>

        {error && <div className="mb-3 text-sm text-red-600">{error}</div>}

        {/* Footer */}
        <div className="flex justify-between">
          <div>
            {isEditing && (
              <button
                onClick={handleDelete}
                className="rounded border border-red-300 px-3 py-2 text-sm text-red-600 hover:bg-red-50"
              >
                Delete
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={!expression.trim() || saving}
              className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? "Saving..." : isEditing ? "Update" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
