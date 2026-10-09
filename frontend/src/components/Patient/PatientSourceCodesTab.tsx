import { useCallback, useEffect, useState } from "react";
import api from "../../api/axios";
import { useAuth } from "../../hooks/useAuth";
import type {
  PatientSourceCode,
  SourceCodesResponse,
  ResolveResult,
} from "../../types/sourceCodes";
import EditMappingDialog from "../CodeMappings/EditMappingDialog";
import {
  type CodeMappingRow,
  type Reference,
  type RepointResult,
  emptyReference,
} from "../CodeMappings/codeMappingTypes";

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

const TABLE_TO_DOMAIN: Record<string, string> = {
  measurement: "Measurement",
  observation: "Observation",
  condition: "Condition",
  drug_exposure: "Drug",
  procedure: "Procedure",
};

/** Build a CodeMappingRow stub from a PatientSourceCode for the edit dialog. */
function sourceCodeToRow(sc: PatientSourceCode): CodeMappingRow {
  const domain = TABLE_TO_DOMAIN[sc.omop_table] || "";
  return {
    mapping_id: sc.mapping_id,
    domain_id: domain,
    source_vocabulary_id: sc.source_vocabulary_id || "",
    source_code: sc.source_code || sc.source_value,
    source_code_description: "",
    source_unit: sc.source_unit || "",
    example_quantity: sc.example_quantity || "",
    source_metadata: sc.source_metadata || {},
    destination_concept_id: sc.mapping_target_concept_id || 0,
    destination_concept_name: sc.mapping_target_concept_name || "",
    destination_concept_code: "",
    destination_vocabulary_id: "",
    destination_concept_class_id: "",
    destination_omop_table: sc.omop_table,
    destination_domain_id: domain,
    status: sc.mapping_status,
    notes: "",
    origin: "",
    origin_system: "",
    suggest_strategy: "",
    umls_cui: "",
    created_by: "",
    occurrence_count: sc.row_count,
    destination_count: 0,
    has_mapping: sc.mapping_id !== null,
  };
}

export default function PatientSourceCodesTab({ personId }: Props) {
  const { currentUser } = useAuth();
  const canApprove = !!(currentUser?.is_staff || currentUser?.is_org_admin);
  const [data, setData] = useState<SourceCodesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const [resolveResult, setResolveResult] = useState<ResolveResult | null>(null);
  const [filter, setFilter] = useState<"all" | "unmapped">("all");
  const [reference, setReference] = useState<Reference>(emptyReference);
  const [banner, setBanner] = useState<string | null>(null);

  // Dialog state
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogMode, setDialogMode] = useState<"new" | "edit">("new");
  const [dialogRow, setDialogRow] = useState<CodeMappingRow | null>(null);
  const [strategies, setStrategies] = useState({ umls: true, lexical: true, vectors: true });
  const [rankingModel, setRankingModel] = useState<"anthropic" | "jev" | "both">("jev");

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

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.get<Reference>("/v1/code-mappings/reference/");
        if (!cancelled) setReference({ ...emptyReference, ...(res.data || {}) });
      } catch {
        // Non-fatal: dialog works with empty reference for basic mapping
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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

  const handleRowClick = (sc: PatientSourceCode) => {
    setBanner(null);
    const row = sourceCodeToRow(sc);
    setDialogRow(row);
    setDialogMode(sc.mapping_id ? "edit" : "new");
    setDialogOpen(true);
  };

  const handleDialogClose = () => {
    setDialogOpen(false);
    setDialogRow(null);
  };

  const handleDialogSaved = (_saved: CodeMappingRow, repoint: RepointResult | null) => {
    handleDialogClose();
    if (repoint && repoint.rows_updated) {
      setBanner(
        `Updated ${repoint.rows_updated} row(s) across `
        + `${repoint.persons_marked_stale} patient(s)`
        + (repoint.rows_collapsed ? `, ${repoint.rows_collapsed} duplicate(s) collapsed` : "")
        + ". Patient records queued for re-derivation.",
      );
    } else {
      setBanner("Mapping saved.");
    }
    void fetchSourceCodes();
  };

  const handleDialogDeleted = () => {
    handleDialogClose();
    setBanner("Mapping deleted.");
    void fetchSourceCodes();
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

      {/* Save banner */}
      {banner && (
        <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-800">
          {banner}
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
                <th className="px-3 py-2 text-left font-medium">Units</th>
                <th className="px-3 py-2 text-left font-medium">Qty</th>
                <th className="px-3 py-2 text-left font-medium">Current Concept</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Status</th>
                <th className="px-3 py-2 text-left font-medium">Mapping Target</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filtered.map((sc, i) => {
                const badge = STATUS_BADGE[sc.mapping_status] || STATUS_BADGE.unmapped;
                return (
                  <tr
                    key={`${sc.omop_table}-${sc.source_value}-${i}`}
                    className="cursor-pointer hover:bg-muted/30"
                    onClick={() => handleRowClick(sc)}
                  >
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
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                      {sc.source_unit || "\u2014"}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                      {sc.example_quantity || "\u2014"}
                    </td>
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
              })}
            </tbody>
          </table>
        </div>
      )}

      <EditMappingDialog
        open={dialogOpen}
        onClose={handleDialogClose}
        mode={dialogMode}
        row={dialogRow}
        reference={reference}
        canApprove={canApprove}
        strategies={strategies}
        rankingModel={rankingModel}
        onStrategiesChange={setStrategies}
        onRankingModelChange={setRankingModel}
        onSaved={handleDialogSaved}
        onDeleted={handleDialogDeleted}
        onBanner={setBanner}
      />
    </div>
  );
}
