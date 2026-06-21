import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, FileText, Trash2, LogOut } from "lucide-react";
import api from "@/api/axios";
import { clearTokens } from "@/utils/oauth";

interface Patient {
  person_id: number;
  patient_name: string;
  age: number | null;
  disease: string;
  stage: string;
  updated_at: string;
}

export default function PatientList() {
  const navigate = useNavigate();
  const [patients, setPatients] = useState<Patient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const fetchPatients = async () => {
    try {
      setLoading(true);
      const response = await api.get("/patient-info/");
      setPatients(response.data);
      setError(null);
    } catch (err) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
          : undefined;
      setError(msg || "Failed to fetch patients");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    fetchPatients();
  }, []);

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
      await fetchPatients();
      setSelectedIds(new Set());
      setDeleteDialogOpen(false);
      setError(null);
    } catch (err) {
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
          : undefined;
      setError(msg || "Failed to delete patients");
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

  const isAllSelected = patients.length > 0 && selectedIds.size === patients.length;

  const handleLogout = () => {
    clearTokens();
    navigate("/login");
  };

  return (
    <div className="p-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-foreground">Patient Records</h1>
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
                  No patients found. Upload a CSV or FHIR file to get started.
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
