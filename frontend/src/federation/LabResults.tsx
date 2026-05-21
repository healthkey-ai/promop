import { useMemo, useState } from "react";
import { ArrowLeft, Pencil, Trash2 } from "lucide-react";

import { LabsProvider } from "./LabsProvider";
import type { LabResultsProps, LabResultCard, LabResultValue } from "./types";
import { useLabResultsSummary, useUpdateMeasurement, useDeleteMeasurement } from "./hooks";
import { ConfirmDeleteDialog } from "@/components/labs/ConfirmDeleteDialog";
import { EditMeasurementDialog } from "@/components/labs/EditMeasurementDialog";
import { LabValueCard } from "@/components/labs/LabValueCard";
import { StatusDot } from "@/components/labs/StatusDot";
import { DataSourceBadge } from "@/components/labs/DataSourceBadge";
import { fmtNum, formatShortDate } from "@/lib/format";
import { LabTrendChart } from "@/components/labs/LabTrendChart";
import { PaginationControls } from "@/components/labs/PaginationControls";
import { Button } from "@/components/ui-labs/button";
import { Card, CardContent } from "@/components/ui-labs/card";
import { useLocalPagination } from "@/lib/pagination";

function ResultDetail({
  card,
  onBack,
  onResultDeleted,
}: {
  card: LabResultCard;
  onBack: () => void;
  onResultDeleted?: (measurementId: number) => void;
}) {
  const updateMeasurement = useUpdateMeasurement();
  const deleteMeasurement = useDeleteMeasurement();
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [editingMeasurement, setEditingMeasurement] = useState<LabResultValue | null>(null);

  const values = card.values;
  const latest = values[0];
  const testName = card.concept_name;
  const category = card.category;
  const isQualitative = latest && latest.value == null && latest.value_string != null;
  const unit = latest?.unit ?? "";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-4 w-4" /> Back to results
        </button>
      </div>

      <div>
        {category && (
          <p className="text-xs uppercase tracking-wide text-muted-foreground">{category}</p>
        )}
        <h2 className="text-xl font-bold text-foreground">{testName}</h2>
        {latest && (
          <p className="mt-1 text-sm text-muted-foreground">
            Latest:{" "}
            <span className="font-mono text-foreground">
              {formatValue(latest)}
              {unit && ` ${unit}`}
            </span>
            {latest.measured_at && (
              <span className="ml-2 text-xs">· {formatShortDate(latest.measured_at)}</span>
            )}
          </p>
        )}
      </div>

      {!isQualitative && (
        <Card>
          <CardContent className="p-4">
            <LabTrendChart values={values} unit={unit} />
          </CardContent>
        </Card>
      )}

      <div>
        <h3 className="mb-2 text-sm font-semibold text-foreground">History</h3>
        {values.length === 0 ? (
          <Card>
            <CardContent className="p-6 text-center">
              <p className="text-sm font-medium text-foreground">No measurements yet</p>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <ul className="divide-y divide-border">
              {values.map((r) => (
                <li key={r.measurement_id} className="flex items-center gap-3 px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-sm text-foreground">
                        {formatValue(r)}
                      </span>
                      {r.unit && (
                        <span className="text-xs text-muted-foreground">{r.unit}</span>
                      )}
                      <StatusDot status={r.status} />
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                      {r.measured_at && <span>{formatShortDate(r.measured_at)}</span>}
                      <DataSourceBadge
                        source={r.source}
                        labName={r.lab_name}
                        reportFilename={r.report_filename}
                      />
                      {(r.range_low != null || r.range_high != null) && (
                        <span>
                          ref{" "}
                          {r.range_low != null && r.range_high != null
                            ? `${fmtNum(Number(r.range_low))}–${fmtNum(Number(r.range_high))}`
                            : r.range_high != null
                              ? `< ${fmtNum(Number(r.range_high))}`
                              : `> ${fmtNum(Number(r.range_low!))}`}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => setEditingMeasurement(r)}
                      className="rounded p-1.5 text-muted-foreground hover:text-foreground transition-colors"
                      aria-label={`Edit ${testName} measurement`}
                    >
                      <Pencil className="h-4 w-4" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmDeleteId(r.measurement_id)}
                      className="rounded p-1.5 text-muted-foreground hover:text-error-700 transition-colors"
                      aria-label={`Delete ${testName} measurement`}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>

      <EditMeasurementDialog
        open={editingMeasurement !== null}
        onOpenChange={(open) => { if (!open) setEditingMeasurement(null); }}
        measurement={editingMeasurement}
        onSave={(data) => {
          updateMeasurement.mutate(data, {
            onSuccess: () => setEditingMeasurement(null),
          });
        }}
        isPending={updateMeasurement.isPending}
      />
      <ConfirmDeleteDialog
        open={confirmDeleteId !== null}
        onOpenChange={(open) => { if (!open) setConfirmDeleteId(null); }}
        onConfirm={() => {
          if (confirmDeleteId !== null) {
            deleteMeasurement.mutate(confirmDeleteId, {
              onSuccess: () => {
                onResultDeleted?.(confirmDeleteId);
                setConfirmDeleteId(null);
              },
            });
          }
        }}
        isPending={deleteMeasurement.isPending}
      />
    </div>
  );
}

function LabResultsInner({
  selectedTest: selectedTestProp,
  onNavigateToDetail,
  onBack,
  onResultDeleted,
}: Pick<LabResultsProps, "selectedTest" | "onNavigateToDetail" | "onBack" | "onResultDeleted">) {
  const { page, pageSize, setPage, setPageSize } = useLocalPagination(50);
  const { data, isLoading, isError } = useLabResultsSummary({ page, pageSize });
  const totalCount = data?.count ?? 0;
  const [selectedTest, setSelectedTest] = useState<string | null>(null);

  const activeTest = selectedTestProp ?? selectedTest;

  const cards = data?.results ?? [];

  const categoryGroups = useMemo(() => {
    const groups = new Map<string, LabResultCard[]>();
    for (const card of cards) {
      const cat = card.category || "Other";
      const list = groups.get(cat);
      if (list) list.push(card);
      else groups.set(cat, [card]);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => {
      if (a === "Other") return 1;
      if (b === "Other") return -1;
      return a.localeCompare(b);
    });
  }, [cards]);

  const handleNavigate = (conceptCode: string) => {
    if (onNavigateToDetail) {
      onNavigateToDetail(conceptCode);
    } else {
      setSelectedTest(conceptCode);
    }
  };

  if (activeTest) {
    const activeCard = cards.find((c) => c.concept_code === activeTest);
    if (activeCard) {
      return (
        <ResultDetail
          card={activeCard}
          onBack={onBack ?? (() => setSelectedTest(null))}
          onResultDeleted={onResultDeleted}
        />
      );
    }
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="p-6 text-center">
          <p className="text-sm font-medium text-foreground">Something went wrong</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Could not load results.{" "}
            <Button type="button" variant="link" className="h-auto p-0" onClick={() => setPage(1)}>
              Go to page 1
            </Button>
          </p>
        </CardContent>
      </Card>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-3">
        <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-32 animate-pulse rounded-md bg-muted" />
          ))}
        </div>
      </div>
    );
  }

  if (categoryGroups.length === 0 && page === 1) {
    return (
      <Card>
        <CardContent className="p-6">
          <div className="py-4 text-center">
            <p className="text-sm font-medium text-foreground">No lab values yet</p>
            <p className="mt-1 text-sm text-muted-foreground">
              Upload a lab report, or add values manually to see them trend over time.
            </p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">Lab Results</h2>
      {categoryGroups.map(([category, groupCards]) => (
        <div key={category}>
          <h3 className="mb-2 text-sm font-semibold text-foreground">
            {category}
          </h3>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {groupCards.map((card) => (
              <LabValueCard
                key={card.concept_id}
                card={card}
                onNavigate={handleNavigate}
              />
            ))}
          </div>
        </div>
      ))}
      {totalCount > pageSize && (
        <PaginationControls
          page={page}
          pageSize={pageSize}
          totalCount={totalCount}
          onPageChange={setPage}
          onPageSizeChange={setPageSize}
        />
      )}
    </div>
  );
}

export function LabResults({
  apiClient,
  apiBasePath,
  queryClient,
  className,
  theme,
  selectedTest,
  onNavigateToDetail,
  onBack,
  onResultDeleted,
}: LabResultsProps) {
  return (
    <LabsProvider
      apiClient={apiClient}
      apiBasePath={apiBasePath}
      queryClient={queryClient}
      theme={theme}
      className={className}
    >
      <LabResultsInner
        selectedTest={selectedTest}
        onNavigateToDetail={onNavigateToDetail}
        onBack={onBack}
        onResultDeleted={onResultDeleted}
      />
    </LabsProvider>
  );
}

export default LabResults;

function formatValue(r: LabResultValue): string {
  if (r.value != null) return String(Number(Number(r.value).toFixed(2)));
  return r.value_string || "—";
}
