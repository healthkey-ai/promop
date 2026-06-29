import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, FileText, Trash2, LogOut, Settings } from "lucide-react";
import api from "@/api/axios";
import { clearTokens } from "@/utils/oauth";
import { useAuth } from "@/hooks/useAuth";
import { PaginationControls } from "@/components/labs/PaginationControls";
import { useLocalPagination } from "@/lib/pagination";

interface Patient {
  person_id: number;
  patient_name: string;
  age: number | null;
  organization_name?: string | null;
  organization_slug?: string | null;
  disease: string;
  stage: string;
  updated_at: string;
}

const ALL_FILTER_VALUE = "all";

interface FilterOptions {
  orgs: Array<{ value: string; label: string }>;
  diseases: string[];
  stages: string[];
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
  const navigate = useNavigate();
  const { currentUser } = useAuth();
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
  }, [page, pageSize, orgFilter, diseaseFilter, stageFilter, dateFilter]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    fetchPatients();
  }, [fetchPatients]);

  const resetToFirstPage = () => {
    setPage(1);
    setSelectedIds(new Set());
  };

  const stageOptions = useMemo(
    () => (filterOptions.stages.length > 0 ? filterOptions.stages : ["I", "II", "III", "IV"]),
    [filterOptions.stages]
  );

  const handleSelectAll = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.checked) {
      setSelectedIds(new Set(patients.map((p) => p.person_id)));
    } else {
      setSelectedIds(new Set());
    }
  };

  const handleSelectOne = (personId: number) => {
    const next = new Set(selectedIds);
    if (next.has(personId)) next.delete(personId);
    else next.add(personId);
    setSelectedIds(next);
  };

  const handleDeleteConfirm = async () => {
    try {
      setDeleting(true);
      await api.delete("/patient-info/bulk_delete/", {
        data: { person_ids: Array.from(selectedIds) },
      });
      const refreshed = await fetchPatients();
      if (!refreshed) return;
      setSelectedIds(new Set());
      setDeleteDialogOpen(false);
      setError(null);
    } catch (err) {
      setError(getErrorMessage(err, "Failed to delete patients"));
    } finally {
      setDeleting(false);
    }
  };

  const formatDate = (dateString: string) => {
    if (!dateString) return "N/A";
    try {
      return new Date(dateString).toLocaleDateString();
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
    patients.length > 0 && visibleSelectedCount === patients.length;

  const handleLogout = () => {
    clearTokens();
    navigate("/login");
  };

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">PROMOP Admin</h1>
        <div className="flex gap-2">
          {selectedIds.size > 0 && (
            <button
              onClick={() => setDeleteDialogOpen(true)}
              className="inline-flex items-center gap-2 rounded-md border border-destructive px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10"
            >
              <Trash2 size={16} />
              Delete ({selectedIds.size})
            </button>
          )}
          <button
            onClick={() => navigate("/stats")}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
          >
            Stats
          </button>
          {(currentUser?.is_staff || currentUser?.is_org_admin) && (
            <button
              onClick={() => navigate("/org-admin")}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
            >
              <Settings size={14} />
              Org Admin
            </button>
          )}
          <button
            onClick={() => navigate("/upload-csv")}
            className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-foreground hover:bg-accent"
          >
            <Upload size={16} />
            Upload CSV
          </button>
          <button
            onClick={() => navigate("/upload-fhir")}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            <FileText size={16} />
            Upload FHIR
          </button>
          {currentUser?.email && (
            <button
              onClick={() => navigate("/profile")}
              className="inline-flex items-center gap-2 rounded-md border border-input px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-accent"
            >
              {currentUser.email}
            </button>
          )}
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

      <div className="mb-3 text-sm font-medium text-muted-foreground">
        {patientCount} patient{patientCount === 1 ? "" : "s"}
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-background shadow-sm">
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
              <th className="px-4 py-3 font-medium text-muted-foreground">Name</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Age</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Disease</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Stage</th>
              <th className="px-4 py-3 font-medium text-muted-foreground">Last Updated</th>
            </tr>
          </thead>
          <tbody>
            {patients.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-12 text-center text-muted-foreground">
                  {error
                    ? "Unable to load patients."
                    : patientCount === 0 &&
                  orgFilter === ALL_FILTER_VALUE &&
                  diseaseFilter === ALL_FILTER_VALUE &&
                  stageFilter === ALL_FILTER_VALUE &&
                  dateFilter === ALL_FILTER_VALUE
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
                      checked={selectedIds.has(patient.person_id)}
                      onChange={() => handleSelectOne(patient.person_id)}
                      className="h-4 w-4 rounded border-input"
                    />
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.person_id}
                  </td>
                  <td className="px-4 py-3 font-medium" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.patient_name}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.age ?? "N/A"}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.disease || "N/A"}
                  </td>
                  <td className="px-4 py-3" onClick={() => navigate(`/patient/${patient.person_id}`)}>
                    {patient.stage || "N/A"}
                  </td>
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
            setPage(nextPage);
          }}
          onPageSizeChange={(nextPageSize) => {
            setSelectedIds(new Set());
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
              Are you sure you want to delete {selectedIds.size} patient record
              {selectedIds.size !== 1 ? "s" : ""}? This action cannot be undone.
            </p>
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
