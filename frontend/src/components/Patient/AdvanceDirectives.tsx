import { useState, useEffect, useCallback, useRef } from "react";
import { AlertCircle, FileText, Upload } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";
import { formatDate } from "@/utils/date";

interface Document {
  id: number;
  doc_type: string;
  title: string | null;
  file_url: string | null;
  file_name: string | null;
  verified: boolean;
  uploaded_at: string;
}

export default function AdvanceDirectives({ user }: { user: User | null }) {
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchDocuments = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/documents/", {
        params: { doc_type: "ADVANCE_DIRECTIVE", person_id: user!.person_id },
      });
      setDocuments(Array.isArray(res.data) ? res.data : res.data.results ?? []);
    } catch {
      setError("Failed to load advance directives.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (user && user.person_id != null) {
        await fetchDocuments();
      } else {
        setLoading(false);
      }
    })();
  }, [user, fetchDocuments]);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !user?.person_id) return;
    setUploading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append("person", String(user.person_id));
      formData.append("doc_type", "ADVANCE_DIRECTIVE");
      formData.append("title", file.name);
      formData.append("file_name", file.name);
      formData.append("file", file);
      await api.post("/v1/documents/", formData);
      await fetchDocuments();
    } catch {
      setError("Failed to upload advance directive.");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  if (!user || user.person_id == null) return null;

  if (loading) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-40 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-3">
          {[1, 2].map((i) => (
            <div key={i} className="h-12 animate-pulse rounded-lg bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <FileText className="h-5 w-5 text-portal-brand" />
          <h2 className="text-xl font-bold text-foreground">Advance Directives</h2>
        </div>
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50">
          <Upload className="h-4 w-4" />
          {uploading ? "Uploading..." : "Upload"}
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".pdf,.jpg,.jpeg,.png,.doc,.docx"
            onChange={handleUpload}
            disabled={uploading}
          />
        </label>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Living wills, healthcare proxies, and other advance directive documents.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {documents.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          No advance directives on file. Use the upload button to add one.
        </p>
      )}

      <div className="space-y-3">
        {documents.map((doc) => (
          <div
            key={doc.id}
            className="flex items-center justify-between rounded-lg border border-border px-5 py-4 transition-colors hover:bg-muted/30"
          >
            <div>
              <p className="text-sm font-medium text-foreground">
                {doc.title || doc.file_name || "Advance Directive"}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Uploaded {formatDate(doc.uploaded_at)}
              </p>
            </div>
            {doc.verified && (
              <span className="rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-700">
                Verified
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
