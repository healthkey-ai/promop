import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import api from "../../api/axios";
import { useAuth } from "../../hooks/useAuth";
import type {
  PatientSourceCode,
  SourceCodesResponse,
} from "../../types/sourceCodes";
import EditMappingDialog from "../CodeMappings/EditMappingDialog";
import SourceQuantityCell from "../CodeMappings/SourceQuantityCell";
import {
  type CodeMappingRow,
  type Reference,
  type RepointResult,
  emptyReference,
  repointBanner,
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
    source_code_description: sc.source_code_description || "",
    source_unit: sc.source_unit || "",
    example_quantity: sc.example_quantity || "",
    based_on: sc.based_on || [],
    source_code_id: sc.source_code_id,
    extracted_source_unit: sc.extracted_source_unit,
    extracted_example_quantity: sc.extracted_example_quantity,
    source_unit_override: sc.source_unit_override,
    example_quantity_override: sc.example_quantity_override,
    source_metadata: sc.source_metadata || {},
    // No destination is no destination, never concept 0 ("No matching concept").
    destination_concept_id: sc.mapping_target_concept_id || null,
    destination_concept_name: sc.mapping_target_concept_name || "",
    destination_concept_code: sc.mapping_destination_concept_code || "",
    destination_vocabulary_id: sc.mapping_destination_vocabulary_id || "",
    destination_concept_class_id: "",
    destination_omop_table: sc.omop_table,
    destination_domain_id: domain,
    status: sc.mapping_status,
    notes: "",
    origin: sc.mapping_origin || "",
    origin_system: sc.mapping_origin_system || "",
    suggest_strategy: "",
    umls_cui: "",
    created_by: sc.mapping_created_by || "",
    occurrence_count: sc.row_count,
    destination_count: 0,
    has_mapping: sc.mapping_id !== null,
  };
}

type SectionKey = "unmapped" | "proposed" | "approved";

/** Rows rendered per section before "Show all": thousands of rows render slowly. */
export const SECTION_ROW_LIMIT = 200;

const SECTIONS: { key: SectionKey; label: string; defaultOpen: boolean }[] = [
  { key: "unmapped", label: "Unmapped", defaultOpen: true },
  { key: "proposed", label: "Proposed", defaultOpen: true },
  { key: "approved", label: "Approved", defaultOpen: false },
];

function SourceCodeTable({
  rows,
  onRowClick,
}: {
  rows: PatientSourceCode[];
  onRowClick: (sc: PatientSourceCode) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="border-b bg-muted/50">
          <tr>
            <th className="px-3 py-2 text-left font-medium">Source</th>
            <th className="px-3 py-2 text-left font-medium">Domain</th>
            <th className="px-3 py-2 text-right font-medium">Seen</th>
            <th className="px-3 py-2 text-left font-medium">Qty</th>
            <th className="px-3 py-2 text-left font-medium">Units</th>
            <th className="px-3 py-2 text-left font-medium">Destination</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((sc, i) => (
            <tr
              key={`${sc.omop_table}-${sc.source_value}-${i}`}
              className="cursor-pointer hover:bg-muted/30"
              onClick={() => onRowClick(sc)}
            >
              <td className="px-3 py-2">
                <div className="font-mono text-xs">{sc.source_value}</div>
                {sc.source_code_description && (
                  <div className="text-xs">{sc.source_code_description}</div>
                )}
                {sc.source_vocabulary_id && (
                  <div className="text-xs text-muted-foreground">{sc.source_vocabulary_id}</div>
                )}
              </td>
              <td className="px-3 py-2 text-xs text-muted-foreground">
                {TABLE_LABELS[sc.omop_table] || sc.omop_table}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{sc.row_count}</td>
              <SourceQuantityCell quantity={sc.example_quantity} units={sc.source_unit}
                basedOn={sc.based_on} correctedUnit={sc.source_unit_override != null} />
              <td className="px-3 py-2 text-xs">
                {sc.mapping_destination_concept_code ? (
                  <>
                    <div className="font-mono text-xs">{sc.mapping_destination_concept_code}</div>
                    <div>{sc.mapping_target_concept_name || "\u2014"}</div>
                    {sc.mapping_destination_vocabulary_id && (
                      <div className="text-muted-foreground">{sc.mapping_destination_vocabulary_id}</div>
                    )}
                  </>
                ) : (
                  <span className="text-muted-foreground">&mdash;</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PatientSourceCodesTab({ personId }: Props) {
  const { currentUser } = useAuth();
  const canApprove = !!(currentUser?.is_staff || currentUser?.is_org_admin);
  const [data, setData] = useState<SourceCodesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const [banner, setBanner] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<SectionKey, boolean>>({
    unmapped: false,
    proposed: false,
    approved: false,
  });
  const [collapsed, setCollapsed] = useState<Record<SectionKey, boolean>>({
    unmapped: false,
    proposed: false,
    approved: true,
  });

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

  const handleDialogSaved = (saved: CodeMappingRow, repoint: RepointResult | null) => {
    handleDialogClose();
    const message = repointBanner(saved.source_code, repoint);
    if (message) {
      setBanner(message);
    } else if (saved.status === "approved") {
      setBanner("Mapping approved. Run Generate OMOP to apply to clinical rows.");
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

  const toggleSection = (key: SectionKey) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  // A refetch after a save keeps the table on screen; only the first load blanks it.
  if (loading && !data) {
    return (
      <div className="space-y-4 p-6" role="status" aria-live="polite">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
          Loading source codes…
        </div>
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

  const grouped: Record<SectionKey, PatientSourceCode[]> = {
    unmapped: source_codes.filter((sc) => sc.mapping_status === "unmapped"),
    proposed: source_codes.filter((sc) => sc.mapping_status === "proposed"),
    approved: source_codes.filter((sc) => sc.mapping_status === "approved"),
  };

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
          {loading && (
            <span className="ml-2 inline-flex items-center gap-1" role="status">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Refreshing…
            </span>
          )}
        </div>
      </div>

      {/* Save banner */}
      {banner && (
        <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-800">
          {banner}
        </div>
      )}

      {/* Collapsible status sections */}
      {SECTIONS.map(({ key, label }) => {
        const rows = grouped[key];
        if (rows.length === 0) return null;
        const badge = STATUS_BADGE[key];
        const isCollapsed = collapsed[key];
        return (
          <section key={key}>
            <button
              onClick={() => toggleSection(key)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm font-medium hover:bg-muted/50"
            >
              {isCollapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
              <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${badge.bg} ${badge.text}`}>
                {label}
              </span>
              <span className="text-muted-foreground">({rows.length})</span>
            </button>
            {!isCollapsed && (
              <div className="mt-1">
                <SourceCodeTable
                  rows={expanded[key] ? rows : rows.slice(0, SECTION_ROW_LIMIT)}
                  onRowClick={handleRowClick}
                />
                {!expanded[key] && rows.length > SECTION_ROW_LIMIT && (
                  <button
                    onClick={() => setExpanded((prev) => ({ ...prev, [key]: true }))}
                    className="mt-2 text-sm font-medium text-primary hover:underline"
                  >
                    Show all {rows.length} ({rows.length - SECTION_ROW_LIMIT} more)
                  </button>
                )}
              </div>
            )}
          </section>
        );
      })}

      {source_codes.length === 0 && (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No source codes found.
        </p>
      )}

      <EditMappingDialog
        open={dialogOpen}
        patientId={personId}
        onSourceCodeUpdated={() => { void fetchSourceCodes(); }}
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
