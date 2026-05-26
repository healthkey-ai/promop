import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Check, AlertCircle } from "lucide-react";

import { PatientInfoProvider } from "./PatientInfoProvider";
import { usePatientInfoMe, usePatchPatientInfo } from "./patientInfoHooks";
import type { PatientInfoProps } from "./patientInfoTypes";
import GeneralTab from "@/components/PatientInfo/tabs/GeneralTab";
import DiseaseTab from "@/components/PatientInfo/tabs/DiseaseTab";
import TreatmentTab from "@/components/PatientInfo/tabs/TreatmentTab";
import BloodTab from "@/components/PatientInfo/tabs/BloodTab";
import LabsTab from "@/components/PatientInfo/tabs/LabsTab";
import BehaviorTab from "@/components/PatientInfo/tabs/BehaviorTab";

type SaveStatus = "idle" | "pending" | "saving" | "saved" | "error";

function SaveStatusIndicator({ status, onRetry }: { status: SaveStatus; onRetry: () => void }) {
  if (status === "idle") return null;
  return (
    <div className="flex items-center gap-1.5 select-none">
      {status === "pending" && <span className="text-[11px] text-muted-foreground">Unsaved…</span>}
      {status === "saving" && (
        <>
          <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
          <span className="text-[11px] text-muted-foreground">Saving…</span>
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

function PatientInfoSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex gap-6 border-b border-border pb-3">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div key={i} className="h-4 w-16 animate-pulse rounded bg-muted" />
        ))}
      </div>
      <div className="space-y-10 rounded-2xl bg-background px-8 py-8 shadow-sm">
        <div>
          <div className="mb-2 h-6 w-20 animate-pulse rounded bg-muted" />
          <div className="mb-7 h-3.5 w-64 animate-pulse rounded bg-muted" />
          <div className="grid grid-cols-2 gap-x-8 gap-y-5">
            {Array.from({ length: 6 }).map((_, i) => <SkeletonField key={i} />)}
          </div>
        </div>
      </div>
    </div>
  );
}

