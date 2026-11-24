import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import api from "../../api/axios";
import { useAuth } from "../../hooks/useAuth";
import {
  type PatientSourceCode,
  type ResolveRunStatus,
  type SourceCodesResponse,
  isWearableSourceCode,
} from "../../types/sourceCodes";
import EditMappingDialog from "../CodeMappings/EditMappingDialog";
import SourceQuantityCell from "../CodeMappings/SourceQuantityCell";
import PatientWearableCodes from "./PatientWearableCodes";
import { canWriteMappings, isStaffOrOrgAdmin } from "@/utils/roles";
import {
  type CodeMappingRow,
  type Reference,
  type RepointResult,
  emptyReference,
  patientRepointBanner,
  repointBanner,
} from "../CodeMappings/codeMappingTypes";
import {
  GENERATE_LOST,
  GENERATE_POLL_MS,
  GENERATE_POLL_RETRIES,
  GENERATE_SLOW,
  GENERATE_WATCH_MS,
  generateOmopOutcome,
  generateStartFailure,
} from "./generateOmop";

type Props = {
  personId: string;
  /** Called when Generate OMOP has refreshed this patient's record, so the
   *  page can re-read it (#2822). */
  onRecordRefreshed?: () => void | Promise<void>;
};

const wait = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

const errorDetail = (err: unknown): string | undefined => {
  const data = err && typeof err === "object" && "response" in err
    ? (err as { response?: { data?: { detail?: string; error?: string } } }).response?.data
    : undefined;
  return data?.detail || data?.error;
};

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
  /** Absent for a reader: the row opens nothing (#2416). */
  onRowClick?: (sc: PatientSourceCode) => void;
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
              className={onRowClick ? "cursor-pointer hover:bg-muted/30" : undefined}
              onClick={onRowClick ? () => onRowClick(sc) : undefined}
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

