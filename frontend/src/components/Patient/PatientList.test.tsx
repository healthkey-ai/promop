import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { vi, it, expect, beforeEach } from 'vitest';
import PatientList from './PatientList';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import api from '@/api/axios';

const navigate = vi.hoisted(() => vi.fn());
const mockUseAuth = vi.hoisted(() => vi.fn());
vi.mock('react-router-dom', async importOriginal => ({ ...await importOriginal<typeof import('react-router-dom')>(), useNavigate: () => navigate }));
vi.mock('@/api/axios', () => ({ default: { get: vi.fn(), delete: vi.fn() } }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => mockUseAuth() }));

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({ currentUser: { is_staff: true } });
  localStorage.clear();
});

it('shows unmapped counts last with zero in red and removes Genomics and Disease Status', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { count: 2, results: [
    { person_id: 1, patient_name: 'Alexandra Catherine Example', disease: 'Breast Cancer',
      therapy_lines_count: 3, unmapped_source_code_count: 12 },
    { person_id: 2, patient_name: 'Sam Example', disease: 'Follicular Lymphoma',
      therapy_lines_count: 0, unmapped_source_code_count: 0 },
  ] } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  const first = (await screen.findByTitle('Alexandra Catherine Example')).closest('tr')!;
  const second = screen.getByTitle('Sam Example').closest('tr')!;
  expect(screen.getAllByRole('columnheader').at(-1)).toHaveTextContent('Unmapped Source Codes');
  expect(screen.queryByRole('columnheader', { name: 'Genomics' })).not.toBeInTheDocument();
  expect(screen.queryByRole('columnheader', { name: 'Disease Status' })).not.toBeInTheDocument();
  expect(first.lastElementChild).toHaveTextContent('12');
  expect(first.lastElementChild).not.toHaveClass('text-red-600');
  expect(second.lastElementChild).toHaveTextContent('0');
  expect(second.lastElementChild).toHaveClass('text-red-600');
  expect(screen.getByTitle('Alexandra Catherine Example')).toHaveClass('truncate', 'w-28');
  fireEvent.click(first.lastElementChild!);
  expect(navigate).toHaveBeenCalledWith('/patient/1');
  expect(screen.queryByLabelText('Audience view')).not.toBeInTheDocument();
});

it('sorts unmapped counts on the server and preserves the ordering preference', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { count: 30, results: [] } });
  const { unmount } = render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByRole('button', { name: 'Unmapped Source Codes' });
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ page: 2 }) })));
  fireEvent.click(await screen.findByRole('button', { name: 'Unmapped Source Codes' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ page: 1, ordering: 'unmapped_source_codes' }) })));
  fireEvent.click(screen.getByRole('button', { name: /Unmapped Source Codes/ }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: '-unmapped_source_codes' }) })));
  expect(screen.getByRole('columnheader', { name: /Unmapped Source Codes/ })).toHaveAttribute('aria-sort', 'descending');
  unmount();
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  expect(await screen.findByRole('columnheader', { name: /Unmapped Source Codes/ })).toHaveAttribute('aria-sort', 'descending');
  expect(screen.queryByLabelText('Sort by')).not.toBeInTheDocument();
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
  expect(within(row).queryByText('Remission')).not.toBeInTheDocument();
  expect(within(row).getAllByText('0')).toHaveLength(2);
});

it('saves clinical filters and sort in this browser, carrying over an audience-preset record', async () => {
  localStorage.setItem('promop-patient-list:session', JSON.stringify({
    view: 'foundation', views: { foundation: { filters: { ecog: '1' }, ordering: '-freshness' }, doctor: { filters: { ecog: '2' }, ordering: '-gaps' } },
  }));
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  const { unmount } = render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByLabelText('ECOG')).toHaveValue('1');
  expect(screen.getByRole('columnheader', { name: /Latest Clinical Result/ })).toHaveAttribute('aria-sort', 'descending');
  fireEvent.change(screen.getByLabelText('ECOG'), { target: { value: '0' } });
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ecog: '0', page: 1 }) })));
  fireEvent.click(screen.getByRole('button', { name: 'Key Data Gaps' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: '-gaps' }) })));
  await screen.findByText('Recorded regimen');
  unmount();
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByLabelText('ECOG')).toHaveValue('0');
  expect(screen.getByRole('columnheader', { name: /Key Data Gaps/ })).toHaveAttribute('aria-sort', 'descending');
});

it('sorts A to Z first on text columns and reverses on a second click', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  fireEvent.click(screen.getByRole('button', { name: 'Disease' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: 'disease', page: 1 }) })));
  fireEvent.click(screen.getByRole('button', { name: /Disease/ }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: '-disease' }) })));
  expect(screen.getByRole('columnheader', { name: /Disease/ })).toHaveAttribute('aria-sort', 'descending');
});