function PatientInfoInner({ readOnly, onPatientUpdated }: Pick<PatientInfoProps, "readOnly" | "onPatientUpdated">) {
  const { data, isLoading, isError, error } = usePatientInfoMe();
  const patchMutation = usePatchPatientInfo();

  const initialInfo = useMemo(() => {
    if (!data) return {};
    const d = { ...data.patient_info };
    if (d.ecog_performance_status != null)
      d.ecog_performance_status = String(d.ecog_performance_status);
    if (d.estrogen_receptor_status && d.progesterone_receptor_status && d.her2_status) {
      const erNeg = ["Negative", "ER-"].includes(String(d.estrogen_receptor_status));
      const prNeg = ["Negative", "PR-"].includes(String(d.progesterone_receptor_status));
      const her2Neg = ["Negative", "HER2-"].includes(String(d.her2_status));
      d.tnbc_status = erNeg && prNeg && her2Neg;
    }
    return d;
  }, [data]);

  const initialName = useMemo(() => {
    if (!data) return "";
    return data.patient_name || data.user?.name || data.user?.email || "Patient";
  }, [data]);

  const [editedInfo, setEditedInfo] = useState<Record<string, unknown>>(initialInfo);
  const [editedName, setEditedName] = useState(initialName);
  const [activeTab, setActiveTab] = useState(0);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");

  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingDataRef = useRef<Record<string, unknown> | null>(null);
  const editedInfoRef = useRef<Record<string, unknown>>({});
  const editedNameRef = useRef("");
  const [prevData, setPrevData] = useState(data);

  if (data && data !== prevData) {
    setPrevData(data);
    setEditedInfo(initialInfo);
    setEditedName(initialName);
  }

  useEffect(() => { editedInfoRef.current = editedInfo; }, [editedInfo]);
  useEffect(() => { editedNameRef.current = editedName; }, [editedName]);
  useEffect(() => () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); }, []);

  const doSave = useCallback(() => {
    const info = pendingDataRef.current;
    if (!info || readOnly) return;
    setSaveStatus("saving");
    patchMutation.mutate(info, {
      onSuccess: (result) => {
        setSaveStatus("saved");
        onPatientUpdated?.(result);
        setTimeout(() => setSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
      },
      onError: () => setSaveStatus("error"),
    });
  }, [patchMutation, readOnly, onPatientUpdated]);

  const scheduleAutoSave = useCallback((info: Record<string, unknown>) => {
    pendingDataRef.current = info;
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    setSaveStatus("pending");
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      doSave();
    }, 2000);
  }, [doSave]);

  const handleFieldChange = useCallback((field: string, value: unknown) => {
    if (readOnly) return;
    const base = pendingDataRef.current ?? editedInfoRef.current;
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
    scheduleAutoSave(updated);
  }, [scheduleAutoSave, readOnly]);

  const handleNameChange = useCallback((name: string) => {
    if (readOnly) return;
    setEditedName(name);
    const base = pendingDataRef.current ?? editedInfoRef.current;
    scheduleAutoSave({ ...base, patient_name: name });
  }, [scheduleAutoSave, readOnly]);

  const handleMutationAdd = useCallback(() => {
    const raw = pendingDataRef.current?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
    const m = [...(raw as { gene: string; mutation: string; origin: string; interpretation: string }[])];
    m.push({ gene: "", mutation: "", origin: "", interpretation: "" });
    handleFieldChange("genetic_mutations", m);
  }, [handleFieldChange]);

  const handleMutationRemove = useCallback((i: number) => {
    const raw = pendingDataRef.current?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
    const m = [...(raw as { gene: string; mutation: string; origin: string; interpretation: string }[])];
    m.splice(i, 1);
    handleFieldChange("genetic_mutations", m);
  }, [handleFieldChange]);

  const handleMutationChange = useCallback((i: number, field: string, value: string) => {
    const raw = pendingDataRef.current?.genetic_mutations ?? editedInfoRef.current?.genetic_mutations ?? [];
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
          const zipData = await res.json();
          if (zipData.places?.length > 0) {
            const place = zipData.places[0];
            setEditedInfo((prev) => {
              const updated = { ...prev, city: place["place name"], region: place["state"] };
              pendingDataRef.current = updated;
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

  if (isLoading) return <PatientInfoSkeleton />;

  if (isError) {
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-sm">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-400" />
        <p className="text-sm text-red-700">
          {(error as { response?: { data?: { error?: string } } })?.response?.data?.error || "Failed to load patient information"}
        </p>
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

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-foreground">Patient Info</h1>
        <SaveStatusIndicator status={saveStatus} onRetry={doSave} />
      </div>

      <div className="border-b border-border">
        <nav className="flex gap-x-1 overflow-x-auto" aria-label="Patient info tabs">
          {tabLabels.map((label, i) => (
            <button
              key={i}
              onClick={() => setActiveTab(i)}
              className={[
                "whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium -mb-px transition-colors focus:outline-none",
                activeTab === i
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              ].join(" ")}
            >
              {label}
            </button>
          ))}
        </nav>
      </div>

      <div className="rounded-2xl bg-background shadow-sm">
        <div className="px-8 pb-6 pt-8">
          <h2 className="text-xl font-bold text-foreground">{tabLabels[activeTab]}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{tabDescriptions[activeTab]}</p>
        </div>

        <div className="px-8 pb-10">
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
  );
}

export function PatientInfo({
  apiClient,
  apiBasePath,
  queryClient,
  className,
  theme,
  readOnly,
  onPatientUpdated,
}: PatientInfoProps) {
  return (
    <PatientInfoProvider
      apiClient={apiClient}
      apiBasePath={apiBasePath}
      queryClient={queryClient}
      theme={theme}
      className={className}
    >
      <PatientInfoInner readOnly={readOnly} onPatientUpdated={onPatientUpdated} />
    </PatientInfoProvider>
  );
}

export default PatientInfo;
