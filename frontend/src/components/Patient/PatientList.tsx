import PageTitle from '@/components/Branding/PageTitle';
import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, Trash2, LogOut, Settings, Globe, RefreshCw, Download } from "lucide-react";
import api from "@/api/axios";
import { useAuth, type User } from "@/hooks/useAuth";
import type { ResolveRunStatus } from "@/types/sourceCodes";
import { PaginationControls } from "@/components/labs/PaginationControls";
import { useLocalPagination } from "@/lib/pagination";

import { columnsFor, DEFAULT_ORDERING, loadPreferences, REVIEW_FILTER_DEFAULTS, type ExtraColumn, type ReviewFilters } from './patientListViews';
import { canOpenPatients, canViewMappings, isStaffOrOrgAdmin } from '@/utils/roles';

interface Patient {
  person_id: number;
  patient_name: string | null;
  age: number | null;
  organization_name?: string | null;
  organization_slug?: string | null;
  disease: string;
  stage: string;
  unmapped_source_code_count?: number;
  therapy_lines_count?: number | null;
  updated_at: string;
  treatment_summary?: { name: string; line: number; start_date: string | null; end_date: string | null } | null;
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
  disease_categories?: Array<{ value: string; label: string }>;
  diseases_by_category?: Record<string, string[]>;
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
  const reviewFilters = preferences.filters;
  // A reader who cannot open a patient -- an analyst browsing a public demo
  // org's sample patients (#1392, #2416) -- gets the rows de-identified by
  // the server and no way into the detail page, which would be 404 (#2460):
  // no Name column, no row link, no name or age sort. Upload and the mapping
  // hub are that same curator's; a demo-org analyst gets Code Mapping.
  const canOpen = canOpenPatients(currentUser);
  const canCurate = canOpen;
  const canAdministerOrganizations = isStaffOrOrgAdmin(currentUser);
  // A saved name or age sort means nothing to a reader whose names and ages
  // the server withholds, and no heading would show it: back to the default.
  const ordering = !canOpen && /^-?(name|age)$/.test(preferences.ordering) ? DEFAULT_ORDERING : preferences.ordering;
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
  // Columns follow the roles (#2470): the outreach columns are an org
  // admin's, and Organization appears once the list spans several real
  // organizations (the Unassigned entry is not one).
  const extraColumns = useMemo(() => columnsFor({
    outreach: canAdministerOrganizations,
    multiOrg: filterOptions.orgs.filter((org) => org.value !== '__unassigned__').length > 1,
  }), [canAdministerOrganizations, filterOptions.orgs]);
  const show = (column: ExtraColumn) => extraColumns.includes(column);
  // A filter whose control the reader does not see is not applied: a record
  // saved under the old Foundation preset could otherwise narrow a doctor's
  // list by a contact or location filter nothing on screen shows.
  const activeFilters: ReviewFilters = useMemo(() => ({
    ...reviewFilters,
    ...(extraColumns.includes('contact') ? {} : { contact: REVIEW_FILTER_DEFAULTS.contact }),
    ...(extraColumns.includes('location') ? {} : { location: REVIEW_FILTER_DEFAULTS.location }),
  }), [reviewFilters, extraColumns]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [selectAllMode, setSelectAllMode] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [resolveDialogOpen, setResolveDialogOpen] = useState(false);
  const [resolveRun, setResolveRun] = useState<ResolveRunStatus | null>(null);
  const resolvePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (resolvePollRef.current) clearInterval(resolvePollRef.current); }, []);
  const [orgFilter, setOrgFilter] = useState(ALL_FILTER_VALUE);
  const [search, setSearch] = useState("");
  const [diseaseCategoryFilter, setDiseaseCategoryFilter] = useState(ALL_FILTER_VALUE);
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
          search,
          disease_category: diseaseCategoryFilter,
          disease: diseaseFilter,
          stage: stageFilter,
          date: dateFilter,
          ...activeFilters,
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
  }, [page, pageSize, orgFilter, search, diseaseCategoryFilter, diseaseFilter, stageFilter, dateFilter, activeFilters, ordering]);

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
    setPreferences((previous) => ({ ...previous, filters: { ...previous.filters, [name]: value } }));
  };
  const changeOrdering = (value: string) => {
    resetToFirstPage();
    setPreferences((previous) => ({ ...previous, ordering: value }));
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
          params: { org: orgFilter, search, disease_category: diseaseCategoryFilter, disease: diseaseFilter, stage: stageFilter, date: dateFilter, ...activeFilters },
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

  const handleResolveConfirm = async () => {
    if (!selectAllMode && selectedIds.size === 0) return;
    try {
      const body = selectAllMode
        ? { select_all: true }
        : { person_ids: Array.from(selectedIds) };
      const params = selectAllMode
        ? { org: orgFilter, search, disease_category: diseaseCategoryFilter, disease: diseaseFilter, stage: stageFilter, date: dateFilter, ...activeFilters }
        : undefined;
      const res = await api.post<{ run_id: string; total: number }>(
        "/v1/patient-records/bulk-resolve-source-codes/",
        body,
        { params }
      );
      const runId = res.data.run_id;
      setResolveRun({ run_id: runId, state: "pending", total: res.data.total, done: 0, resolved: 0, created: 0, skipped: 0, already_resolved: 0, errors: 0, code_details: [] });

      resolvePollRef.current = setInterval(async () => {
        try {
          const status = await api.get<ResolveRunStatus>(`/v1/resolve-runs/${runId}/`);
          setResolveRun(status.data);
          if (status.data.state === "completed" || status.data.state === "failed") {
            if (resolvePollRef.current) clearInterval(resolvePollRef.current);
            resolvePollRef.current = null;
          }
        } catch {
          if (resolvePollRef.current) clearInterval(resolvePollRef.current);
          resolvePollRef.current = null;
        }
      }, 2000);
    } catch (err) {
      setError(getErrorMessage(err, "Failed to start bulk resolve"));
      setResolveDialogOpen(false);
    }
  };

  const handleDownloadCsv = async () => {
    try {
      setDownloading(true);
      const params = selectAllMode
        ? { org: orgFilter, search, disease_category: diseaseCategoryFilter, disease: diseaseFilter, stage: stageFilter, date: dateFilter, ...activeFilters, ordering }
        : { person_ids: Array.from(selectedIds).join(",") };
      const response = await api.get<Blob>("/v1/patient-records/export-csv/", { params, responseType: "blob" });
      const disposition = String(response.headers?.["content-disposition"] ?? "");
      const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? "patients.csv";
      const url = URL.createObjectURL(response.data);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setError(null);
    } catch (err) {
      // With responseType "blob" the error body arrives as a Blob too.
      const data = (err as { response?: { data?: unknown } })?.response?.data;
      let message = "Failed to download patients";
      if (data instanceof Blob) {
        try {
          const parsed = JSON.parse(await data.text()) as { error?: string; detail?: string };
          message = parsed.error || parsed.detail || message;
        } catch { /* not JSON */ }
      } else {
        message = getErrorMessage(err, message);
      }
      setError(message);
    } finally {
      setDownloading(false);
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
  const plainHeader = (key: string, label: string) => (
    <th key={key} className="whitespace-nowrap px-4 py-3 font-medium text-muted-foreground">{label}</th>
  );
  // The column headings are the sort: a first click takes the column's
  // natural direction (newest, most, A to Z), a second reverses it.
  const sortHeader = (key: string, label: string, descendingFirst: boolean, className = '') => {
    const active = ordering.replace(/^-/, '') === key;
    const descending = ordering.startsWith('-');
    const next = active ? (descending ? key : `-${key}`) : (descendingFirst ? `-${key}` : key);
    return (
      <th key={key} aria-sort={active ? (descending ? 'descending' : 'ascending') : 'none'}
          className={`whitespace-nowrap px-4 py-3 font-medium text-muted-foreground ${className}`}>
        <button type="button" onClick={() => changeOrdering(next)} className="hover:text-foreground">
          {label}{active ? (descending ? ' ↓' : ' ↑') : ''}
        </button>
      </th>
    );
  };
  const extraHeader = (column: ExtraColumn) => {
    switch (column) {
      case 'treatment': return plainHeader('treatment', 'Latest Recorded Line');
      case 'ecog': return sortHeader('ecog', 'ECOG', false);
      case 'gaps': return sortHeader('gaps', 'Key Data Gaps', true);
      case 'freshness': return sortHeader('freshness', 'Latest Clinical Result', true);
      case 'location': return plainHeader('location', 'Location');
      case 'contact': return plainHeader('contact', 'Contact Info');
      case 'organization': return sortHeader('organization', 'Organization', false);
    }
  };
  const openPatient = (personId: number) =>
    canOpen ? () => navigate(`/patient/${personId}`) : undefined;

  const handleLogout = () => {
    void logout();
  };

  return (
    <div className="p-6">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <PageTitle className="text-2xl font-bold text-foreground">Patients</PageTitle>
        <div className="flex flex-wrap gap-2">
          {(selectedIds.size > 0 || selectAllMode) && (
            <>
              <button
                onClick={() => setDeleteDialogOpen(true)}
                className="inline-flex items-center gap-2 rounded-md border border-destructive px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10"
              >
                <Trash2 size={16} />
                Delete ({selectAllMode ? `All ${patientCount}` : selectedIds.size})
              </button>
              {canAdministerOrganizations && (
                <button
                  onClick={() => { setResolveRun(null); setResolveDialogOpen(true); }}
                  className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-foreground hover:bg-accent"
                >
                  <RefreshCw size={16} />
                  Generate OMOP ({selectAllMode ? `All ${patientCount}` : selectedIds.size})
                </button>
              )}
              <button
                onClick={() => void handleDownloadCsv()}
                disabled={downloading}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                <Download size={16} />
                {downloading ? "Downloading…" : `Download CSV (${selectAllMode ? `All ${patientCount}` : selectedIds.size})`}
              </button>
            </>
          )}
          {canCurate ? (
            <button
              onClick={() => navigate("/mappings")}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              <Globe size={16} />
              Mappings
            </button>
          ) : canViewMappings(currentUser) && (
            <button
              onClick={() => navigate("/code-mappings")}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              <Globe size={16} />
              Code Mapping
            </button>
          )}
          {canCurate && (
            <button
              onClick={() => navigate("/upload")}
              className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-foreground hover:bg-accent"
            >
              <Upload size={16} />
              Upload
            </button>
          )}
          <div className="ml-auto flex gap-2">
            {canAdministerOrganizations && (
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

      <p className="mb-4 text-xs text-muted-foreground">Click a column heading to sort. Sort and clinical filters are saved in this browser.</p>

      <div className="mb-4 rounded-lg border border-border bg-background p-4 shadow-sm">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            {canOpen ? 'Name or email' : 'Patient ID'}
            <input
              type="search"
              defaultValue={search}
              placeholder={canOpen ? 'Search, then press Enter' : 'Search by ID, then press Enter'}
              onBlur={(event) => {
                const value = event.target.value.trim();
                if (value !== search) { resetToFirstPage(); setSearch(value); }
              }}
              onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            />
          </label>
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
              <option value={ALL_FILTER_VALUE}>{currentUser?.is_staff ? 'All (including unassigned)' : 'All accessible'}</option>
              {filterOptions.orgs.map((org) => (
                <option key={org.value} value={org.value}>
                  {org.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5 text-sm font-medium text-foreground">
            Disease Category
            <select
              value={diseaseCategoryFilter}
              onChange={(e) => {
                resetToFirstPage();
                setDiseaseCategoryFilter(e.target.value);
                setDiseaseFilter(ALL_FILTER_VALUE);
              }}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground"
            >
              <option value={ALL_FILTER_VALUE}>All</option>
              {(filterOptions.disease_categories ?? []).map((cat) => (
                <option key={cat.value} value={cat.value}>
                  {cat.label}
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
              disabled={diseaseCategoryFilter === ALL_FILTER_VALUE}
              className="h-10 rounded-md border border-input bg-background px-3 text-sm font-normal text-foreground disabled:opacity-50"
            >
              <option value={ALL_FILTER_VALUE}>All</option>
              {diseaseCategoryFilter !== ALL_FILTER_VALUE &&
                (filterOptions.diseases_by_category?.[diseaseCategoryFilter] ?? []).map((disease) => (
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
            ...(show('contact') ? [['contact', 'Contact information', [['all', 'All'], ['available', 'Recorded'], ['missing', 'Not recorded']]]] : []),
          ] as [keyof ReviewFilters, string, string[][]][]).map(([name, label, options]) => (
            <label key={name} className="flex flex-col gap-1 text-sm">{label}
              <select value={reviewFilters[name]} onChange={(event) => changeReviewFilter(name, event.target.value)} className="rounded-md border border-input bg-background p-2">
                {options.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
              </select>
            </label>
          ))}
          {([['treatment', 'Treatment history'], ['biomarker', 'Subtype / biomarker'],
             ...(show('location') ? [['location', 'City / region / country']] : [])] as [keyof ReviewFilters, string][]).map(([name, label]) => (
            <label key={name} className="flex flex-col gap-1 text-sm">{label}
              <input type="search" defaultValue={reviewFilters[name]} key={`${name}-${reviewFilters[name]}`} placeholder="Search, then press Enter" onBlur={(event) => {
                if (event.target.value !== reviewFilters[name]) changeReviewFilter(name, event.target.value);
              }} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur(); }} className="rounded-md border border-input bg-background p-2" />
            </label>
          ))}
        </div>
        <p className="mt-3 text-xs text-muted-foreground">Key gaps track stage, ECOG, and genomic data. A recorded negative result counts as data. Clinical freshness uses result dates, not record update times.</p>
        <button type="button" className="mt-2 text-sm underline" onClick={() => {
          resetToFirstPage();
          setPreferences((previous) => ({ ...previous, filters: { ...REVIEW_FILTER_DEFAULTS } }));
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

      {!canOpen && (
        <p role="status" className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
          Read-only sample list: patient names and other demographics are withheld, and records cannot be opened.
        </p>
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
              {canOpen && sortHeader('name', 'Name', false, 'w-36')}
              {canOpen ? sortHeader('age', 'Age', true) : plainHeader('age', 'Age')}
              {sortHeader('disease', 'Disease', false)}
              {sortHeader('stage', 'Stage', false)}
              {sortHeader('lines', 'Num Lines', true, 'text-center')}
              {extraColumns.map(extraHeader)}
              {sortHeader('updated', 'Last Updated', true)}
              {sortHeader('unmapped_source_codes', 'Unmapped Source Codes', false, 'text-right')}
            </tr>
          </thead>
          <tbody>
            {patients.length === 0 ? (
              <tr>
                <td colSpan={(canOpen ? 9 : 8) + extraColumns.length} className="py-12 text-center text-muted-foreground">
                  {error
                    ? "Unable to load patients."
                    : patientCount === 0 &&
                  orgFilter === ALL_FILTER_VALUE &&
                  !search &&
                  diseaseFilter === ALL_FILTER_VALUE &&
                  stageFilter === ALL_FILTER_VALUE &&
                  dateFilter === ALL_FILTER_VALUE &&
                  Object.entries(activeFilters).every(([key, value]) => value === REVIEW_FILTER_DEFAULTS[key as keyof ReviewFilters])
                    ? "No patients found. Upload a CSV or FHIR file to get started."
                    : "No patients match the selected filters."}
                </td>
              </tr>
            ) : (
              patients.map((patient) => (
                <tr
                  key={patient.person_id}
                  className={`border-b border-border last:border-0 ${canOpen ? 'hover:bg-muted/30 cursor-pointer' : 'cursor-default'}`}
                  aria-disabled={canOpen ? undefined : true}
                >
                  <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectAllMode || selectedIds.has(patient.person_id)}
                      onChange={() => handleSelectOne(patient.person_id)}
                      className="h-4 w-4 rounded border-input"
                    />
                  </td>
                  <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {patient.person_id}
                  </td>
                  {canOpen && <td className="px-4 py-3 font-medium" onClick={openPatient(patient.person_id)}>
                    <span className="block w-28 truncate" title={patient.patient_name || undefined}>{patient.demographics_redacted ? 'Withheld' : patient.patient_name || 'Not recorded'}</span>
                  </td>}
                  <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {patient.demographics_redacted ? 'Withheld' : patient.age ?? 'Not recorded'}
                  </td>
                  <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {patient.disease || "Not recorded"}
                    {patient.subtype_biomarkers && <span className="mt-1 block max-w-56 truncate text-xs text-muted-foreground" title={patient.subtype_biomarkers}>{patient.subtype_biomarkers}</span>}
                  </td>
                  <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {patient.stage || "N/A"}
                  </td>
                  <td className="px-4 py-3 text-center tabular-nums" onClick={openPatient(patient.person_id)}>
                    {patient.therapy_lines_count ?? "N/A"}
                  </td>
                  {show('treatment') && <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {patient.treatment_summary ? <><span className="block max-w-56 truncate" title={patient.treatment_summary.name}>{patient.treatment_summary.name}</span>
                      <span className="block text-xs text-muted-foreground">Line {patient.treatment_summary.line} · Start: {formatDate(patient.treatment_summary.start_date)}</span>
                      <span className="block text-xs text-muted-foreground">End: {formatDate(patient.treatment_summary.end_date)}</span></> : 'Not recorded'}
                  </td>}
                  {show('ecog') && <td className="px-4 py-3 tabular-nums" onClick={openPatient(patient.person_id)}>{patient.ecog_performance_status ?? 'Not recorded'}
                    {patient.ecog_performance_status != null && <span className="block whitespace-nowrap text-xs text-muted-foreground">Assessed: {formatDate(patient.ecog_assessment_date)}</span>}
                  </td>}
                  {show('organization') && <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>{patient.organization_name || 'Not recorded'}</td>}
                  {show('location') && <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>{patient.demographics_redacted ? 'Withheld' : patient.location_summary || 'Not recorded'}</td>}
                  {show('contact') && <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>{patient.contact_available ? 'Recorded' : 'Not recorded'}</td>}
                  {show('gaps') && <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>{patient.data_gaps?.length ? patient.data_gaps.join(', ') : patient.data_gaps ? 'No key gaps' : 'Not assessed'}</td>}
                  {show('freshness') && <td className="whitespace-nowrap px-4 py-3" onClick={openPatient(patient.person_id)}>{formatDate(patient.latest_result_date)}</td>}
                  <td className="px-4 py-3" onClick={openPatient(patient.person_id)}>
                    {formatDate(patient.updated_at)}
                  </td>
                  <td className={`px-4 py-3 text-right tabular-nums${patient.unmapped_source_code_count === 0 ? ' text-red-600 dark:text-red-400' : ''}`} onClick={openPatient(patient.person_id)}>
                    {patient.unmapped_source_code_count ?? 'Not available'}
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

      {resolveDialogOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-md rounded-lg bg-background p-6 shadow-xl">
            <h2 className="text-lg font-semibold">Generate OMOP</h2>
            {!resolveRun ? (
              <>
                <p className="mt-2 text-sm text-muted-foreground">
                  Re-resolve source codes for {selectAllMode ? `all ${patientCount}` : selectedIds.size} selected patient{(selectAllMode ? patientCount : selectedIds.size) !== 1 ? "s" : ""}?
                  This will update unmapped clinical rows to approved OMOP concepts.
                </p>
                <div className="mt-6 flex justify-end gap-3">
                  <button
                    onClick={() => setResolveDialogOpen(false)}
                    className="rounded-md border border-input px-4 py-2 text-sm font-medium hover:bg-accent"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleResolveConfirm}
                    className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    Generate OMOP
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="mt-4 space-y-3">
                  <div className="flex items-center gap-2 text-sm">
                    {(resolveRun.state === "pending" || resolveRun.state === "running") && (
                      <span className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                    )}
                    <span className="capitalize">{resolveRun.state}</span>
                    <span className="text-muted-foreground">
                      &mdash; {resolveRun.done}/{resolveRun.total} patients
                    </span>
                  </div>
                  {resolveRun.total > 0 && (
                    <div className="h-2 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary transition-all"
                        style={{ width: `${Math.round((resolveRun.done / resolveRun.total) * 100)}%` }}
                      />
                    </div>
                  )}
                  {(resolveRun.state === "completed" || resolveRun.state === "failed") && (
                    <p className="text-sm text-muted-foreground">
                      Resolved {resolveRun.resolved} source code(s).
                      {resolveRun.created > 0 && ` ${resolveRun.created} clinical row(s) created.`}
                      {resolveRun.skipped > 0 && ` ${resolveRun.skipped} skipped.`}
                      {resolveRun.already_resolved > 0 && ` ${resolveRun.already_resolved} already resolved.`}
                      {resolveRun.errors > 0 && ` ${resolveRun.errors} error(s).`}
                    </p>
                  )}
                </div>
                <div className="mt-6 flex justify-end">
                  <button
                    onClick={() => { setResolveDialogOpen(false); setResolveRun(null); }}
                    disabled={resolveRun.state === "pending" || resolveRun.state === "running"}
                    className="rounded-md border border-input px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                  >
                    {resolveRun.state === "completed" || resolveRun.state === "failed" ? "Close" : "Running..."}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
