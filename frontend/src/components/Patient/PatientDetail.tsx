import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, AlertCircle, ChevronDown, Download } from "lucide-react";
import api from "@/api/axios";
import { fetchWritableFields, LIFECYCLE, type FieldDescriptors } from "@/hooks/useWritableFields";
import { writeClinicalFact } from "@/api/clinicalFacts";
import { getActiveBranding } from "@/config/branding";
import type { User } from "@/hooks/useAuth";
import DeleteAccountDialog from "./DeleteAccountDialog";
import AllergyList from "./AllergyList";
import ImmunizationList from "./ImmunizationList";
import PatientSurveys from "./PatientSurveys";
import PatientConsents from "./PatientConsents";
import AdvanceDirectives from "./AdvanceDirectives";
import PatientMessages from "./PatientMessages";
import GeneralTab from "@/components/PatientInfo/tabs/GeneralTab";
import DiseaseTab from "@/components/PatientInfo/tabs/DiseaseTab";
import TreatmentTab from "@/components/PatientInfo/tabs/TreatmentTab";
import BloodTab from "@/components/PatientInfo/tabs/BloodTab";
import LabsTab from "@/components/PatientInfo/tabs/LabsTab";
import BehaviorTab from "@/components/PatientInfo/tabs/BehaviorTab";
import WearableTab from "@/components/PatientInfo/tabs/WearableTab";
import PatientOmopTab from "./PatientOmopTab";

type SaveStatus = "idle" | "pending" | "saving" | "saved" | "error";

