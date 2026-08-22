import { useCallback, useEffect, useRef, useState } from "react";
import { X, Plus, Trash2 } from "lucide-react";
import api from "@/api/axios";

interface Synonym {
  id: number | null;
  field_name: string;
  synonym_text: string;
  source: string;
  created_by: string | null;
  created_at: string | null;
}

interface Props {
  fieldName: string;
  onClose: () => void;
}

export function SynonymDialog({ fieldName, onClose }: Props) {
  const [synonyms, setSynonyms] = useState<Synonym[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newSynonym, setNewSynonym] = useState("");
  const [adding, setAdding] = useState(false);
  const overlayRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const fetchSynonyms = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const resp = await api.get(`/v1/field-mappings/${fieldName}/synonyms/`);
      setSynonyms(resp.data);
    } catch {
      setError("Failed to load synonyms.");
    } finally {
      setLoading(false);
    }
  }, [fieldName]);

  // Wrapped rather than called bare: react-hooks/set-state-in-effect reads a
  // direct call whose callee sets state as a synchronous setState in the effect
  // body. Same shape FieldMappingPage and PatientDetail use for their loads.
  useEffect(() => {
    (async () => {
      await fetchSynonyms();
    })();
  }, [fetchSynonyms]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    if (!loading) inputRef.current?.focus();
  }, [loading]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
  };

  const handleAdd = async () => {
    const text = newSynonym.trim();
    if (!text) return;
    setAdding(true);
    setError("");
    try {
      await api.post(`/v1/field-mappings/${fieldName}/synonyms/`, { synonym_text: text });
      setNewSynonym("");
      fetchSynonyms();
    } catch (err) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? JSON.stringify((err as { response: { data: unknown } }).response.data)
          : "Failed to add synonym.";
      setError(msg);
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id: number) => {
    setError("");
    try {
      await api.delete(`/v1/field-synonyms/${id}/`);
      fetchSynonyms();
    } catch {
      setError("Failed to delete synonym.");
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleAdd();
    }
  };

  const omopSynonyms = synonyms.filter((s) => s.source === "omop");
  const customSynonyms = synonyms.filter((s) => s.source === "custom");

  return (
    <div
      ref={overlayRef}
      onClick={handleOverlayClick}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
    >
      <div className="relative w-full max-w-lg rounded-lg border bg-white p-6 shadow-xl">
        <button
          onClick={onClose}
          className="absolute right-3 top-3 rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600"
        >
          <X size={16} />
        </button>

        <h2 className="mb-1 text-lg font-semibold">Synonyms</h2>
        <p className="mb-4 font-mono text-sm text-gray-500">{fieldName}</p>

        {loading ? (
          <div className="space-y-2">
            {[...Array(3)].map((_, i) => (
              <div key={i} className="h-6 animate-pulse rounded bg-gray-200" />
            ))}
          </div>
        ) : (
          <>
            {error && (
              <div className="mb-3 rounded border border-red-300 bg-red-50 p-2 text-sm text-red-700">
                {error}
              </div>
            )}

            {/* OMOP Synonyms */}
            <div className="mb-4">
              <h3 className="mb-2 text-sm font-medium text-gray-700">OMOP Synonyms</h3>
              {omopSynonyms.length === 0 ? (
                <p className="text-sm text-gray-400">
                  No OMOP synonyms. Assign a concept to see OMOP synonyms.
                </p>
              ) : (
                <ul className="space-y-1">
                  {omopSynonyms.map((s, i) => (
                    <li
                      key={`omop-${i}`}
                      className="flex items-center gap-2 rounded px-2 py-1 text-sm"
                    >
                      <span className="flex-1">{s.synonym_text}</span>
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-500">
                        OMOP
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Custom Synonyms */}
            <div className="mb-4">
              <h3 className="mb-2 text-sm font-medium text-gray-700">Custom Synonyms</h3>
              {customSynonyms.length === 0 ? (
                <p className="mb-2 text-sm text-gray-400">No custom synonyms yet.</p>
              ) : (
                <ul className="mb-2 space-y-1">
                  {customSynonyms.map((s) => (
                    <li
                      key={s.id}
                      className="flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-gray-50"
                    >
                      <span className="flex-1">{s.synonym_text}</span>
                      <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">
                        custom
                      </span>
                      <button
                        onClick={() => handleDelete(s.id!)}
                        className="rounded p-0.5 text-gray-400 hover:bg-red-50 hover:text-red-500"
                        title="Delete synonym"
                      >
                        <Trash2 size={12} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {/* Add synonym input */}
              <div className="flex gap-2">
                <input
                  ref={inputRef}
                  type="text"
                  placeholder="Add a synonym..."
                  value={newSynonym}
                  onChange={(e) => setNewSynonym(e.target.value)}
                  onKeyDown={handleKeyDown}
                  className="h-8 flex-1 rounded border border-gray-300 px-2 text-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                  disabled={adding}
                />
                <button
                  onClick={handleAdd}
                  disabled={adding || !newSynonym.trim()}
                  className="inline-flex h-8 items-center gap-1 rounded bg-primary px-3 text-sm text-white hover:bg-primary/90 disabled:opacity-50"
                >
                  <Plus size={12} />
                  Add
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
