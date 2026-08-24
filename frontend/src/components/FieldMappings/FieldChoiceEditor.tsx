import { useCallback, useEffect, useState } from "react";
import { X, Plus, Trash2, ChevronUp, ChevronDown } from "lucide-react";
import api from "@/api/axios";

interface ChoiceCode {
  id?: number;
  code: string;
  vocabulary_id: string;
  display: string;
  is_primary: boolean;
}

interface Choice {
  id?: number;
  field_name: string;
  display: string;
  sort_order: number;
  codes: ChoiceCode[];
}

interface Props {
  fieldName: string;
  onClose: () => void;
}

export function FieldChoiceEditor({ fieldName, onClose }: Props) {
  const [choices, setChoices] = useState<Choice[]>([]);
  const [loading, setLoading] = useState(true);
  const [newDisplay, setNewDisplay] = useState("");
  const [saving, setSaving] = useState(false);
  const [reordering, setReordering] = useState(false);
  const [error, setError] = useState("");

  const fetchChoices = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await api.get(`/v1/field-choices/?field_name=${encodeURIComponent(fieldName)}`);
      setChoices(resp.data);
    } catch {
      setError("Failed to load choices.");
    } finally {
      setLoading(false);
    }
  }, [fieldName]);

  useEffect(() => {
    (async () => {
      await fetchChoices();
    })();
  }, [fetchChoices]);

  const handleAddChoice = async () => {
    if (!newDisplay.trim()) return;
    setSaving(true);
    setError("");
    try {
      await api.post("/v1/field-choices/", {
        field_name: fieldName,
        display: newDisplay.trim(),
        sort_order: choices.length,
      });
      setNewDisplay("");
      await fetchChoices();
    } catch {
      setError("Failed to add choice.");
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteChoice = async (choiceId: number) => {
    if (!window.confirm("Delete this choice?")) return;
    try {
      await api.delete(`/v1/field-choices/${choiceId}/`);
      await fetchChoices();
    } catch {
      setError("Failed to delete choice.");
    }
  };

  const handleMoveChoice = async (index: number, direction: "up" | "down") => {
    if (reordering) return;
    const newIndex = direction === "up" ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= choices.length) return;
    const updated = [...choices];
    [updated[index], updated[newIndex]] = [updated[newIndex], updated[index]];
    // Update sort_order for both
    setReordering(true);
    try {
      await Promise.all([
        api.patch(`/v1/field-choices/${updated[index].id}/`, { sort_order: index }),
        api.patch(`/v1/field-choices/${updated[newIndex].id}/`, { sort_order: newIndex }),
      ]);
      await fetchChoices();
    } catch {
      setError("Failed to reorder.");
    } finally {
      setReordering(false);
    }
  };

  const handleAddCode = async (choiceId: number) => {
    const code = window.prompt("Enter code (e.g. SNOMED code):");
    if (!code) return;
    const vocab = window.prompt("Vocabulary (SNOMED, ICD10CM, etc.):", "SNOMED");
    if (!vocab) return;
    try {
      await api.post(`/v1/field-choices/${choiceId}/codes/`, {
        code,
        vocabulary_id: vocab,
        is_primary: true,
      });
      await fetchChoices();
    } catch {
      setError("Failed to add code.");
    }
  };

  // Close on Escape
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
      <div className="relative w-full max-w-lg rounded-lg border bg-white p-6 shadow-xl max-h-[80vh] overflow-y-auto">
        <button
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:text-gray-600"
        >
          <X size={16} />
        </button>

        <h2 className="mb-1 text-lg font-semibold">Field Choices</h2>
        <p className="mb-4 text-sm text-gray-500">
          Manage allowed values for <span className="font-mono">{fieldName}</span>
        </p>

        {error && <div className="mb-3 text-sm text-red-600">{error}</div>}

        {loading ? (
          <div className="py-4 text-center text-sm text-gray-400">Loading...</div>
        ) : (
          <>
            {/* Existing choices */}
            <div className="mb-4 space-y-2">
              {choices.map((choice, index) => (
                <div
                  key={choice.id}
                  className="flex items-start gap-2 rounded border border-gray-200 p-2"
                >
                  <div className="flex flex-col gap-0.5">
                    <button
                      onClick={() => handleMoveChoice(index, "up")}
                      disabled={index === 0}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-30"
                    >
                      <ChevronUp size={12} />
                    </button>
                    <button
                      onClick={() => handleMoveChoice(index, "down")}
                      disabled={index === choices.length - 1}
                      className="text-gray-400 hover:text-gray-600 disabled:opacity-30"
                    >
                      <ChevronDown size={12} />
                    </button>
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-medium">{choice.display}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {choice.codes.map((c) => (
                        <span
                          key={`${c.vocabulary_id}-${c.code}`}
                          className={`rounded px-1.5 py-0.5 text-[10px] ${
                            c.is_primary
                              ? "bg-blue-100 text-blue-700"
                              : "bg-gray-100 text-gray-600"
                          }`}
                        >
                          {c.vocabulary_id}:{c.code}
                        </span>
                      ))}
                      <button
                        onClick={() => choice.id && handleAddCode(choice.id)}
                        className="rounded px-1.5 py-0.5 text-[10px] text-gray-400 hover:bg-gray-100 hover:text-gray-600"
                      >
                        <Plus size={10} className="inline" /> code
                      </button>
                    </div>
                  </div>
                  <button
                    onClick={() => choice.id && handleDeleteChoice(choice.id)}
                    className="rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-500"
                    title="Delete choice"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
              {choices.length === 0 && (
                <div className="py-3 text-center text-sm text-gray-400">
                  No choices defined yet.
                </div>
              )}
            </div>

            {/* Add new choice */}
            <div className="flex gap-2">
              <input
                type="text"
                value={newDisplay}
                onChange={(e) => setNewDisplay(e.target.value)}
                placeholder="New choice display name..."
                className="h-8 flex-1 rounded border border-gray-300 px-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                onKeyDown={(e) => { if (e.key === "Enter") handleAddChoice(); }}
              />
              <button
                onClick={handleAddChoice}
                disabled={!newDisplay.trim() || saving}
                className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {saving ? "Adding..." : "Add"}
              </button>
            </div>
          </>
        )}

        <div className="mt-4 flex justify-end">
          <button
            onClick={onClose}
            className="rounded border border-gray-300 px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
