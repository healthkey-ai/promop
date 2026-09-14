import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, Trash2, LogOut, Settings, Globe } from "lucide-react";
import api from "@/api/axios";
import { useAuth, type User } from "@/hooks/useAuth";
import { PaginationControls } from "@/components/labs/PaginationControls";
import { useLocalPagination } from "@/lib/pagination";

import { loadPreferences, REVIEW_FILTER_DEFAULTS, VIEW_COLUMNS, type PatientView, type ReviewFilters } from './patientListViews';

interface Patient {
  person_id: number;
  patient_name: string | null;
  age: number | null;
  organization_name?: string | null;
  organization_slug?: string | null;
  disease: string;
  stage: string;
  genomics_summary?: string;
  therapy_lines_count?: number | null;
  updated_at: string;
  treatment_summary?: { name: string; line: number; start_date: string | null; end_date: string | null } | null;
  disease_status?: string | null;
  subtype_biomarkers?: string;
  ecog_performance_status?: number | null;
  ecog_assessment_date?: string | null;
  data_gaps?: string[];
  latest_result_date?: string | null;
  location_summary?: string | null;
  contact_available?: boolean;
  demographics_redacted?: boolean;
}

const ALL_FILTER_VALUE = "all";

interface FilterOptions {
  orgs: Array<{ value: string; label: string }>;
  diseases: string[];
  stages: string[];
  clinical_statuses?: string[];
}

interface PaginatedPatientsResponse {
  count: number;
  next: string | null;
  previous: string | null;
  results: Patient[];
  filter_options?: FilterOptions;
}

const DATE_FILTER_OPTIONS = [
  { value: ALL_FILTER_VALUE, label: "All" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "this_year", label: "This year" },
];

const getErrorMessage = (err: unknown, fallback: string) => {
  const msg =
    err && typeof err === "object" && "response" in err
      ? (err as { response?: { data?: { error?: string; detail?: string } } }).response?.data
          ?.error ||
        (err as { response?: { data?: { error?: string; detail?: string } } }).response?.data
          ?.detail
      : undefined;
  return msg || fallback;
};

export default function PatientList() {
  const { currentUser, loading, logout } = useAuth();
  if (loading) return <div role="status" className="p-6">Loading patient list…</div>;
  return <PatientListContent key={currentUser?.id ?? 'session'} currentUser={currentUser} logout={logout} />;
}

