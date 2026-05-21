import { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, AlertCircle } from "lucide-react";
import api from "@/api/axios";
import { getActiveBranding } from "@/config/branding";
import GeneralTab from "@/components/PatientInfo/tabs/GeneralTab";
import DiseaseTab from "@/components/PatientInfo/tabs/DiseaseTab";
import TreatmentTab from "@/components/PatientInfo/tabs/TreatmentTab";
import BloodTab from "@/components/PatientInfo/tabs/BloodTab";
import LabsTab from "@/components/PatientInfo/tabs/LabsTab";
import BehaviorTab from "@/components/PatientInfo/tabs/BehaviorTab";

type SaveStatus = "idle" | "pending" | "saving" | "saved" | "error";

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

export default function PatientDetail() {
  const { personId } = useParams<{ personId: string }>();
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
   
  const [patientInfo, setPatientInfo] = useState<Record<string, unknown> | null>(null);
   
  const [editedInfo, setEditedInfo] = useState<Record<string, unknown>>({});
  const [patientName, setPatientName] = useState("");
  const [editedName, setEditedName] = useState("");
  const [activeTab, setActiveTab] = useState(0);

  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveSeqRef = useRef(0);
   
  const pendingDataRef = useRef<{ info: Record<string, unknown>; name: string } | null>(null);
   
  const editedInfoRef = useRef<Record<string, unknown>>({});
  const editedNameRef = useRef("");
  const patientNameRef = useRef("");

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
        setEditedInfo(d);

        const user = res.data.user;
        const name = user
          ? (`${user.first_name} ${user.last_name}`.trim() || user.username || `Patient ${personId}`)
          : `Patient ${personId}`;
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
      await api.patch(`/patient-info/${personId}/`, data.info);
      if (seq === saveSeqRef.current) {
        setSaveStatus("saved");
        setTimeout(() => setSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
      }
    } catch {
      if (seq === saveSeqRef.current) setSaveStatus("error");
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
          <button onClick={() => navigate("/")} className="inline-flex items-center gap-2 text-sm font-medium text-portal-brand hover:underline">
            <ArrowLeft className="h-4 w-4" /> Patient List
          </button>
        </div>
      </div>
    );
  }

  const tabLabels = ["General", getDiseaseTabLabel(), "Treatment", "Blood", "Labs", "Behavior"];
  const tabDescriptions: Record<number, string> = {
    0: "Keep patient details up to date for accurate personalisation.",
    1: "Disease-specific clinical information and genetic details.",
    2: "Therapy history, treatment lines, and planned therapies.",
    3: "Blood counts, electrolytes, coagulation, and cardiac markers.",
    4: "Chemistry panel, liver function tests, and other lab markers.",
    5: "Lifestyle, socioeconomic, and behavioural health factors.",
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
            <button
              onClick={() => navigate("/")}
              className="flex h-8 w-8 items-center justify-center rounded-full border border-border bg-[#f5f7fa] text-muted-foreground transition-colors hover:bg-[#eef0f4] hover:text-foreground"
              aria-label="Back to patient list"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="flex min-w-0 items-center gap-3">
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
                <span className="hidden shrink-0 items-center rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700 sm:inline-flex">
                  {editedInfo.disease}
                </span>
              )}
            </div>
            <div className="h-5 w-px shrink-0 bg-border" />
            <div className="shrink-0">
              <SaveStatusIndicator status={saveStatus} onRetry={doSave} />
            </div>
          </div>
        </div>
      </div>

      <div className="mx-auto max-w-5xl px-6 py-6">
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
            {activeTab === 2 && <TreatmentTab formData={editedInfo} onChange={handleFieldChange} diseaseType={getDiseaseType()} />}
            {activeTab === 3 && <BloodTab formData={editedInfo} onChange={handleFieldChange} />}
            {activeTab === 4 && <LabsTab formData={editedInfo} onChange={handleFieldChange} />}
            {activeTab === 5 && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
          </div>
        </div>
      </div>
    </div>
  );
}
