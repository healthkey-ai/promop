import { render, screen, fireEvent } from '@testing-library/react';
import { vi, it, expect } from 'vitest';
import PatientList from './PatientList';
import api from '@/api/axios';

const navigate = vi.hoisted(() => vi.fn());
vi.mock('react-router-dom', () => ({ useNavigate: () => navigate }));
vi.mock('@/api/axios', () => ({ default: { get: vi.fn() } }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ currentUser: { is_staff: true } }) }));

it('shows genomics, zero and nonzero therapy counts, and the full name on hover', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { count: 2, results: [
    { person_id: 1, patient_name: 'Alexandra Catherine Example', disease: 'Breast Cancer',
      genomics_summary: 'BRCA1 c.68_69delAG (present); TP53 (absent)', therapy_lines_count: 3 },
    { person_id: 2, patient_name: 'Sam Example', disease: 'Follicular Lymphoma',
      genomics_summary: '', therapy_lines_count: 0 },
  ] } });
  render(<PatientList />);
  expect(await screen.findByRole('columnheader', { name: 'Genomics' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Num Lines' })).toBeInTheDocument();
  expect(screen.getByTitle('Alexandra Catherine Example')).toHaveClass('truncate', 'w-28');
  expect(screen.getByTitle('BRCA1 c.68_69delAG (present); TP53 (absent)')).toBeInTheDocument();
  expect(screen.getByText('No genomic data')).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '3' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '0' })).toBeInTheDocument();
  fireEvent.click(screen.getByTitle('BRCA1 c.68_69delAG (present); TP53 (absent)'));
  expect(navigate).toHaveBeenCalledWith('/patient/1');
});
