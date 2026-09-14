import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { vi, it, expect, beforeEach } from 'vitest';
import PatientList from './PatientList';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '@/api/axios';

const navigate = vi.hoisted(() => vi.fn());
vi.mock('react-router-dom', async importOriginal => ({ ...await importOriginal<typeof import('react-router-dom')>(), useNavigate: () => navigate }));
vi.mock('@/api/axios', () => ({ default: { get: vi.fn(), delete: vi.fn() } }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ currentUser: { is_staff: true } }) }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

it('shows genomics, zero and nonzero therapy counts, and the full name on hover', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { count: 2, results: [
    { person_id: 1, patient_name: 'Alexandra Catherine Example', disease: 'Breast Cancer',
      genomics_summary: 'BRCA1 c.68_69delAG (present); TP53 (absent)', therapy_lines_count: 3 },
    { person_id: 2, patient_name: 'Sam Example', disease: 'Follicular Lymphoma',
      genomics_summary: '', therapy_lines_count: 0 },
  ] } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
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


const cohort = { count: 30, results: [{ person_id: 7, patient_name: 'Review Example', age: 50,
  disease: 'Breast Cancer', stage: 'II', therapy_lines_count: 0, ecog_performance_status: 0,
  ecog_assessment_date: '2026-01-01', disease_status: 'Remission', subtype_biomarkers: 'HER2: Negative',
  treatment_summary: { name: 'Recorded regimen', line: 2, start_date: '2025-05-01', end_date: null },
  data_gaps: ['Genomics'], latest_result_date: '2026-01-02', location_summary: 'Portland, OR',
  contact_available: true, organization_name: 'Example Foundation',
}] };

it('shows clinical context without inferring active treatment or losing ECOG zero', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  expect(await screen.findByText('Recorded regimen')).toBeInTheDocument();
  expect(screen.getByTitle('HER2: Negative')).toBeInTheDocument();
  expect(screen.getByText('End: Not recorded')).toBeInTheDocument();
  const row = screen.getByTitle('Review Example').closest('tr')!;
  expect(within(row).queryByText(/active treatment/i)).not.toBeInTheDocument();
  expect(within(row).getByText('Remission')).toBeInTheDocument();
  expect(within(row).getAllByText('0')).toHaveLength(2);
});

it('saves clinical filters and sort separately for each audience view', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  const { unmount } = render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  fireEvent.change(screen.getByLabelText('ECOG'), { target: { value: '0' } });
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ecog: '0', page: 1 }) })));
  fireEvent.change(await screen.findByLabelText('Sort by'), { target: { value: '-gaps' } });
  await screen.findByText('Recorded regimen');
  fireEvent.change(screen.getByLabelText('Audience view'), { target: { value: 'foundation' } });
  expect(await screen.findByRole('columnheader', { name: 'Contact Info' })).toBeInTheDocument();
  expect(screen.queryByRole('columnheader', { name: 'Latest Recorded Line' })).not.toBeInTheDocument();
  expect(screen.getByText('Portland, OR')).toBeInTheDocument();
  expect(screen.getByLabelText('ECOG')).toHaveValue('all');
  fireEvent.change(screen.getByLabelText('Audience view'), { target: { value: 'doctor' } });
  await screen.findByText('Recorded regimen');
  expect(screen.getByLabelText('ECOG')).toHaveValue('0');
  expect(screen.getByLabelText('Sort by')).toHaveValue('-gaps');
  unmount();
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByLabelText('ECOG')).toHaveValue('0');
});

it('applies text searches across the server cohort and resets the page', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ page: 2 }) })));
  const search = await screen.findByLabelText('Subtype / biomarker');
  fireEvent.change(search, { target: { value: 'HER2' } });
  fireEvent.blur(search);
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ page: 1, biomarker: 'HER2' }) })));
});

it('uses the same clinical filters when deleting all matching patients', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  vi.mocked(api.delete).mockResolvedValue({ data: {} });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  fireEvent.change(screen.getByLabelText('ECOG'), { target: { value: '0' } });
  await screen.findByText('Recorded regimen');
  fireEvent.click(screen.getAllByRole('checkbox')[0]);
  fireEvent.click(screen.getByRole('button', { name: 'Delete (All 30)' }));
  fireEvent.click(screen.getByRole('button', { name: /^Delete$/ }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/patient-info/bulk_delete_filtered/', expect.objectContaining({ params: expect.objectContaining({ ecog: '0' }) })));
});

it('places Upload immediately after Mappings', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { count: 0, results: [] } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  const mappings = await screen.findByRole('button', { name: 'Mappings' });
  const upload = screen.getByRole('button', { name: 'Upload' });
  expect(mappings.nextElementSibling).toBe(upload);
  fireEvent.click(upload);
  expect(navigate).toHaveBeenCalledWith('/upload');
  expect(screen.queryByRole('button', { name: 'Upload CSV' })).not.toBeInTheDocument();
});


it('links the logo beside Patients to the system home', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  render(<MemoryRouter initialEntries={['/nested']}><Routes>
    <Route path="/nested" element={<PatientList />} />
    <Route path="/" element={<div>System home</div>} />
  </Routes></MemoryRouter>);
  const title = await screen.findByRole('heading', { name: 'Patients' });
  const logo = within(title.parentElement!).getByRole('link', { name: 'PRomop home' });
  expect(logo).toHaveAttribute('href', '/');
  fireEvent.click(logo);
  expect(screen.getByText('System home')).toBeInTheDocument();
});