export default function PatientSourceCodesTab({ personId, onRecordRefreshed }: Props) {
  const { currentUser } = useAuth();
  const canApprove = isStaffOrOrgAdmin(currentUser);
  const canWrite = canWriteMappings(currentUser);
  const [data, setData] = useState<SourceCodesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reference, setReference] = useState<Reference>(emptyReference);
  const [banner, setBanner] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  // The run this tab is watching. Leaving the tab stops the watch, not the run.
  const generationRef = useRef<{ cancelled: boolean } | null>(null);
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
      setError(errorDetail(err) || "Failed to load source codes.");
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

  useEffect(() => () => {
    if (generationRef.current) generationRef.current.cancelled = true;
  }, []);

  /**
   * Generate OMOP for this patient (#2822): the server resolves their rows
   * against the current mappings and then derives their record, as one run.
   * The tab watches the run, then re-reads the list and has the page re-read
   * the record.
   */
  const handleGenerate = async () => {
    if (generating) return;
    const watch = { cancelled: false };
    generationRef.current = watch;
    setGenerating(true);
    setGenerateError(null);
    setBanner(null);
    // A run that stopped early has still committed what it did.
    const reread = () => Promise.all([fetchSourceCodes(), onRecordRefreshed?.()]);
    try {
      let runId: string;
      try {
        const started = await api.post<{ run_id: string }>(
          `/v1/patient-records/${personId}/resolve-source-codes/`, {},
        );
        runId = started.data.run_id;
      } catch (err) {
        if (watch.cancelled) return;
        const failure = generateStartFailure(
          (err as { response?: { status?: number } } | null)?.response?.status,
        );
        setGenerateError(failure.message);
        if (failure.mayHaveRun) await reread();
        return;
      }
      let run: ResolveRunStatus | null = null;
      let failedPolls = 0;
      const giveUpAt = Date.now() + GENERATE_WATCH_MS;
      while (!watch.cancelled && Date.now() < giveUpAt) {
        try {
          const polled = (await api.get<ResolveRunStatus>(`/v1/resolve-runs/${runId}/`)).data;
          failedPolls = 0;
          if (polled.state === "completed" || polled.state === "failed") {
            run = polled;
            break;
          }
        } catch {
          failedPolls += 1;
          if (failedPolls >= GENERATE_POLL_RETRIES) break;
        }
        await wait(GENERATE_POLL_MS);
      }
      if (watch.cancelled) return;
      if (!run) {
        setGenerateError(failedPolls >= GENERATE_POLL_RETRIES ? GENERATE_LOST : GENERATE_SLOW);
        return;
      }
      const outcome = generateOmopOutcome(run);
      if (outcome.ok) setBanner(outcome.message);
      else setGenerateError(outcome.message);
      await reread();
    } finally {
      if (!watch.cancelled) setGenerating(false);
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

  const handleDialogSaved = (saved: CodeMappingRow, repoint: RepointResult | null) => {
    handleDialogClose();
    // An approval from this page moves this patient's rows as it saves; the
    // response says how many (#2791). An approved save that reports no move
    // had nothing waiting, and a code with no row yet still needs Generate OMOP.
    setBanner(
      repointBanner(saved.source_code, repoint)
      ?? patientRepointBanner(saved.source_code, repoint)
      ?? (saved.status === "approved"
        ? "Mapping approved. Run Generate OMOP to apply to clinical rows."
        : "Mapping saved."),
    );
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

  // A wearable device's codes get a section of their own, first (#2835), and
  // are listed nowhere else: the three status sections hold the other codes.
  const wearableCodes = source_codes.filter(isWearableSourceCode);
  const otherCodes = source_codes.filter((sc) => !isWearableSourceCode(sc));

  const grouped: Record<SectionKey, PatientSourceCode[]> = {
    unmapped: otherCodes.filter((sc) => sc.mapping_status === "unmapped"),
    proposed: otherCodes.filter((sc) => sc.mapping_status === "proposed"),
    approved: otherCodes.filter((sc) => sc.mapping_status === "approved"),
  };

  // The summary counts every code. With wearable codes on the tab it names
  // them as a group and counts the rest by status, so each number is the
  // count of the section below it and together they are the total.
  const outsideWearables = (key: SectionKey) =>
    Math.max(summary[key] - wearableCodes.filter((sc) => sc.mapping_status === key).length, 0);

  return (
    <div className="space-y-4 p-6">
      {/* Summary banner */}
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">{summary.total}</span> source
          codes &mdash;{" "}
          {wearableCodes.length > 0 && <>{wearableCodes.length} wearable,{" "}</>}
          <span className="text-red-600">{outsideWearables("unmapped")} unmapped</span>,{" "}
          <span className="text-yellow-600">{outsideWearables("proposed")} proposed</span>,{" "}
          <span className="text-green-600">{outsideWearables("approved")} approved</span>
          {loading && (
            <span className="ml-2 inline-flex items-center gap-1" role="status">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
              Refreshing…
            </span>
          )}
        </div>
        {canApprove && (
          <button
            type="button"
            onClick={() => void handleGenerate()}
            disabled={generating}
            aria-busy={generating}
            title="Resolve this patient's rows against the current mappings, then refresh this patient's record. No other patient is changed."
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {generating ? (
              <>
                <span aria-hidden="true" className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                Generating OMOP…
              </>
            ) : (
              <>
                <RefreshCw aria-hidden="true" size={16} />
                Generate OMOP
              </>
            )}
          </button>
        )}
      </div>

      {generateError && (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {generateError}
        </div>
      )}

      {!canWrite && (
        <p role="status" className="text-sm text-muted-foreground">
          Read-only: mapping codes needs the analyst role.
        </p>
      )}

      {/* Progress and save banner, for whoever can save or run. The region is
          mounted before it has text: a live region inserted together with
          its text is not reliably announced. */}
      {canWrite && (
        <div role="status" className="space-y-4">
          {generating && (
            <p className="text-sm text-muted-foreground">
              Generating this patient&rsquo;s OMOP rows, then refreshing their record. Other patients are not changed.
            </p>
          )}
          {banner && (
            <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-800">
              {banner}
            </div>
          )}
        </div>
      )}

      {/* A wearable device's codes, and what their stored rows are now */}
      <PatientWearableCodes codes={wearableCodes} onRowClick={canWrite ? handleRowClick : undefined} />

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
                  onRowClick={canWrite ? handleRowClick : undefined}
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
        canWrite={canWrite}
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
