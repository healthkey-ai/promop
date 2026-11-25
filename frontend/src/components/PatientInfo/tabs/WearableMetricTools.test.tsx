import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MetricField } from './WearableMetricTools';
import { WearableMetricProvider } from './wearableMetricContext';

const mockGet = vi.fn();
vi.mock('@/api/axios', () => ({ default: { get: (...args: unknown[]) => mockGet(...args) } }));

// recharts measures its container, which jsdom cannot: the chart's numbers are
// read from the table that stands beside it.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('recharts');
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  };
});

const series = (overrides: Record<string, unknown> = {}) => ({
  field: 'median_daily_steps_30d',
  label: 'Median Daily Steps',
  statistic: 'sum',
  aggregate: "Median of the days' step totals",
  window: { start: '2026-10-22', end: '2026-11-20' },
  truncated: false,
  days: [
    { date: '2026-11-19', value: 4000, rows: 1, provisional_rows_excluded: 0, status: 'accepted', in_window: true },
    { date: '2026-11-20', value: 5500, rows: 2, provisional_rows_excluded: 0, status: 'provisional', in_window: true },
  ],
  groups: [{
    table: 'observations', concept_id: 3, concept_name: 'Steps', concept_code: '55423-8',
    vocabulary_id: 'LOINC', source_value: null, wearable: true, rows: 3,
  }],
  ...overrides,
});

function renderField(value: unknown, onOpenOmopRows?: (focus: unknown) => void, personId: number | undefined = 7) {
  return render(
    <WearableMetricProvider value={{ personId, onOpenOmopRows }}>
      <MetricField
        label="Median Daily Steps"
        name="median_daily_steps_30d"
        type="number"
        value={value}
        onChange={() => {}}
      />
    </WearableMetricProvider>,
  );
}

describe('30-day metric tools', () => {
  beforeEach(() => mockGet.mockReset());

  it('offers neither tool for a metric with no value', () => {
    renderField(undefined, vi.fn());
    expect(screen.queryByRole('button', { name: /chart/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /omop rows/i })).toBeNull();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('reads nothing until a tool is used', () => {
    renderField(5500, vi.fn());
    expect(screen.getByRole('button', { name: 'Chart Median Daily Steps over time' })).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('charts the daily values in a popup, each with its status', async () => {
    mockGet.mockResolvedValue({ data: series() });
    renderField(5500);
    await userEvent.click(screen.getByRole('button', { name: 'Chart Median Daily Steps over time' }));
    const dialog = await screen.findByRole('dialog');
    expect(mockGet).toHaveBeenCalledWith('/v1/patient-records/7/wearable-metric/', {
      params: { field: 'median_daily_steps_30d' },
    });
    expect(within(dialog).getByText("Median of the days' step totals")).toBeInTheDocument();
    const table = await within(dialog).findByRole('table');
    expect(within(table).getByText('5500')).toBeInTheDocument();
    expect(within(table).getByText('Provisional')).toBeInTheDocument();
    expect(within(table).getByText('Accepted')).toBeInTheDocument();
  });

  it('says so when no reading is stored', async () => {
    mockGet.mockResolvedValue({ data: series({ days: [], window: null, groups: [] }) });
    renderField(5500);
    await userEvent.click(screen.getByRole('button', { name: /chart/i }));
    expect(await screen.findByText(/no readings are stored/i)).toBeInTheDocument();
  });

  it('says so when the values cannot be loaded', async () => {
    // Only the series read fails: other readers of the mocked client (the field's own) must not.
    mockGet.mockImplementation((url: string) => (
      String(url).includes('wearable-metric') ? Promise.reject(new Error('boom')) : Promise.resolve({ data: {} })
    ));
    renderField(5500);
    await userEvent.click(screen.getByRole('button', { name: /chart/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be loaded/i);
  });

  it('has no link to rows where the OMOP tab is not offered', () => {
    renderField(5500, undefined);
    expect(screen.queryByRole('button', { name: /omop rows/i })).toBeNull();
    expect(screen.getByRole('button', { name: /chart/i })).toBeInTheDocument();
  });

  it('opens the one group holding the rows', async () => {
    mockGet.mockResolvedValue({ data: series() });
    const open = vi.fn();
    renderField(5500, open);
    await userEvent.click(screen.getByRole('button', { name: 'Show the OMOP rows behind Median Daily Steps' }));
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1));
    expect(open).toHaveBeenCalledWith(expect.objectContaining({
      table: 'observations', concept_id: 3, source_value: null, wearable: true,
    }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('lists each group when the rows are in several', async () => {
    const groups = [
      { table: 'observations', concept_id: 3, concept_name: 'Steps', source_value: null, wearable: true, rows: 3 },
      { table: 'measurements', concept_id: 4, concept_name: 'Step count', source_value: null, wearable: false, rows: 1 },
    ];
    mockGet.mockResolvedValue({ data: series({ groups }) });
    const open = vi.fn();
    renderField(5500, open);
    await userEvent.click(screen.getByRole('button', { name: /omop rows/i }));
    const dialog = await screen.findByRole('dialog');
    expect(open).not.toHaveBeenCalled();
    await userEvent.click(await within(dialog).findByRole('button', { name: /Step count/ }));
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ table: 'measurements', concept_id: 4 }));
  });

  it('tells a reader there are no rows when none are found', async () => {
    mockGet.mockResolvedValue({ data: series({ groups: [] }) });
    const open = vi.fn();
    renderField(5500, open);
    await userEvent.click(screen.getByRole('button', { name: /omop rows/i }));
    // Said where a sighted reader sees it, not only to a screen reader.
    const note = await screen.findByText(/no omop rows found/i);
    expect(note).toBeVisible();
    expect(note).not.toHaveClass('sr-only');
    expect(open).not.toHaveBeenCalled();
  });
});