function PatientListContent({ currentUser, logout }: { currentUser: User | null; logout: () => Promise<void> }) {
  const navigate = useNavigate();
  const preferenceKey = `promop-patient-list:${currentUser?.id ?? 'session'}`;
  const [preferences, setPreferences] = useState(() => loadPreferences(preferenceKey));
  const view = preferences.view;
  const savedView = preferences.views[view];
  const reviewFilters = savedView?.filters ?? REVIEW_FILTER_DEFAULTS;
  const ordering = savedView?.ordering ?? '-updated';
  const extraColumns: readonly string[] = VIEW_COLUMNS[view];
  const show = (column: string) => extraColumns.includes(column);
  useEffect(() => {
    try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch { /* Storage may be disabled. */ }
  }, [preferenceKey, preferences]);

  const [patients, setPatients] = useState<Patient[]>([]);
  const [patientCount, setPatientCount] = useState(0);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>({
    orgs: [],
    diseases: [],
    stages: ["I", "II", "III", "IV"],
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [selectAllMode, setSelectAllMode] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [orgFilter, setOrgFilter] = useState(ALL_FILTER_VALUE);
  const [diseaseFilter, setDiseaseFilter] = useState(ALL_FILTER_VALUE);
  const [stageFilter, setStageFilter] = useState(ALL_FILTER_VALUE);
  const [dateFilter, setDateFilter] = useState(ALL_FILTER_VALUE);
  const { page, pageSize, setPage, setPageSize } = useLocalPagination(10);

  const fetchPatients = useCallback(async (): Promise<boolean> => {
    try {
      setLoading(true);
      const response = await api.get<PaginatedPatientsResponse>("/patient-info/", {
        params: {
          page,
          page_size: pageSize,
          org: orgFilter,
          disease: diseaseFilter,
          stage: stageFilter,
          date: dateFilter,
          ...reviewFilters,
          ordering,
        },
      });
      setPatients(response.data.results);
      setPatientCount(response.data.count);
      if (response.data.filter_options) {
        setFilterOptions(response.data.filter_options);
      }
      setError(null);
      return true;
    } catch (err) {
      setError(getErrorMessage(err, "Failed to fetch patients"));
      setPatients([]);
      setPatientCount(0);
      return false;
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, orgFilter, diseaseFilter, stageFilter, dateFilter, reviewFilters, ordering]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    fetchPatients();
  }, [fetchPatients]);

  const resetToFirstPage = useCallback(() => {
    setPage(1);
    setSelectedIds(new Set());
    setSelectAllMode(false);
  }, [setPage]);

  const changeReviewFilter = (name: keyof ReviewFilters, value: string) => {
    resetToFirstPage();
    setPreferences((previous) => ({ ...previous, views: { ...previous.views,
      [view]: { filters: { ...reviewFilters, [name]: value }, ordering },
    } }));
  };
  const changeOrdering = (value: string) => {
    resetToFirstPage();
    setPreferences((previous) => ({ ...previous, views: { ...previous.views,
      [view]: { filters: reviewFilters, ordering: value },
    } }));
  };

  const stageOptions = useMemo(
    () => (filterOptions.stages.length > 0 ? filterOptions.stages : ["I", "II", "III", "IV"]),
    [filterOptions.stages]
  );

  const handleSelectAll = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.checked) {
      setSelectAllMode(true);
      setSelectedIds(new Set());
    } else {
      setSelectAllMode(false);
    }
  };

  const handleSelectOne = (personId: number) => {
    if (selectAllMode) {
      setSelectAllMode(false);
      setSelectedIds(new Set([personId]));
      return;
    }
    const next = new Set(selectedIds);
    if (next.has(personId)) next.delete(personId);
    else next.add(personId);
    setSelectedIds(next);
  };

  const handleDeleteConfirm = async () => {
    try {
      setDeleting(true);
      if (selectAllMode) {
        await api.delete("/patient-info/bulk_delete_filtered/", {
          params: { org: orgFilter, disease: diseaseFilter, stage: stageFilter, date: dateFilter, ...reviewFilters },
        });
      } else {
        await api.delete("/patient-info/bulk_delete/", {
          data: { person_ids: Array.from(selectedIds) },
        });
      }
      const refreshed = await fetchPatients();
      if (!refreshed) return;
      setSelectAllMode(false);
      setSelectedIds(new Set());
      setDeleteDialogOpen(false);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err, "Failed to delete patients"));
    } finally {
      setDeleting(false);
    }
  };

  const formatDate = (dateString?: string | null) => {
    if (!dateString) return "Not recorded";
    try {
      return new Date(`${dateString.slice(0, 10)}T00:00:00`).toLocaleDateString();
    } catch {
      return "Invalid Date";
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-[400px] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  const visibleSelectedCount = patients.filter((patient) =>
    selectedIds.has(patient.person_id)
  ).length;
  const isAllSelected =
    patients.length > 0 &&
    (selectAllMode || visibleSelectedCount === patients.length);
  const canManageMappings = !!(currentUser?.is_staff || currentUser?.is_org_admin);

  const handleLogout = () => {
    void logout();
  };

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">Patients</h1>
        <div className="flex gap-2">
          {(selectedIds.size > 0 || selectAllMode) && (
            <button
              onClick={() => setDeleteDialogOpen(true)}
              className="inline-flex items-center gap-2 rounded-md border border-destructive px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10"
            >
              <Trash2 size={16} />
              Delete ({selectAllMode ? `All ${patientCount}` : selectedIds.size})
            </button>
          )}
          {canManageMappings && (
            <>
              <button
                onClick={() => navigate("/mappings")}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
              >
                <Globe size={16} />
                Mappings
              </button>
              <button
                onClick={() => navigate("/upload")}
                className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-foreground hover:bg-accent"
              >
                <Upload size={16} />
                Upload
              </button>
            </>
          )}
          <div className="ml-auto flex gap-2">
            {canManageMappings && (
              <button
                onClick={() => navigate("/org-admin")}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
              >
                <Settings size={14} />
                Org Admin
              </button>
            )}
            {currentUser?.email && (
              <button
                onClick={() => navigate("/profile")}
                className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-accent"
              >
                {currentUser.email}
              </button>
            )}
          </div>
          <button
            onClick={handleLogout}
            className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-accent"
          >
            <LogOut size={16} />
            Logout
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-md bg-destructive/10 p-4 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-end gap-4">
        <label className="text-sm font-medium">Audience view
          <select aria-label="Audience view" value={view} onChange={(event) => {
            resetToFirstPage();
            setPreferences((previous) => ({ ...previous, view: event.target.value as PatientView }));
          }} className="ml-2 rounded-md border border-input bg-background p-2">
            <option value="doctor">Doctor</option><option value="foundation">Foundation</option><option value="analyst">Analyst</option>
          </select>
        </label>
        <label className="text-sm font-medium">Sort by
          <select aria-label="Sort by" value={ordering} onChange={(event) => changeOrdering(event.target.value)} className="ml-2 rounded-md border border-input bg-background p-2">
            <option value="-updated">Last updated</option><option value="-freshness">Newest clinical result</option>
            <option value="freshness">Oldest clinical result</option><option value="-gaps">Most key data gaps</option>
            <option value="gaps">Fewest key data gaps</option><option value="ecog">ECOG: low to high</option>
            <option value="-ecog">ECOG: high to low</option><option value="-lines">Most therapy lines</option>
            <option value="name">Name</option><option value="age">Age: youngest first</option><option value="-age">Age: oldest first</option>
            <option value="disease">Disease</option><option value="stage">Stage</option><option value="status">Disease status</option>
            <option value="organization">Organization</option>
          </select>
        </label>
        <span className="text-xs text-muted-foreground">View and clinical filters are saved in this browser.</span>
      </div>

      <div className="mb-4 rounded-lg border border-border bg-background p-4 shadow-sm">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            Org
            <select
              value={orgFilter}
              onChange={(e) => {
                resetToFirstPage();
                setOrgFilter(e.target.value);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            >
              <option value={ALL_FILTER_VALUE}>All</option>
              {filterOptions.orgs.map((org) => (
                <option key={org.value} value={org.value}>
                  {org.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            Disease
            <select
              value={diseaseFilter}
              onChange={(e) => {
                resetToFirstPage();
                setDiseaseFilter(e.target.value);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            >
              <option value={ALL_FILTER_VALUE}>All</option>
              {filterOptions.diseases.map((disease) => (
                <option key={disease} value={disease}>
                  {disease}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            Stage
            <select
              value={stageFilter}
              onChange={(e) => {
                resetToFirstPage();
                setStageFilter(e.target.value);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            >
              <option value={ALL_FILTER_VALUE}>All</option>
              {stageOptions.map((stage) => (
                <option key={stage} value={stage}>
                  {stage}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            Date
            <select
              value={dateFilter}
              onChange={(e) => {
                resetToFirstPage();
                setDateFilter(e.target.value);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            >
              {DATE_FILTER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <details className="mb-4 rounded-lg border border-border p-4" open>
        <summary className="cursor-pointer text-sm font-medium">Clinical filters</summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {([
            ['clinical_status', 'Disease status', [['all', 'All'], ['__unknown__', 'Not recorded'], ...(filterOptions.clinical_statuses ?? []).map((value) => [value, value])]],
            ['ecog', 'ECOG', [['all', 'All'], ...['0', '1', '2', '3', '4', '5'].map((value) => [value, value]), ['unknown', 'Not recorded']]],
            ['data_gap', 'Key data gaps', [['all', 'All'], ['any', 'Any key gap'], ['none', 'No key gaps'], ['stage', 'Missing stage'], ['ecog', 'Missing ECOG'], ['genomics', 'Missing genomics']]],
            ['freshness', 'Latest clinical result', [['all', 'All'], ['30d', 'Within 30 days'], ['90d', 'Within 90 days'], ['older', 'Older than 90 days'], ['unknown', 'Not recorded']]],
            ['contact', 'Contact information', [['all', 'All'], ['available', 'Recorded'], ['missing', 'Not recorded']]],
          ] as [keyof ReviewFilters, string, string[][]][]).map(([name, label, options]) => (
            <label key={name} className="flex flex-col gap-1 text-sm">{label}
              <select value={reviewFilters[name]} onChange={(event) => changeReviewFilter(name, event.target.value)} className="rounded-md border border-input bg-background p-2">
                {options.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
              </select>
            </label>
          ))}
          {([['treatment', 'Treatment history'], ['biomarker', 'Subtype / biomarker'], ['location', 'City / region / country']] as [keyof ReviewFilters, string][]).map(([name, label]) => (
            <label key={name} className="flex flex-col gap-1 text-sm">{label}
              <input type="search" defaultValue={reviewFilters[name]} key={`${view}-${name}-${reviewFilters[name]}`} placeholder="Search, then press Enter" onBlur={(event) => {
                if (event.target.value !== reviewFilters[name]) changeReviewFilter(name, event.target.value);
              }} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); }} className="rounded-md border border-input bg-background p-2" />
            </label>
          ))}
        </div>
        <p className="mt-3 text-xs text-muted-foreground">Key gaps track stage, ECOG, and genomic data. A recorded negative result counts as data. Clinical freshness uses result dates, not record update times.</p>
        <button type="button" className="mt-2 text-sm underline" onClick={() => {
          resetToFirstPage();
          setPreferences((previous) => ({ ...previous, views: { ...previous.views, [view]: { filters: { ...REVIEW_FILTER_DEFAULTS }, ordering: '-updated' } } }));
        }}>Clear clinical filters</button>
      </details>

      <div className="mb-3 text-sm font-medium text-muted-foreground">
        {patientCount} patient{patientCount === 1 ? "" : "s"}
      </div>

      {selectAllMode && (
        <div className="mb-3 rounded-md bg-muted px-4 py-2 text-sm text-muted-foreground">
          All {patientCount} patient{patientCount === 1 ? "" : "s"} selected.
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border bg-background shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-border bg-muted/50">
            <tr>
              <th className="w-10 px-4 py-3">
                <input
                  type="checkbox"
                  checked={isAllSelected}
                  onChange={handleSelectAll}
                  className="h-4 w-4 rounded border-input"
                />
              </th>
              <th className="px-4 py-3 font-medium text-muted-foreground">ID</th>
              <th className="w-36 px-4 py-3 font-medium text-muted-foreground">Name</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Age</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Disease</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Stage</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Genomics</th>
              <th className="whitespace-nowrap px-4 py-3 text-center font-medium text-muted-foreground">Num Lines</th>
              {extraColumns.map((column) => <th key={column} className="whitespace-nowrap px-4 py-3 font-medium text-muted-foreground">{{
                treatment: 'Latest Recorded Line', status: 'Disease Status', ecog: 'ECOG', gaps: 'Key Data Gaps',
                freshness: 'Latest Clinical Result', location: 'Location', contact: 'Contact Info', organization: 'Organization',
              }[column]}</th>)}
              <th className="px-4 py-3 font-medium text-muted-foreground">Last Updated</th>
            </tr>
          </thead>
          <tbody>
            {patients.length === 0 ? (
              <tr>
                <td colSpan={9 + extraColumns.length} className="py-12 text-center text-muted-foreground">
                  {error
                    ? "Unable to load patients."
                    : patientCount === 0 &&
                  orgFilter === ALL_FILTER_VALUE &&
                  diseaseFilter === ALL_FILTER_VALUE &&
                  stageFilter === ALL_FILTER_VALUE &&
                  dateFilter === ALL_FILTER_VALUE &&
                  Object.entries(reviewFilters).every(([key, value]) => value === REVIEW_FILTER_DEFAULTS[key as keyof ReviewFilters])
                    ? "No patients found. Upload a CSV or FHIR file to get started."
                    : "No patients match the selected filters."}
                </td>
              </tr>
            ) : (
              patients.map((patient) => (
                <tr
                  key={patient.person_id}
                  className="border-b border-border last:border-0 hover:bg-muted/30 cursor-pointer"
                >
                  <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectAllMode || selectedIds.has(patient.person_id)}
                      onChange={() => handleSelectOne(patient.person_id)}
                      className="h-4 w-4 rounded border-input"
                    />
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.person_id}
                  </td>
                  <td className="px-4 py-3 font-medium" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    <span className="block w-28 truncate" title={patient.patient_name || undefined}>{patient.demographics_redacted ? 'Withheld' : patient.patient_name || 'Not recorded'}</span>
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.demographics_redacted ? 'Withheld' : patient.age ?? 'Not recorded'}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.disease || "Not recorded"}
                    {patient.subtype_biomarkers && <span className="mt-1 block max-w-56 truncate text-xs text-muted-foreground" title={patient.subtype_biomarkers}>{patient.subtype_biomarkers}</span>}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.stage || "N/A"}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    <span className="block max-w-72 truncate" title={patient.genomics_summary || undefined}>
                      {patient.genomics_summary || "No genomic data"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-center tabular-nums" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.therapy_lines_count ?? "N/A"}
                  </td>
                  {show('treatment') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.treatment_summary ? <><span className="block max-w-56 truncate" title={patient.treatment_summary.name}>{patient.treatment_summary.name}</span>
                      <span className="block text-xs text-muted-foreground">Line {patient.treatment_summary.line} · Start: {formatDate(patient.treatment_summary.start_date)}</span>
                      <span className="block text-xs text-muted-foreground">End: {formatDate(patient.treatment_summary.end_date)}</span></> : 'Not recorded'}
                  </td>}
                  {show('status') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.disease_status || 'Not recorded'}</td>}
                  {show('ecog') && <td className="px-4 py-3 tabular-nums" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.ecog_performance_status ?? 'Not recorded'}
                    {patient.ecog_performance_status != null && <span className="block whitespace-nowrap text-xs text-muted-foreground">Assessed: {formatDate(patient.ecog_assessment_date)}</span>}
                  </td>}
                  {show('organization') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.organization_name || 'Not recorded'}</td>}
                  {show('location') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.demographics_redacted ? 'Withheld' : patient.location_summary || 'Not recorded'}</td>}
                  {show('contact') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.contact_available ? 'Recorded' : 'Not recorded'}</td>}
                  {show('gaps') && <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{patient.data_gaps?.length ? patient.data_gaps.join(', ') : patient.data_gaps ? 'No key gaps' : 'Not assessed'}</td>}
                  {show('freshness') && <td className="whitespace-nowrap px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>{formatDate(patient.latest_result_date)}</td>}
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {formatDate(patient.updated_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-4">
        <PaginationControls
          page={page}
          pageSize={pageSize}
          totalCount={patientCount}
          onPageChange={(nextPage) => {
            setSelectedIds(new Set());
            setSelectAllMode(false);
            setPage(nextPage);
          }}
          onPageSizeChange={(nextPageSize) => {
            setSelectedIds(new Set());
            setSelectAllMode(false);
            setPageSize(nextPageSize);
          }}
          pageSizes={[10, 50]}
        />
      </div>

      {deleteDialogOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-md rounded-lg bg-background p-6 shadow-xl">
            <h2 className="text-lg font-semibold">Confirm Delete</h2>
            <p className="mt-2 text-sm text-muted-foreground">
              {selectAllMode
                ? `Delete all ${patientCount} patient${patientCount === 1 ? "" : "s"} matching the current filters? This action cannot be undone.`
                : `Are you sure you want to delete ${selectedIds.size} patient record${selectedIds.size !== 1 ? "s" : ""}? This action cannot be undone.`}
            </p>
            {error && (
              <p className="mt-3 text-sm text-destructive">{error}</p>
            )}
            <div className="mt-6 flex justify-end gap-3">
              <button
                onClick={() => setDeleteDialogOpen(false)}
                disabled={deleting}
                className="rounded-md border border-input px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDeleteConfirm}
                disabled={deleting}
                className="rounded-md bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
              >
                {deleting ? "Deleting..." : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