it.each([
  ['a doctor', { org_accesses: [{ role: 'doctor', org_name: 'Example', org_slug: 'example', expires_at: null }] }, false],
  ['an org admin', { is_org_admin: true, org_accesses: [{ role: 'org_admin', org_name: 'Example', org_slug: 'example', expires_at: null }] }, true],
])('derives the columns from the roles: %s', async (_label, currentUser, outreach) => {
  mockUseAuth.mockReturnValue({ currentUser });
  vi.mocked(api.get).mockResolvedValue({ data: { ...cohort, filter_options: { orgs: [{ value: 'example', label: 'Example' }], diseases: [], stages: [] } } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByRole('columnheader', { name: 'Latest Recorded Line' })).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'ECOG' })).toBeInTheDocument();
  expect(screen.queryByRole('columnheader', { name: 'Organization' })).not.toBeInTheDocument();
  const outreachHeaders = ['Location', 'Contact Info'];
  for (const name of outreachHeaders) {
    if (outreach) expect(screen.getByRole('columnheader', { name })).toBeInTheDocument();
    else expect(screen.queryByRole('columnheader', { name })).not.toBeInTheDocument();
  }
  if (outreach) {
    expect(screen.getByText('Portland, OR')).toBeInTheDocument();
    expect(screen.getByLabelText('Contact information')).toBeInTheDocument();
  } else {
    expect(screen.queryByLabelText('Contact information')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('City / region / country')).not.toBeInTheDocument();
  }
});

it('does not refetch when only the filter_options identity changes', async () => {
  // The response replaces `filter_options` on every page-1 answer; keying a
  // fetch dependency on that array made the list re-request for ever.
  vi.mocked(api.get).mockImplementation(() => Promise.resolve({ data: { ...cohort, filter_options: {
    orgs: [{ value: 'example', label: 'Example' }], diseases: [], stages: [],
  } } }));
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  // A response-triggered loop issues its next request immediately; let those
  // turns pass before counting.
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(vi.mocked(api.get).mock.calls.filter(([url]) => url === '/patient-info/')).toHaveLength(1);
});

it('adds the Organization column once the list spans more than one organization', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { ...cohort, filter_options: {
    orgs: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }], diseases: [], stages: [],
  } } });

  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByRole('columnheader', { name: 'Organization' })).toBeInTheDocument();
  expect(screen.getByText('Example Foundation')).toBeInTheDocument();
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

it('searches names or email and keeps Org All when selecting unassigned patients', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { ...cohort, filter_options: {
    orgs: [{ value: 'example', label: 'Example' }, { value: '__unassigned__', label: 'Unassigned' }],
    diseases: [], stages: [],
  } } });
  vi.mocked(api.delete).mockResolvedValue({ data: {} });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.getByLabelText('Org')).toHaveValue('all');
  expect(screen.getByRole('option', { name: 'All (including unassigned)' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
  const search = await screen.findByLabelText('Name or email');
  fireEvent.change(search, { target: { value: 'ada@example.test' } });
  fireEvent.blur(search);
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({
    params: expect.objectContaining({ page: 1, org: 'all', search: 'ada@example.test' }),
  })));
  fireEvent.click(screen.getAllByRole('checkbox')[0]);
  fireEvent.click(screen.getByRole('button', { name: 'Delete (All 30)' }));
  fireEvent.click(screen.getByRole('button', { name: /^Delete$/ }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/patient-info/bulk_delete_filtered/', expect.objectContaining({
    params: expect.objectContaining({ org: 'all', search: 'ada@example.test' }),
  })));
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

