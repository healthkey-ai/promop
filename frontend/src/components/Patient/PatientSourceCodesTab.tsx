import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../../api/axios";
import type {
  PatientSourceCode,
  SourceCodesResponse,
  ResolveResult,
} from "../../types/sourceCodes";

type Props = { personId: string };

const STATUS_BADGE: Record<string, { bg: string; text: string; label: string }> = {
  approved: { bg: "bg-green-100", text: "text-green-800", label: "Approved" },
  proposed: { bg: "bg-yellow-100", text: "text-yellow-800", label: "Proposed" },
  unmapped: { bg: "bg-red-100", text: "text-red-800", label: "Unmapped" },
};

const TABLE_LABELS: Record<string, string> = {
  measurement: "Measurement",
  observation: "Observation",
  condition: "Condition",
  drug_exposure: "Drug Exposure",
  procedure: "Procedure",
};

export default function PatientSourceCodesTab({ personId }: Props) {
  const navigate = useNavigate();
  const [data, setData] = useState<SourceCodesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [resolveResult, setResolveResult] = useState<ResolveResult | null>(null);
  const [filter, setFilter] = useState<"all" | "unmapped">("all");

  const fetchSourceCodes = useCallback(async () => {
    try {
      setLoading(true);
      const res = await api.get<SourceCodesResponse>(
        `/v1/patient-records/${personId}/source-codes/`
      );
      setData(res.data);
      setError(null);
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string; error?: string } } })
              .response?.data
          : undefined;
      setError(detail?.detail || detail?.error || "Failed to load source codes.");
    } finally {
      setLoading(false);
    }
  }, [personId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await fetchSourceCodes();
      if (cancelled) return;
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchSourceCodes]);

  const handleResolve = async () => {
    setResolving(true);
    setResolveResult(null);
    try {
      const res = await api.post<ResolveResult>(
        `/v1/patient-records/${personId}/resolve-source-codes/`,
        {}
      );
      setResolveResult(res.data);
      await fetchSourceCodes();
    } catch (err) {
      const detail =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { detail?: string; error?: string } } })
              .response?.data
          : undefined;
      setError(detail?.detail || detail?.error || "Failed to resolve source codes.");
    } finally {
      setResolving(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-4 p-6">
        <div className="h-6 w-64 animate-pulse rounded bg-muted" />
        <div className="h-48 animate-pulse rounded bg-muted" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-destructive">
        <p>{error}</p>
      </div>
    );
  }

  if (!data) return null;

  const { source_codes, summary } = data;
  const filtered =
    filter === "unmapped"
      ? source_codes.filter(
          (sc) => sc.mapping_status === "unmapped" || sc.concept_id === 0
        )
      : source_codes;

  return (
    <div className="space-y-4 p-6">
      {/* Summary banner */}
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">{summary.total}</span> source
          codes &mdash;{" "}
          <span className="text-red-600">{summary.unmapped} unmapped</span>,{" "}
          <span className="text-yellow-600">{summary.proposed} proposed</span>,{" "}
          <span className="text-green-600">{summary.approved} approved</span>
        </div>
        <button
          onClick={handleResolve}
          disabled={resolving}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {resolving ? (
            <>
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Resolving...
            </>
          ) : (
            "Generate OMOP"
          )}
        </button>
      </div>

      {/* Resolve result toast */}
      {resolveResult && (
        <div className="rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-800">
          Resolved {resolveResult.resolved} code(s). {resolveResult.skipped} skipped,{" "}
          {resolveResult.already_resolved} already resolved.
        </div>
      )}

      {/* Filter toggles */}
      <div className="flex gap-2">
        <button
          onClick={() => setFilter("all")}
          className={`rounded-md px-3 py-1 text-sm ${
            filter === "all"
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          }`}
        >
          All ({summary.total})
        </button>
        <button
          onClick={() => setFilter("unmapped")}
          className={`rounded-md px-3 py-1 text-sm ${
            filter === "unmapped"
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:bg-muted/80"
          }`}
        >
          Unmapped ({summary.unmapped})
        </button>
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No source codes{filter === "unmapped" ? " need mapping" : " found"}.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/50">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Source Code</th>
                <th className="px-3 py-2 text-left font-medium">Domain</th>
                <th className="px-3 py-2 text-right font-medium">Rows</th>
                <th className="px-3 py-2 text-left font-medium">Current Concept</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Status</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Target</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map((sc, i) => (
                <SourceCodeRow
                  key={`${sc.omop_table}-${sc.source_value}-${i}`}
                  sc={sc}
                  onClick={() => navigate(`/code-mappings?search=${encodeURIComponent(sc.source_value)}`)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SourceCodeRow({ sc, onClick }: { sc: PatientSourceCode; onClick: () => void }) {
  const badge = STATUS_BADGE[sc.mapping_status] || STATUS_BADGE.unmapped;

  return (
    <tr className="cursor-pointer hover:bg-muted/30" onClick={onClick}>
      <td className="px-3 py-2">
        <div className="font-mono text-xs">{sc.source_value}</div>
        {sc.source_vocabulary_id && (
          <div className="text-xs text-muted-foreground">{sc.source_vocabulary_id}</div>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {TABLE_LABELS[sc.omop_table] || sc.omop_table}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{sc.row_count}</td>
      <td className="px-3 py-2 text-xs">
        {sc.concept_id === 0 ? (
          <span className="text-muted-foreground">No matching concept</span>
        ) : (
          <span>
            {sc.concept_name || `Concept ${sc.concept_id}`}
            <span className="ml-1 text-muted-foreground">({sc.concept_id})</span>
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        <span
          className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${badge.bg} ${badge.text}`}
        >
          {badge.label}
        </span>
      </td>
      <td className="px-3 py-2 text-xs">
        {sc.mapping_target_concept_id ? (
          <span>
            {sc.mapping_target_concept_name || `Concept ${sc.mapping_target_concept_id}`}
            <span className="ml-1 text-muted-foreground">
              ({sc.mapping_target_concept_id})
            </span>
          </span>
        ) : (
          <span className="text-muted-foreground">&mdash;</span>
        )}
      </td>
    </tr>
  );
}
