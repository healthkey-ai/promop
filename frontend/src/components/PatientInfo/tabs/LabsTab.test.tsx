import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import LabsTab from './LabsTab';
import type { LabMeasurementGroup } from '@/hooks/useLabMeasurements';
import type { LabResultValue } from '@/federation/types';

// ── Mocks ────────────────────────────────────────────────────────────────────

vi.mock('@/hooks/useWritableFields', () => ({
  useWritableFields: vi.fn(() => ({ descriptors: {}, loading: false, error: false })),
}));

vi.mock('@/hooks/useLabMeasurements', () => ({
  useLabMeasurements: vi.fn(() => ({ grouped: new Map(), isLoading: false })),
}));

vi.mock('@/components/labs/LabTrendChart', () => ({
  LabTrendChart: ({ values, unit }: { values: LabResultValue[]; unit: string }) => (
    <div data-testid="lab-trend-chart">{values.length} points, {unit}</div>
  ),
}));

vi.mock('../ClinicalField', () => ({
  default: ({ label, name }: { label: string; name: string }) => (
    <div data-testid={`field-${name}`}>{label}</div>
  ),
}));

vi.mock('../Section', () => ({
  default: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div data-testid={`section-${title}`}>{children}</div>
  ),
}));

import { useWritableFields } from '@/hooks/useWritableFields';
import { useLabMeasurements } from '@/hooks/useLabMeasurements';

const mockUseWritableFields = vi.mocked(useWritableFields);
const mockUseLabMeasurements = vi.mocked(useLabMeasurements);

function makeValues(count: number): LabResultValue[] {
  return Array.from({ length: count }, (_, i) => ({
    measurement_id: i + 1,
    value: 10 + i,
    value_string: null,
    unit: 'mg/dL',
    status: 'in_range' as const,
    measured_at: `2024-0${i + 1}-01`,
    range_low: 5,
    range_high: 20,
    source: '2160-0',
    lab_name: null,
    report_filename: null,
  }));
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('LabsTab', () => {
  const onChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    mockUseWritableFields.mockReturnValue({ descriptors: {}, loading: false, error: false });
    mockUseLabMeasurements.mockReturnValue({ grouped: new Map(), isLoading: false });
  });

  it('renders section titles', () => {
    render(<LabsTab formData={{}} onChange={onChange} />);
    expect(screen.getByTestId('section-Chemistry Panel')).toBeInTheDocument();
    expect(screen.getByTestId('section-Liver Function')).toBeInTheDocument();
  });

  it('does not show chart icon when field has fewer than 3 data points', () => {
    const group: LabMeasurementGroup = {
      sourceValue: '2160-0',
      unit: 'mg/dL',
      values: makeValues(2),
    };
    mockUseWritableFields.mockReturnValue({
      descriptors: {
        serum_creatinine_mg_dl: {
          kind: 'editable',
          writable: true,
          projection: { source_value: '2160-0' },
        },
      },
      loading: false,
      error: false,
    });
    mockUseLabMeasurements.mockReturnValue({
      grouped: new Map([['2160-0', group]]),
      isLoading: false,
    });
    render(<LabsTab formData={{}} onChange={onChange} />);
    expect(screen.queryByLabelText(/view trend chart for serum creatinine/i)).not.toBeInTheDocument();
  });

  it('shows chart icon when field has 3+ data points', () => {
    const group: LabMeasurementGroup = {
      sourceValue: '2160-0',
      unit: 'mg/dL',
      values: makeValues(5),
    };
    mockUseWritableFields.mockReturnValue({
      descriptors: {
        serum_creatinine_mg_dl: {
          kind: 'editable',
          writable: true,
          projection: { source_value: '2160-0' },
        },
      },
      loading: false,
      error: false,
    });
    mockUseLabMeasurements.mockReturnValue({
      grouped: new Map([['2160-0', group]]),
      isLoading: false,
    });
    render(<LabsTab formData={{}} onChange={onChange} />);
    expect(screen.getByLabelText(/view trend chart for serum creatinine/i)).toBeInTheDocument();
  });

  it('opens chart dialog on icon click', () => {
    const group: LabMeasurementGroup = {
      sourceValue: '2160-0',
      unit: 'mg/dL',
      values: makeValues(4),
    };
    mockUseWritableFields.mockReturnValue({
      descriptors: {
        serum_creatinine_mg_dl: {
          kind: 'editable',
          writable: true,
          projection: { source_value: '2160-0' },
        },
      },
      loading: false,
      error: false,
    });
    mockUseLabMeasurements.mockReturnValue({
      grouped: new Map([['2160-0', group]]),
      isLoading: false,
    });
    render(<LabsTab formData={{}} onChange={onChange} />);

    fireEvent.click(screen.getByLabelText(/view trend chart for serum creatinine/i));
    expect(screen.getByTestId('lab-trend-chart')).toBeInTheDocument();
    expect(screen.getByText('4 points, mg/dL')).toBeInTheDocument();
  });
});