function ErrorToast({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  useEffect(() => {
    const timer = setTimeout(onDismiss, 8000);
    return () => clearTimeout(timer);
  }, [onDismiss]);

  return (
    <div className="fixed bottom-6 right-6 z-50 flex max-w-sm items-start gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 shadow-lg animate-fade-in">
      <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-red-500" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-red-800">Save failed</p>
        <p className="mt-0.5 text-sm text-red-700">{message}</p>
      </div>
      <button onClick={onDismiss} className="shrink-0 text-red-400 hover:text-red-600" aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

function getInitials(name: string) {
  return name.split(" ").filter(Boolean).map((n) => n[0]).slice(0, 2).join("").toUpperCase() || "?";
}

function getAvatarBg(name: string) {
  const palette = ["#6366f1", "#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899"];
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return palette[h % palette.length];
}

function SaveStatusIndicator({ status, onRetry }: { status: SaveStatus; onRetry: () => void }) {
  if (status === "idle") return null;
  return (
    <div className="flex items-center gap-1.5 select-none animate-fade-in">
      {status === "pending" && <span className="text-[11px] text-portal-text-tertiary">Unsaved…</span>}
      {status === "saving" && (
        <>
          <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-portal-text-tertiary border-t-transparent" />
          <span className="text-[11px] text-portal-text-secondary">Saving…</span>
        </>
      )}
      {status === "saved" && (
        <>
          <Check className="h-3.5 w-3.5 text-emerald-500" strokeWidth={2.5} />
          <span className="text-[11px] font-semibold text-emerald-600">Saved</span>
        </>
      )}
      {status === "error" && (
        <>
          <AlertCircle className="h-3.5 w-3.5 text-red-500" />
          <button onClick={onRetry} className="text-[11px] font-semibold text-red-600 hover:underline">
            Save failed · Retry
          </button>
        </>
      )}
    </div>
  );
}

type PatientView = "tabs" | "settings" | "messages";

function ProfileDropdown({
  onLogout,
  initials,
  avatarBg,
  name,
  onNavigate,
}: {
  onLogout: () => void;
  initials: string;
  avatarBg: string;
  name: string;
  onNavigate: (view: PatientView) => void;
}) {
  const [open, setOpen] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <>
      <div className="relative" ref={ref}>
        <button
          onClick={() => setOpen((v) => !v)}
          className="flex shrink-0 items-center gap-1.5 rounded-full border border-border px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground"
        >
          <div
            className="flex h-5 w-5 items-center justify-center rounded-full text-[9px] font-bold text-white"
            style={{ backgroundColor: avatarBg }}
          >
            {initials}
          </div>
          <span className="max-w-[120px] truncate">{name}</span>
          <ChevronDown className="h-3 w-3" />
        </button>
        {open && (
          <div className="absolute right-0 top-full z-40 mt-1 w-48 rounded-lg border border-border bg-background py-1 shadow-lg">
            <button
              onClick={() => { setOpen(false); onNavigate("messages"); }}
              className="w-full px-4 py-2 text-left text-sm text-foreground hover:bg-muted"
            >
              Messages
            </button>
            <button
              onClick={() => { setOpen(false); onNavigate("settings"); }}
              className="w-full px-4 py-2 text-left text-sm text-foreground hover:bg-muted"
            >
              Settings
            </button>
            <div className="my-1 border-t border-border" />
            <button
              onClick={() => { setOpen(false); onLogout(); }}
              className="w-full px-4 py-2 text-left text-sm text-foreground hover:bg-muted"
            >
              Sign out
            </button>
            <button
              onClick={() => { setOpen(false); setShowDeleteDialog(true); }}
              className="w-full px-4 py-2 text-left text-sm text-red-600 hover:bg-red-50"
            >
              Delete my account
            </button>
          </div>
        )}
      </div>
      {showDeleteDialog && (
        <DeleteAccountDialog
          onClose={() => setShowDeleteDialog(false)}
          onDeleted={onLogout}
        />
      )}
    </>
  );
}

function SkeletonField() {
  return (
    <div className="space-y-1.5">
      <div className="h-3.5 w-20 animate-pulse rounded bg-muted" />
      <div className="h-9 w-full animate-pulse rounded-lg bg-muted" />
    </div>
  );
}

function PatientDetailSkeleton() {
  return (
    <div className="min-h-screen bg-[#f5f7fa]">
      <div className="flex h-14 items-center border-b border-border bg-background/90">
        <div className="mx-auto flex w-full max-w-5xl items-center gap-3 px-6">
          <div className="h-8 w-8 animate-pulse rounded-full bg-muted" />
          <div className="h-8 w-8 animate-pulse rounded-full bg-muted" />
          <div className="h-4 w-36 animate-pulse rounded bg-muted" />
          <div className="ml-1 h-5 w-10 animate-pulse rounded-full bg-muted" />
        </div>
      </div>
      <div className="mx-auto max-w-5xl px-6 py-6">
        <div className="mb-5 flex gap-6 border-b border-border pb-3">
          {["w-14", "w-28", "w-20", "w-14", "w-10", "w-18"].map((w, i) => (
            <div key={i} className={`h-4 ${w} animate-pulse rounded bg-muted`} />
          ))}
        </div>
        <div className="space-y-10 rounded-2xl bg-background px-8 py-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
          <div>
            <div className="mb-2 h-6 w-20 animate-pulse rounded bg-muted" />
            <div className="mb-7 h-3.5 w-64 animate-pulse rounded bg-muted" />
            <div className="grid grid-cols-2 gap-x-8 gap-y-5">
              {Array.from({ length: 6 }).map((_, i) => <SkeletonField key={i} />)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

interface PatientDetailProps {
  /** Override the :personId route param — used to render a fixed record (patient mode). */
  personIdOverride?: string;
  /** Patient (PHR Account Holder) mode: hides provider-only chrome (back-to-list, #id). */
  patientMode?: boolean;
  /** Logout handler shown in the header when in patient mode. */
  onLogout?: () => void;
  /** Authenticated user — needed for patient-mode tabs (Allergies, Immunizations, Settings). */
  user?: User | null;
}

export default function PatientDetail({
  personIdOverride,
  patientMode = false,
  onLogout,
  user,
}: PatientDetailProps = {}) {
  const params = useParams<{ personId: string }>();
  const personId = personIdOverride ?? params.personId;
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const [saveErrorMsg, setSaveErrorMsg] = useState<string | null>(null);

  const [patientInfo, setPatientInfo] = useState<Record<string, unknown> | null>(null);
  // Server-side baseline for change detection. A clinical edit is only written
  // when its value actually moved: the editor holds the whole record, so without
  // this every autosave would rewrite every lab as a fresh dated result.
  const patientInfoRef = useRef<Record<string, unknown> | null>(null);
   
  const [editedInfo, setEditedInfo] = useState<Record<string, unknown>>({});
  const [patientName, setPatientName] = useState("");
  const [editedName, setEditedName] = useState("");
  const [activeTab, setActiveTab] = useState(0);
  const [patientView, setPatientView] = useState<PatientView>("tabs");

  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  type InviteState = "idle" | "sending" | "sent" | "error";
  const [inviteState, setInviteState] = useState<InviteState>("idle");
  const [inviteMsg, setInviteMsg] = useState("");

  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveSeqRef = useRef(0);
   
  const pendingDataRef = useRef<{ info: Record<string, unknown>; name: string } | null>(null);
   
  const editedInfoRef = useRef<Record<string, unknown>>({});
  const editedNameRef = useRef("");
  const patientNameRef = useRef("");

  // Prevent the browser from navigating away when files are dropped outside the
  // WearableTab drop zone (browser default is to open the file as a new page).
  useEffect(() => {
    const prevent = (e: DragEvent) => { e.preventDefault(); };
    document.addEventListener('dragover', prevent);
    document.addEventListener('drop', prevent);
    return () => {
      document.removeEventListener('dragover', prevent);
      document.removeEventListener('drop', prevent);
    };
  }, []);

  useEffect(() => { editedInfoRef.current = editedInfo; }, [editedInfo]);
  useEffect(() => { editedNameRef.current = editedName; }, [editedName]);
  useEffect(() => { patientNameRef.current = patientName; }, [patientName]);
  useEffect(() => () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); }, []);

  useEffect(() => {
    if (!personId) return;
    (async () => {
      try {
        setLoading(true);
        const res = await api.get(`/patient-info/${personId}/`);
        const d = res.data.patient_info;

        if (d.ecog_performance_status != null)
          d.ecog_performance_status = String(d.ecog_performance_status);

        if (d.estrogen_receptor_status && d.progesterone_receptor_status && d.her2_status) {
          const erNeg = ["Negative", "ER-"].includes(d.estrogen_receptor_status);
          const prNeg = ["Negative", "PR-"].includes(d.progesterone_receptor_status);
          const her2Neg = ["Negative", "HER2-"].includes(d.her2_status);
          d.tnbc_status = erNeg && prNeg && her2Neg;
        }

        setPatientInfo(d);
        patientInfoRef.current = { ...d };
        setEditedInfo(d);

        const user = res.data.user;
        const name = d.patient_name
          || (user ? (user.name || user.email) : null)
          || `Patient ${personId}`;
        setPatientName(name);
        setEditedName(name);
        setFetchError(null);
      } catch (err) {
        const msg =
          err && typeof err === "object" && "response" in err
            ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
            : undefined;
        setFetchError(msg || "Failed to fetch patient information");
      } finally {
        setLoading(false);
      }
    })();
  }, [personId]);

   
  const doSave = useCallback(async () => {
    const data = pendingDataRef.current;
    if (!data || !personId) return;
    const seq = ++saveSeqRef.current;
    setSaveStatus("saving");
    try {
      // scheduleAutoSave carries the edited name alongside the field data, but
      // only data.info was ever sent — and data.info is the whole GET response,
      // which already carries the ORIGINAL patient_name. So a rename never
      // reached the server, and every autosave echoed the old name back.
      //
      // Strip it unconditionally and re-add only on a real rename. Sending the
      // server's own value back is never useful, and for a patient with no name
      // it is harmful: the rendered value is the synthesised "Patient 3542".
      const { patient_name: _echoed, ...info } = data.info as Record<string, unknown>;
      const renamed = !!data.name && data.name !== patientNameRef.current;

      // A clinical field is not a column on this record — it is the projection of
      // an OMOP fact, and PatientRecord owns no writable clinical column. Writing
      // one means writing the fact and letting derivation follow, so those edits
      // leave through writeClinicalFact rather than riding along in the PATCH,
      // which would refuse them.
      // Fail closed, and that has to mean the whole save — not just the clinical
      // half. An empty descriptor makes nothing look writable, which correctly
      // stops the OMOP writes, but it also makes the projection filter below
      // match everything: without it there is no way to tell an OMOP-mapped
      // column from one this record owns. Degrading to "PATCH the lot" is how
      // the read-only 405 comes back, so a descriptor we could not fetch is a
      // failed save, reported as one.
      let descriptors: FieldDescriptors;
      try {
        descriptors = await fetchWritableFields();
      } catch {
        throw new Error(
          'Could not load the writable-field descriptor, so the save was not '
          + 'attempted. Retry once the connection is back.',
        );
      }
      const baseline = patientInfoRef.current ?? {};
      const clinicalEdits = Object.keys(info).filter(
        (f) => descriptors[f]?.writable && info[f] !== baseline[f],
      );
      for (const field of clinicalEdits) {
        await writeClinicalFact(personId, field, descriptors[field], info[field]);
        // Advance the baseline so a later keystroke elsewhere does not re-write
        // this same value as another result.
        if (patientInfoRef.current) patientInfoRef.current[field] = info[field];
      }

      // Everything the projection still owns goes the old way — and *only* that.
      //
      // This used to drop just the writable fields and echo the rest back, on the
      // theory that an unchanged value is a no-op. It cannot work: writing the
      // OMOP fact triggers derivation, which updates the canonical column AND its
      // aliases, so by the time this PATCH is sent the payload captured before the
      // write is stale. The server reads a stale value as an attempted change to a
      // read-only field and refuses the whole request —
      //
      //   OMOP-mapped PatientRecord fields are read-only …
      //   fields: [absolute_neutrophile_count, calcium_mg_dl, egfr, …]
      //
      // — every one of them an alias the edit itself had just moved.
      //
      // Any field the descriptor knows about is OMOP-mapped and never belongs in
      // this request, whatever its kind. Lifecycle columns are dropped too: they
      // are serializer read-only, and updated_at goes stale the moment anything
      // is written.
      //
      // And only what actually CHANGED. Echoing back unchanged values is what
      // created the bug in the first place: a value the client holds is only
      // guaranteed current until the next write, and computed fields like
      // lines_of_therapy or age go stale exactly the same way an alias does. A
      // PATCH should carry the edit, not the record.
      const projectionInfo = Object.fromEntries(
        Object.entries(info).filter(
          ([f, v]) =>
            !(f in descriptors) && !LIFECYCLE.has(f) && v !== baseline[f],
        ),
      );
      // Nothing left to say is not a reason to say it: a save that only moved
      // clinical facts has already done its work through the OMOP writes above.
      if (renamed || Object.keys(projectionInfo).length > 0) {
        await api.patch(
          `/patient-info/${personId}/`,
          renamed ? { ...projectionInfo, patient_name: data.name } : projectionInfo,
        );
        for (const f of Object.keys(projectionInfo)) {
          if (patientInfoRef.current) patientInfoRef.current[f] = info[f];
        }
      }
      if (seq === saveSeqRef.current) {
        // Header name is state set once on load, so without this the rename
        // only shows up after a refetch.
        if (renamed) setPatientName(data.name);
        setSaveStatus("saved");
        setTimeout(() => setSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
      }
    } catch (err) {
      if (seq === saveSeqRef.current) {
        setSaveStatus("error");
        let detail = "An unexpected error occurred.";
        if (err && typeof err === "object" && "response" in err) {
          const resp = (err as { response?: { data?: Record<string, unknown>; status?: number } }).response;
          const data = resp?.data;
          if (data) {
            // DRF returns {field: [errors]} for validation, or {detail: "..."} / {error: "..."}
            if (typeof data.detail === "string") {
              detail = data.detail;
              // The read-only guards name the offending columns in `fields`, and
              // that list is the whole diagnosis — which field was rejected says
              // whether the value itself was refused or a derived column rode
              // along. Dropping it left the on-screen error unactionable.
              if (Array.isArray(data.fields) && data.fields.length) {
                detail += ` (${(data.fields as unknown[]).join(", ")})`;
              }
            }
            else if (typeof data.error === "string") detail = data.error;
            else {
              const fieldErrors = Object.entries(data)
                .filter(([, v]) => Array.isArray(v))
                .map(([k, v]) => `${k}: ${(v as string[]).join(", ")}`)
                .join("; ");
              if (fieldErrors) detail = fieldErrors;
            }
          } else if (resp?.status) {
            detail = `Server returned ${resp.status}.`;
          }
        } else if (err instanceof Error && err.message) {
          // Not every failure comes back from the server. A client-side refusal
          // to save carries its reason in the message, and "An unexpected error
          // occurred" would throw it away.
          detail = err.message;
        }
        setSaveErrorMsg(detail);
      }
    }
  }, [personId]);

   
  const scheduleAutoSave = useCallback((info: Record<string, unknown>, name: string) => {
    pendingDataRef.current = { info, name };
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    setSaveStatus("pending");
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      doSave();
    }, 2000);
  }, [doSave]);

   
  const handleFieldChange = useCallback((field: string, value: unknown) => {
    const base = pendingDataRef.current?.info ?? editedInfoRef.current;
    const updated = { ...base, [field]: value };

    if (["estrogen_receptor_status", "progesterone_receptor_status", "her2_status"].includes(field)) {
      const er = String(field === "estrogen_receptor_status" ? value : updated.estrogen_receptor_status ?? "");
      const pr = String(field === "progesterone_receptor_status" ? value : updated.progesterone_receptor_status ?? "");
      const her2 = String(field === "her2_status" ? value : updated.her2_status ?? "");
      const neg = (v: string) => ["Negative", "ER-", "PR-", "HER2-"].includes(v);
      if (neg(er) && neg(pr) && neg(her2)) updated.tnbc_status = true;
      else if (er || pr || her2) updated.tnbc_status = false;
    }

    setEditedInfo(updated);
    scheduleAutoSave(updated, editedNameRef.current);
  }, [scheduleAutoSave]);

  const handleNameChange = useCallback((name: string) => {
    setEditedName(name);
    scheduleAutoSave(pendingDataRef.current?.info ?? editedInfoRef.current, name);
  }, [scheduleAutoSave]);

  const handleInvite = useCallback(async () => {
    if (!personId) return;
    setInviteState("sending");
    setInviteMsg("");
    try {
      const email = editedInfoRef.current?.email;
      const body = typeof email === "string" && email.trim() ? { email: email.trim() } : {};
      const res = await api.post(`/v1/patients/${personId}/invite/`, body);
      setInviteState("sent");
      setInviteMsg(
        res.data?.email_warning
          ? res.data.email_warning
          : `Invitation sent to ${res.data?.email ?? "the patient"}.`
      );
    } catch (err) {
      setInviteState("error");
      const msg =
        err && typeof err === "object" && "response" in err
          ? (err as { response?: { data?: { error?: string } } }).response?.data?.error
          : undefined;
      setInviteMsg(msg || "Could not send invitation.");
    }
  }, [personId]);

  const handleMutationAdd = useCallback(() => {
    const raw = pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
    const m = [...(raw as { gene: string; mutation: string; origin: string; interpretation: string }[])];
    m.push({ gene: "", mutation: "", origin: "", interpretation: "" });
    handleFieldChange("genetic_mutations", m);
  }, [handleFieldChange]);

  const handleMutationRemove = useCallback((i: number) => {
    const raw = pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
    const m = [...(raw as { gene: string; mutation: string; origin: string; interpretation: string }[])];
    m.splice(i, 1);
    handleFieldChange("genetic_mutations", m);
  }, [handleFieldChange]);

  const handleMutationChange = useCallback((i: number, field: string, value: string) => {
    const raw = pendingDataRef.current?.info?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
    const m = [...(raw as { gene: string; mutation: string; origin: string; interpretation: string }[])];
    m[i] = { ...m[i], [field]: value };
    if (field === "gene") m[i].mutation = "";
    handleFieldChange("genetic_mutations", m);
  }, [handleFieldChange]);

  const handleZipcodeChange = useCallback(async (zipcode: string) => {
    handleFieldChange("postal_code", zipcode);
    if (zipcode.length === 5 && /^\d{5}$/.test(zipcode)) {
      try {
        const res = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
        if (res.ok) {
          const data = await res.json();
          if (data.places?.length > 0) {
            const place = data.places[0];
             
            setEditedInfo((prev: Record<string, unknown>) => {
              const updated = { ...prev, city: place["place name"], region: place["state"] };
              pendingDataRef.current = { info: updated, name: editedNameRef.current };
              return updated;
            });
          }
        }
      } catch { /* ignore zip lookup failures */ }
    }
  }, [handleFieldChange]);

  const handleDownloadFhir = useCallback(async () => {
    if (!personId) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await api.get(`/v1/patient-records/${personId}/export-fhir/`);
      const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: "application/fhir+json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "my-health-record.fhir.json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      setDownloadError("Failed to download your health record. Please try again.");
    } finally {
      setDownloading(false);
    }
  }, [personId]);

  const getDiseaseType = (): "breast" | "lymphoma" | "myeloma" | "cll" | "other" => {
    const d = (typeof editedInfo?.disease === "string" ? editedInfo.disease : "").toLowerCase();
    if (d.includes("breast")) return "breast";
    if (d.includes("lymphoma")) return "lymphoma";
    if (d.includes("myeloma")) return "myeloma";
    if (d.includes("cll") || d.includes("chronic lymphocytic")) return "cll";
    return "other";
  };

  const getDiseaseTabLabel = () =>
    ({ breast: "Breast Cancer", lymphoma: "Follicular Lymphoma", myeloma: "Multiple Myeloma", cll: "CLL", other: "Disease Specific" })[getDiseaseType()];

  if (loading) return <PatientDetailSkeleton />;

  if (fetchError && !patientInfo) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[#f5f7fa] p-6">
        <div className="w-full max-w-sm rounded-2xl bg-background p-8 text-center shadow">
          <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-400" />
          <p className="mb-6 text-sm text-red-700">{fetchError}</p>
          {patientMode ? (
            <button onClick={() => (onLogout ? onLogout() : navigate("/login"))} className="inline-flex items-center gap-2 text-sm font-medium text-portal-brand hover:underline">
              Sign out
            </button>
          ) : (
            <button onClick={() => navigate("/")} className="inline-flex items-center gap-2 text-sm font-medium text-portal-brand hover:underline">
              <ArrowLeft className="h-4 w-4" /> Patient List
            </button>
          )}
        </div>
      </div>
    );
  }

  // Build tab list dynamically — patient mode adds Allergies (after Labs) and Surveys (last).
  // Immunizations are shown inside the Treatment tab, not as a separate tab.
  const canViewOmop = !patientMode && !!(user?.is_staff || user?.is_org_admin);
  const coreTabs = ["General", getDiseaseTabLabel(), "Treatment", "Blood", "Labs"];
  const afterLabsTabs = patientMode ? ["Allergies"] : [];
  const trailingTabs = ["Behavior", "Wearables"];
  const surveyTabs = patientMode ? ["Surveys"] : [];
  const adminTabs = canViewOmop ? ["OMOP"] : [];
  const tabLabels = [...coreTabs, ...afterLabsTabs, ...trailingTabs, ...surveyTabs, ...adminTabs];

  // Compute dynamic indices
  const allergiesIdx = patientMode ? coreTabs.length : -1;
  const behaviorIdx = coreTabs.length + afterLabsTabs.length;
  const wearablesIdx = behaviorIdx + 1;
  const surveysIdx = patientMode ? wearablesIdx + 1 : -1;
  const omopIdx = canViewOmop ? tabLabels.length - 1 : -1;

  const tabDescriptions: Record<number, string> = {
    0: "Keep patient details up to date for accurate personalisation.",
    1: "Disease-specific clinical information and genetic details.",
    2: "Therapy history, treatment lines, and planned therapies.",
    3: "Blood counts, electrolytes, coagulation, and cardiac markers.",
    4: "Chemistry panel, liver function tests, and other lab markers.",
    ...(allergiesIdx >= 0 ? { [allergiesIdx]: "Known allergies and intolerances from your health records." } : {}),
    [behaviorIdx]: "Lifestyle, socioeconomic, and behavioural health factors.",
    [wearablesIdx]: "30 day summaries derived from synced OMOP data.",
    ...(surveysIdx >= 0 ? { [surveysIdx]: "Surveys assigned to you by your care team." } : {}),
    ...(omopIdx >= 0 ? { [omopIdx]: "Raw OMOP rows associated with this patient." } : {}),
  };

  const initials = getInitials(patientName);
  const avatarBg = getAvatarBg(patientName);
  const branding = getActiveBranding();

  return (
    <div className="min-h-screen bg-[#f5f7fa]">
      <div className="sticky top-0 z-20 border-b border-border bg-background/90 shadow-[0_1px_3px_rgba(0,0,0,0.05)] backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between gap-4 px-6">
          <div className="flex shrink-0 items-center gap-3">
            {branding.logoUrl && (
              <>
                <img src={branding.logoUrl} alt={branding.appName} className="h-6 w-auto" />
                <div className="h-4 w-px bg-border" />
              </>
            )}
            {!branding.logoUrl && branding.appName && (
              <>
                <span className="text-sm font-bold tracking-tight text-portal-brand">{branding.appName}</span>
                <div className="h-4 w-px bg-border" />
              </>
            )}
            {!patientMode && (
              <button
                onClick={() => navigate("/")}
                className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-[#f5f7fa] text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground"
                aria-label="Back to patient list"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
              </button>
            )}
            {patientMode && (
              <div className="flex items-center gap-3">
                <span className="text-sm font-semibold text-foreground">My Health Record</span>
                <button
                  onClick={handleDownloadFhir}
                  disabled={downloading}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Download className="h-3.5 w-3.5" />
                  {downloading ? "Downloading..." : "Download"}
                </button>
              </div>
            )}
          </div>

          <div className="flex min-w-0 items-center gap-3">
            {!patientMode && (
              <>
                <div
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-bold text-white shadow-sm ring-2 ring-background"
                  style={{ backgroundColor: avatarBg }}
                >
                  {initials}
                </div>
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-semibold text-foreground">{patientName}</span>
                  <span className="inline-flex shrink-0 items-center rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-indigo-600">
                    #{personId}
                  </span>
                  {typeof editedInfo?.disease === "string" && editedInfo.disease && (
                    <span className="inline-flex shrink-0 items-center rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                      {editedInfo.disease}
                    </span>
                  )}
                </div>
              </>
            )}
            <div className="h-5 w-px shrink-0 bg-border" />
            <div className="shrink-0">
              <SaveStatusIndicator status={saveStatus} onRetry={doSave} />
            </div>
            {!patientMode && (
              <div className="flex shrink-0 items-center gap-2">
                <button
                  onClick={handleInvite}
                  disabled={inviteState === "sending"}
                  title="Email this patient a link to create their portal account"
                  className="shrink-0 rounded-full border border-border px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground disabled:opacity-50"
                >
                  {inviteState === "sending" ? "Inviting…" : "Invite to portal"}
                </button>
                {inviteMsg && (
                  <span
                    className={[
                      "text-[11px]",
                      inviteState === "error" ? "text-red-600" : "text-emerald-600",
                    ].join(" ")}
                  >
                    {inviteMsg}
                  </span>
                )}
              </div>
            )}
            {patientMode && onLogout && (
              <ProfileDropdown
                onLogout={onLogout}
                initials={initials}
                avatarBg={avatarBg}
                name={patientName}
                onNavigate={setPatientView}
              />
            )}
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-5xl px-6 py-6">
        {patientView === "tabs" && (
          <>
            <div className="mb-5 border-b border-border">
              <nav className="flex gap-x-1 overflow-x-auto" aria-label="Patient info tabs">
                {tabLabels.map((label, i) => (
                  <button
                    key={i}
                    onClick={() => setActiveTab(i)}
                    className={[
                      "whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium -mb-px transition-colors focus:outline-none",
                      activeTab === i
                        ? "border-portal-brand text-portal-brand"
                        : "border-transparent text-muted-foreground hover:text-foreground",
                    ].join(" ")}
                  >
                    {label}
                  </button>
                ))}
              </nav>
            </div>

            <div className="rounded-2xl bg-background shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
              <div className="px-8 pb-6 pt-8">
                <h2 className="text-xl font-bold text-foreground">{tabLabels[activeTab]}</h2>
                <p className="mt-1 text-sm text-muted-foreground">{tabDescriptions[activeTab]}</p>
              </div>

              <div key={activeTab} className="animate-tab-in px-8 pb-10">
                {activeTab === 0 && (
                  <GeneralTab
                    formData={editedInfo}
                    onChange={handleFieldChange}
                    editedName={editedName}
                    onNameChange={handleNameChange}
                    onZipcodeChange={handleZipcodeChange}
                    diseaseType={getDiseaseType()}
                  />
                )}
                {activeTab === 1 && (
                  <DiseaseTab
                    formData={editedInfo}
                    onChange={handleFieldChange}
                    onMutationAdd={handleMutationAdd}
                    onMutationRemove={handleMutationRemove}
                    onMutationChange={handleMutationChange}
                    diseaseType={getDiseaseType()}
                  />
                )}
                {activeTab === 2 && (
                  <>
                    <TreatmentTab formData={editedInfo} onChange={handleFieldChange} diseaseType={getDiseaseType()} />
                    {patientMode && (
                      <div className="mt-8 border-t border-gray-200 pt-6">
                        <ImmunizationList user={user ?? null} />
                      </div>
                    )}
                  </>
                )}
                {activeTab === 3 && <BloodTab formData={editedInfo} onChange={handleFieldChange} />}
                {activeTab === 4 && <LabsTab formData={editedInfo} onChange={handleFieldChange} />}
                {allergiesIdx >= 0 && activeTab === allergiesIdx && <AllergyList user={user ?? null} />}
                {activeTab === behaviorIdx && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
                {activeTab === wearablesIdx && <WearableTab formData={editedInfo} onChange={handleFieldChange} onRefresh={() => { if (personId) { api.get(`/patient-info/${personId}/`).then(res => { const d = res.data.patient_info; setPatientInfo(d); setEditedInfo(d); }).catch(() => {}); } }} />}
                {surveysIdx >= 0 && activeTab === surveysIdx && <PatientSurveys user={user ?? null} />}
                {omopIdx >= 0 && activeTab === omopIdx && personId && <PatientOmopTab personId={personId} />}
              </div>
            </div>
          </>
        )}

        {patientView === "settings" && (
          <div className="animate-tab-in">
            <button
              onClick={() => setPatientView("tabs")}
              className="mb-4 inline-flex items-center gap-1.5 text-sm font-medium text-portal-brand hover:underline"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Back to Health Record
            </button>
            <div className="space-y-6">
              <PatientConsents user={user ?? null} />
              <AdvanceDirectives user={user ?? null} />
            </div>
          </div>
        )}

        {patientView === "messages" && (
          <div className="animate-tab-in">
            <button
              onClick={() => setPatientView("tabs")}
              className="mb-4 inline-flex items-center gap-1.5 text-sm font-medium text-portal-brand hover:underline"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Back to Health Record
            </button>
            <PatientMessages user={user ?? null} />
          </div>
        )}
      </div>

      {saveErrorMsg && (
        <ErrorToast message={saveErrorMsg} onDismiss={() => setSaveErrorMsg(null)} />
      )}
      {downloadError && (
        <ErrorToast message={downloadError} onDismiss={() => setDownloadError(null)} />
      )}
    </div>
  );
}
