import { render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import StatsPage from './StatsPage';

vi.mock('@/api/axios', () => ({
  default: {
    get: vi.fn(),
  },
}));

import api from '@/api/axios';

const mockData = [
  {
    org_slug: 'abc-foundation',
    org_name: 'ABC Foundation',
    total: 5,
    disease_counts: [
      { disease_slug: 'mm', label: 'Multiple Myeloma', count: 3 },
      { disease_slug: 'breast-cancer', label: 'Breast Cancer', count: 2 },
    ],
  },
];

describe('StatsPage', () => {
  afterEach(() => vi.clearAllMocks());

  it('renders org cards with disease counts', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: mockData });
    render(<StatsPage />);
    await waitFor(() => expect(screen.getByText('ABC Foundation')).toBeInTheDocument());
    expect(screen.getByText('Multiple Myeloma')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('Breast Cancer')).toBeInTheDocument();
  });

  it('shows empty state when response is empty', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: [] });
    render(<StatsPage />);
    await waitFor(() =>
      expect(screen.getByText('No patient data available for your account.')).toBeInTheDocument()
    );
  });

  it('shows loading state before response resolves', () => {
    (api.get as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(() => {}));
    render(<StatsPage />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });
});
