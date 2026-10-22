import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import ClinicalSummaryTab from "./ClinicalSummaryTab";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockInfiniteQuery = vi.fn();
vi.mock("@/hooks/useInfiniteOmopQuery", () => ({
  useInfiniteOmopQuery: (...args: unknown[]) => mockInfiniteQuery(...args),
}));

vi.mock("../Section", () => ({
  default: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div data-testid={`section-${title}`}>{title}{children}</div>
  ),
}));

vi.mock("@/components/labs/LabTrendChart", () => ({
  LabTrendChart: ({ values, unit }: { values: unknown[]; unit: string }) => (
    <div data-testid="lab-trend-chart">
      Chart: {values.length} values, unit={unit}
    </div>
  ),
}));

vi.mock("@/components/UI/InfiniteScrollSentinel", () => ({
  InfiniteScrollSentinel: () => <div data-testid="scroll-sentinel" />,
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function emptyQuery(_endpoint: string) {
  return {
    allResults: [],
    totalCount: 0,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    data: { pages: [{ count: 0, next: null, previous: null, results: [] }] },
  };
}

function queryWith<T>(endpoint: string, rows: T[], total?: number) {
  return {
    allResults: rows,
    totalCount: total ?? rows.length,
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    data: {
      pages: [{ count: total ?? rows.length, next: null, previous: null, results: rows }],
    },
  };
}

function setupEmptyQueries() {
  mockInfiniteQuery.mockImplementation((endpoint: string) => emptyQuery(endpoint));
}

function renderTab(
  formData: Record<string, unknown> = {},
  onNavigateToLabs = vi.fn(),
) {
  return {
    onNavigateToLabs,
    ...render(
      <ClinicalSummaryTab formData={formData} onNavigateToLabs={onNavigateToLabs} />,
    ),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  mockInfiniteQuery.mockReset();
});

describe("ClinicalSummaryTab — empty state", () => {
  it("renders global empty state when all domains are empty", () => {
    setupEmptyQueries();
    renderTab();
    expect(screen.getByText(/no clinical data yet/i)).toBeInTheDocument();
  });

  it("shows CTA link to Labs tab", () => {
    setupEmptyQueries();
    const { onNavigateToLabs } = renderTab();
    const link = screen.getByRole("button", { name: /labs tab/i });
    fireEvent.click(link);
    expect(onNavigateToLabs).toHaveBeenCalled();
  });

  it("does not show empty state when wearable data exists", () => {
    setupEmptyQueries();
    renderTab({ resting_heart_rate_avg_30d: 65 });
    expect(screen.queryByText(/no clinical data yet/i)).not.toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — conditions section", () => {
  it("renders condition rows", () => {
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "conditions") {
        return queryWith("conditions", [
          {
            condition_occurrence_id: 1,
            person: 100,
            condition_source_value: "Type 2 diabetes",
            condition_start_date: "2024-01-15",
            condition_end_date: null,
            condition_status_source_value: "active",
            condition_concept: 123,
            is_erroneous: false,
          },
        ]);
      }
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByText("Type 2 diabetes")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — medications section", () => {
  it("renders drug exposure rows", () => {
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "drug-exposures") {
        return queryWith("drug-exposures", [
          {
            drug_exposure_id: 1,
            person: 100,
            drug_source_value: "Metformin 500mg",
            drug_exposure_start_date: "2024-03-01",
            drug_exposure_end_date: null,
            days_supply: 30,
            drug_concept: 456,
            is_erroneous: false,
          },
        ]);
      }
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByText("Metformin 500mg")).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — procedures section", () => {
  it("renders procedure rows", () => {
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "procedures") {
        return queryWith("procedures", [
          {
            procedure_occurrence_id: 1,
            person: 100,
            procedure_source_value: "Colonoscopy",
            procedure_date: "2024-06-15",
            procedure_end_date: null,
            procedure_concept: 789,
            is_erroneous: false,
          },
        ]);
      }
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByText("Colonoscopy")).toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — lab results (measurements)", () => {
  it("shows trend chart for measurement groups with 3+ readings", () => {
    const measurements = [
      { measurement_id: 1, person: 100, measurement_source_value: "Hemoglobin", measurement_date: "2024-01-01", value_as_number: 14.0, unit_source_value: "g/dL", range_low: 12, range_high: 17, measurement_concept: 10, is_erroneous: false, value_as_string: null, measurement_datetime: null },
      { measurement_id: 2, person: 100, measurement_source_value: "Hemoglobin", measurement_date: "2024-02-01", value_as_number: 13.5, unit_source_value: "g/dL", range_low: 12, range_high: 17, measurement_concept: 10, is_erroneous: false, value_as_string: null, measurement_datetime: null },
      { measurement_id: 3, person: 100, measurement_source_value: "Hemoglobin", measurement_date: "2024-03-01", value_as_number: 14.2, unit_source_value: "g/dL", range_low: 12, range_high: 17, measurement_concept: 10, is_erroneous: false, value_as_string: null, measurement_datetime: null },
    ];
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "measurements") return queryWith("measurements", measurements);
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByTestId("lab-trend-chart")).toBeInTheDocument();
    expect(screen.getByText(/3 values/)).toBeInTheDocument();
  });

  it("does not show trend chart for groups with <3 readings", () => {
    const measurements = [
      { measurement_id: 1, person: 100, measurement_source_value: "Hemoglobin", measurement_date: "2024-01-01", value_as_number: 14.0, unit_source_value: "g/dL", range_low: 12, range_high: 17, measurement_concept: 10, is_erroneous: false, value_as_string: null, measurement_datetime: null },
      { measurement_id: 2, person: 100, measurement_source_value: "Hemoglobin", measurement_date: "2024-02-01", value_as_number: 13.5, unit_source_value: "g/dL", range_low: 12, range_high: 17, measurement_concept: 10, is_erroneous: false, value_as_string: null, measurement_datetime: null },
    ];
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "measurements") return queryWith("measurements", measurements);
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.queryByTestId("lab-trend-chart")).not.toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — observations section", () => {
  it("renders observation rows", () => {
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "observations") {
        return queryWith("observations", [
          {
            observation_id: 1,
            person: 100,
            observation_source_value: "Smoking status",
            observation_date: "2024-05-01",
            value_as_number: null,
            value_as_string: "Never smoker",
            value_source_value: null,
            observation_concept: 321,
            is_erroneous: false,
          },
        ]);
      }
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByText("Smoking status")).toBeInTheDocument();
    expect(screen.getByText("Never smoker")).toBeInTheDocument();
  });
});

describe("ClinicalSummaryTab — wearables section", () => {
  it("renders wearable data from formData fields", () => {
    setupEmptyQueries();
    renderTab({
      person_id: 100,
      resting_heart_rate_avg_30d: 65,
      median_daily_steps_30d: 8500,
      wearable_last_sync_at: "2024-07-01T00:00:00Z",
    });
    expect(screen.getByText("65 bpm")).toBeInTheDocument();
    expect(screen.getByText("8,500 steps/day")).toBeInTheDocument();
  });

  it("shows empty wearable section when no wearable data", () => {
    // Need at least one domain to have data so global empty state doesn't show
    mockInfiniteQuery.mockImplementation((endpoint: string) => {
      if (endpoint === "conditions") {
        return queryWith("conditions", [
          { condition_occurrence_id: 1, person: 100, condition_source_value: "Test", condition_start_date: "2024-01-01", condition_end_date: null, condition_status_source_value: null, condition_concept: 1, is_erroneous: false },
        ]);
      }
      return emptyQuery(endpoint);
    });
    renderTab({ person_id: 100 });
    expect(screen.getByText(/no wearable data/i)).toBeInTheDocument();
  });
});