it.each([
  [['doctor'], '/mappings'],
  [['doctor', 'analyst'], '/mappings'],
])('offers an organization %s "Mappings" to %s and Upload, never Org Admin', async (roles, target) => {
  mockUseAuth.mockReturnValue({
    currentUser: {
      org_accesses: roles.map((role) => ({ role, org_name: 'Example', org_slug: 'example', expires_at: null })),
    },
  });
  vi.mocked(api.get).mockResolvedValue({ data: { count: 0, results: [] } });

  render(<MemoryRouter><PatientList /></MemoryRouter>);

  fireEvent.click(await screen.findByRole('button', { name: 'Mappings' }));
  expect(navigate).toHaveBeenCalledWith(target);
  expect(screen.getByRole('button', { name: 'Upload' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Org Admin' })).not.toBeInTheDocument();
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

it('downloads the selected patients as CSV', async () => {
  const csv = new Blob(['person_id\n7\n'], { type: 'text/csv' });
  vi.mocked(api.get).mockImplementation(async (url: string) => (url === '/v1/patient-records/export-csv/'
    ? { data: csv, headers: { 'content-disposition': 'attachment; filename="patients_2026-10-10.csv"' } }
    : { data: cohort }));
  const createObjectURL = vi.fn(() => 'blob:csv');
  const revokeObjectURL = vi.fn();
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.queryByRole('button', { name: /Download CSV/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getAllByRole('checkbox')[1]);
  const download = screen.getByRole('button', { name: 'Download CSV (1)' });
  expect(download).toHaveClass('bg-primary');
  fireEvent.click(download);
  await waitFor(() => expect(api.get).toHaveBeenCalledWith('/v1/patient-records/export-csv/',
    { params: { person_ids: '7' }, responseType: 'blob' }));
  await waitFor(() => expect(click).toHaveBeenCalled());
  expect(createObjectURL).toHaveBeenCalledWith(csv);
  expect((click.mock.contexts[0] as HTMLAnchorElement).download).toBe('patients_2026-10-10.csv');
  click.mockRestore();
});

it('downloads every matching patient with the list filters when all are selected', async () => {
  vi.mocked(api.get).mockImplementation(async (url: string) => (url === '/v1/patient-records/export-csv/'
    ? Promise.reject({ response: { data: new Blob([JSON.stringify({ error: 'At most 500 patients per export; narrow the filters.' })]) } })
    : { data: cohort }));
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  fireEvent.change(screen.getByLabelText('ECOG'), { target: { value: '0' } });
  await screen.findByText('Recorded regimen');
  fireEvent.click(screen.getAllByRole('checkbox')[0]);
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV (All 30)' }));
  await waitFor(() => expect(api.get).toHaveBeenCalledWith('/v1/patient-records/export-csv/', expect.objectContaining({
    params: expect.objectContaining({ ecog: '0', org: 'all' }), responseType: 'blob',
  })));
  expect(await screen.findByText(/At most 500 patients per export/)).toBeInTheDocument();
});

it('shows an analyst at a public demo org a de-identified, unopenable list (#2460)', async () => {
  mockUseAuth.mockReturnValue({
    currentUser: {
      org_accesses: [{ role: 'analyst', org_name: 'Demo', org_slug: 'demo', expires_at: null, org_allows_patient_signup: true }],
    },
  });
  vi.mocked(api.get).mockResolvedValue({ data: { count: 1, results: [
    { person_id: 5, patient_name: null, age: null, demographics_redacted: true, disease: 'Breast Cancer',
      therapy_lines_count: 1, unmapped_source_code_count: 2 },
  ] } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  const row = (await screen.findByText('Breast Cancer')).closest('tr')!;
  expect(screen.getByRole('status')).toHaveTextContent(/names and other demographics are withheld, and records cannot be opened/);
  expect(screen.queryByRole('columnheader', { name: 'Name' })).not.toBeInTheDocument();
  expect(row).toHaveAttribute('aria-disabled', 'true');
  expect(row).not.toHaveClass('cursor-pointer');
  fireEvent.click(row.lastElementChild!);
  fireEvent.click(screen.getByText('Breast Cancer'));
  expect(navigate).not.toHaveBeenCalledWith('/patient/5');
  expect(screen.getByRole('button', { name: 'Code Mapping' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument();
  // No name search and no name or age sort over fields the server withholds.
  expect(screen.getByLabelText('Patient ID')).toHaveAttribute('placeholder', 'Search by ID, then press Enter');
  expect(screen.queryByRole('button', { name: 'Name' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Age' })).not.toBeInTheDocument();
});

it('does not apply a saved contact or location filter whose control the reader cannot see', async () => {
  localStorage.setItem('promop-patient-list:session', JSON.stringify({
    view: 'foundation', views: { foundation: { filters: { contact: 'missing', location: 'Portland', ecog: '1' }, ordering: '-freshness' } },
  }));
  mockUseAuth.mockReturnValue({ currentUser: { org_accesses: [{ role: 'doctor', org_name: 'Example', org_slug: 'example', expires_at: null }] } });
  vi.mocked(api.get).mockResolvedValue({ data: cohort });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ contact: 'all', location: '', ecog: '1' }) }));
  // "Clear clinical filters" keeps the heading sort.
  fireEvent.click(screen.getByText('Clear clinical filters'));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ecog: 'all', ordering: '-freshness' }) })));
});

it('falls back from a saved name sort for a reader whose names are withheld, and still sorts by another heading', async () => {
  localStorage.setItem('promop-patient-list:session', JSON.stringify({ filters: {}, ordering: '-name' }));
  mockUseAuth.mockReturnValue({
    currentUser: { org_accesses: [{ role: 'analyst', org_name: 'Demo', org_slug: 'demo', expires_at: null, org_allows_patient_signup: true }] },
  });
  vi.mocked(api.get).mockResolvedValue({ data: { count: 1, results: [{ person_id: 5, patient_name: null, disease: 'Breast Cancer', therapy_lines_count: 1, unmapped_source_code_count: 2 }] } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Breast Cancer');
  expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: '-updated' }) }));
  expect(screen.getByRole('columnheader', { name: /Last Updated/ })).toHaveAttribute('aria-sort', 'descending');
  fireEvent.click(screen.getByRole('button', { name: 'Disease' }));
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith('/patient-info/', expect.objectContaining({ params: expect.objectContaining({ ordering: 'disease' }) })));
});

it('does not count unassigned patients as a second organization', async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { ...cohort, filter_options: {
    orgs: [{ value: 'example', label: 'Example' }, { value: '__unassigned__', label: 'Unassigned' }], diseases: [], stages: [],
  } } });
  render(<MemoryRouter><PatientList /></MemoryRouter>);
  await screen.findByText('Recorded regimen');
  expect(screen.queryByRole('columnheader', { name: 'Organization' })).not.toBeInTheDocument();
});
