import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Check, AlertCircle } from "lucide-react";

import { PatientInfoProvider } from "./PatientInfoProvider";
import { usePatientInfoMe, usePatchPatientInfo } from "./patientInfoHooks";
import { useCommitConditionsAnswer } from './patientInfoApi';
import type { PatientInfoProps } from "./patientInfoTypes";
import { fetchWritableFields, LIFECYCLE, type FieldDescriptors } from "@/hooks/useWritableFields";
// Profile fields now write through PatientRecord PATCH alongside clinical fields.
import GeneralTab from "@/components/PatientInfo/tabs/GeneralTab";
import DiseaseTab from "@/components/PatientInfo/tabs/DiseaseTab";
import GenomicsTab from "@/components/PatientInfo/tabs/GenomicsTab";
import TreatmentTab from "@/components/PatientInfo/tabs/TreatmentTab";
import LabsTab from "@/components/PatientInfo/tabs/LabsTab";
import BehaviorTab from "@/components/PatientInfo/tabs/BehaviorTab";
import WearableTab from "@/components/PatientInfo/tabs/WearableTab";
import ClinicalSummaryTab from "@/components/PatientInfo/tabs/ClinicalSummaryTab";

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
        {[1, 2, 3, 4, 5, 6, 7].map((i) => (
          <div key={i} className="h-4 w-16 animate-pulse rounded bg-muted" />
        ))}
      </div>
      <div className="space-y-10 pt-2">
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

function PatientInfoInner({ readOnly, federated, onPatientUpdated, showHeading = true }: Pick<PatientInfoProps, "readOnly" | "federated" | "onPatientUpdated" | "showHeading">) {
  const { data, isLoading, isError, error } = usePatientInfoMe();
  const patchMutation = usePatchPatientInfo();
  const commitConditionsAnswer = useCommitConditionsAnswer();

  const initialInfo = useMemo(() => {
    if (!data) return {};
    const d = { ...data.patient_info };
    if (d.ecog_performance_status != null)
      d.ecog_performance_status = String(d.ecog_performance_status);
    if (d.estrogen_receptor_status && d.progesterone_receptor_status && d.her2_status) {
      const erNeg = String(d.estrogen_receptor_status) === "Negative";
      const prNeg = String(d.progesterone_receptor_status) === "Negative";
      const her2Neg = String(d.her2_status) === "Negative";
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

  // The CB header links to `/info?tab=<key>` (the same keys its native form
  // understands). The widget owns its own tabs, so consume the parameter once
  // instead of always landing on General. Disease keys all select the one
  // disease tab; `blood` is the native name for the merged Labs tab.
  const deepLinkAppliedRef = useRef(false);
  useEffect(() => {
    if (deepLinkAppliedRef.current || !data) return;
    deepLinkAppliedRef.current = true;
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (!requested) return;
    const disease = String(data.patient_info?.disease ?? "").toLowerCase();
    const diseaseTab = disease.includes("breast") || disease.includes("lymphoma")
      || disease.includes("myeloma") || disease.includes("cll")
      || disease.includes("chronic lymphocytic");
    const keys = [
      "general",
      ...(diseaseTab ? ["disease"] : []),
      "treatment", "labs", "behavior",
      ...(federated ? [] : ["wearable"]),
      "genomics", "history",
    ];
    const aliases: Record<string, string> = {
      blood: "labs",
      breast_cancer: "disease",
      multiple_myeloma: "disease",
      follicular_lymphoma: "disease",
      chronic_lymphocytic_leukemia: "disease",
      mantle_cell_lymphoma: "disease",
    };
    const key = aliases[requested] ?? requested;
    const index = keys.indexOf(key);
    if (index >= 0) setActiveTab(index);
  }, [data, federated]);

  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingDataRef = useRef<Record<string, unknown> | null>(null);
  const dirtyFieldsRef = useRef(new Set<string>());
  const serverInfoRef = useRef<Record<string, unknown>>(initialInfo);
  const editedInfoRef = useRef<Record<string, unknown>>({});
  const editedNameRef = useRef("");
  const [prevData, setPrevData] = useState(data);

  if (data && data !== prevData) {
    setPrevData(data);
    const pending = pendingDataRef.current;
    const rebased = { ...initialInfo };
    for (const field of dirtyFieldsRef.current) {
      if (field === 'patient_name') continue;
      if (pending && pending[field] !== initialInfo[field]) rebased[field] = pending[field];
      else dirtyFieldsRef.current.delete(field);
    }
    if (pending && ['estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status']
      .some((field) => dirtyFieldsRef.current.has(field))) {
      rebased.tnbc_status = pending.tnbc_status;
    }
    const pendingName = dirtyFieldsRef.current.has('patient_name')
      ? pending?.patient_name : initialName;
    pendingDataRef.current = pending
      ? { ...rebased, ...(dirtyFieldsRef.current.has('patient_name') ? { patient_name: pendingName } : {}) }
      : null;
    serverInfoRef.current = { ...initialInfo };
    setEditedInfo(rebased);
    setEditedName(String(pendingName ?? initialName));
  }

  useEffect(() => { editedInfoRef.current = editedInfo; }, [editedInfo]);
  useEffect(() => { editedNameRef.current = editedName; }, [editedName]);
  useEffect(() => () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); }, []);

  // The last state the server confirmed, so an edit can be told from a value that
  // merely came back in the payload. Advanced as each write lands.
  /**
   * PatientRecord is a derived read model with no writable clinical columns, so
   * this cannot be "PATCH whatever changed" — it used to send the whole record,
   * which meant any edit on the Blood or Labs tab was a write to an OMOP-mapped
   * column and came back 405:
   *
   *   OMOP-mapped PatientRecord fields are read-only. Write a complete clinical
   *   fact to the appropriate OMOP resource, then rederive the record.
   *
   * Same split as the provider editor: clinical values leave as OMOP facts and
   * derivation follows; only what the projection genuinely owns rides in the
   * PATCH. This view renders the very same tab components, so it needs the very
   * same write path.
   */
  const doSave = useCallback(async () => {
    const info = pendingDataRef.current;
    if (!info || readOnly) return;
    setSaveStatus("saving");
    try {
      // Fail closed, over the whole save. An empty descriptor stops the OMOP
      // writes correctly, but it also makes the projection filter below match
      // everything — and PATCHing the lot is exactly the 405 above.
      let descriptors: FieldDescriptors;
      try {
        descriptors = await fetchWritableFields();
      } catch {
        throw new Error(
          "Could not load the writable-field descriptor, so the save was not "
          + "attempted. Retry once the connection is back.",
        );
      }

      const baseline = serverInfoRef.current;
      const personId = baseline.person_id ?? data?.patient_info?.person_id;

      // All fields — clinical and profile — go through PatientRecord PATCH.
      // The backend projects profile fields to Person/Location and clinical
      // fields to OMOP tables after the PATCH lands.
      const patchFields: Record<string, unknown> = {};

      if (personId) {
        for (const [f, v] of Object.entries(info)) {
          if (f === 'no_pre_existing_conditions' || f === 'preexisting_conditions') continue;
          if (f === "patient_name" || LIFECYCLE.has(f) || v === baseline[f]) continue;
          const desc = descriptors[f];
          if (desc?.writable && desc.target === 'patient_record') {
            patchFields[f] = v;
          } else if (!(f in descriptors)) {
            patchFields[f] = v;
          }
        }
      }

      const renamed = typeof info.patient_name === "string";
      const combined = { ...patchFields };
      const payload = renamed
        ? { ...combined, patient_name: info.patient_name }
        : combined;

      const clearUnsendableEdits = () => {
        for (const field of dirtyFieldsRef.current) {
          const descriptor = descriptors[field];
          if (descriptor && (!descriptor.writable || descriptor.target !== 'patient_record')
              && info[field] === pendingDataRef.current?.[field]) {
            dirtyFieldsRef.current.delete(field);
          }
        }
      };

      // Nothing left to say is not a reason to say it: the OMOP writes above have
      // already done the work, and an empty PATCH can only fail.
      if (Object.keys(payload).length === 0) {
        clearUnsendableEdits();
        setSaveStatus("saved");
        setTimeout(() => setSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
        return;
      }

      const result = await patchMutation.mutateAsync(payload);
      for (const f of Object.keys(combined)) {
        serverInfoRef.current[f] = info[f];
        if (pendingDataRef.current?.[f] === info[f]) dirtyFieldsRef.current.delete(f);
      }
      if (renamed && pendingDataRef.current?.patient_name === info.patient_name) {
        dirtyFieldsRef.current.delete('patient_name');
      }
      clearUnsendableEdits();
      setSaveStatus("saved");
      onPatientUpdated?.(result);
      setTimeout(() => setSaveStatus((s) => (s === "saved" ? "idle" : s)), 1200);
    } catch {
      setSaveStatus("error");
    }
  }, [patchMutation, readOnly, onPatientUpdated, data]);

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
    if (value === serverInfoRef.current[field]) dirtyFieldsRef.current.delete(field);
    else dirtyFieldsRef.current.add(field);

    if (["estrogen_receptor_status", "progesterone_receptor_status", "her2_status"].includes(field)) {
      const er = String(field === "estrogen_receptor_status" ? value : updated.estrogen_receptor_status ?? "");
      const pr = String(field === "progesterone_receptor_status" ? value : updated.progesterone_receptor_status ?? "");
      const her2 = String(field === "her2_status" ? value : updated.her2_status ?? "");
      const neg = (v: string) => v === "Negative";
      if (neg(er) && neg(pr) && neg(her2)) updated.tnbc_status = true;
      else if (er || pr || her2) updated.tnbc_status = false;
    }

    setEditedInfo(updated);
    scheduleAutoSave(updated);
  }, [scheduleAutoSave, readOnly]);

  const handleNameChange = useCallback((name: string) => {
    if (readOnly) return;
    setEditedName(name);
    if (name === initialName) dirtyFieldsRef.current.delete('patient_name');
    else dirtyFieldsRef.current.add('patient_name');
    const base = pendingDataRef.current ?? editedInfoRef.current;
    scheduleAutoSave({ ...base, patient_name: name });
  }, [scheduleAutoSave, readOnly, initialName]);



  const handleZipcodeChange = useCallback(async (zipcode: string) => {
    handleFieldChange("postal_code", zipcode);
    if (zipcode.length === 5 && /^\d{5}$/.test(zipcode)) {
      try {
        const res = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
        if (res.ok) {
          const zipData = await res.json();
          if (zipData.places?.length > 0) {
            const place = zipData.places[0];
            // `state` is the full name ("Massachusetts"); `region` maps to OMOP
            // Location.state, which the CDM caps at two characters, so writing
            // the full name fails the save with a 400. Take the abbreviation the
            // lookup already returns, and leave the field alone if it is absent
            // rather than storing a value the API will refuse.
            const abbreviation = place["state abbreviation"];
            setEditedInfo((prev) => {
              const updated: Record<string, unknown> = {
                ...prev,
                city: place["place name"],
                ...(abbreviation ? { region: abbreviation } : {}),
              };
              pendingDataRef.current = updated;
              for (const field of ['city', 'region']) {
                if (updated[field] === serverInfoRef.current[field]) dirtyFieldsRef.current.delete(field);
                else dirtyFieldsRef.current.add(field);
              }
              return updated;
            });
          }
        }
      } catch { /* ignore zip lookup failures */ }
    }
  }, [handleFieldChange]);

  const getDiseaseType = (): "breast" | "lymphoma" | "myeloma" | "cll" | "mcl" | "other" => {
    const d = (typeof editedInfo?.disease === "string" ? editedInfo.disease : "").toLowerCase();
    if (d.includes("breast")) return "breast";
    if (d.includes("mantle")) return "mcl";
    if (d.includes("lymphoma")) return "lymphoma";
    if (d.includes("myeloma")) return "myeloma";
    if (d.includes("cll") || d.includes("chronic lymphocytic")) return "cll";
    return "other";
  };

  const getDiseaseTabLabel = () =>
    ({ breast: "Breast Cancer", lymphoma: "Follicular Lymphoma", myeloma: "Multiple Myeloma", cll: "CLL", mcl: "Mantle Cell Lymphoma", other: "Disease Specific" })[getDiseaseType()];

  if (isLoading) return <PatientInfoSkeleton />;

  if (isError) {
    const httpStatus = (error as { response?: { status?: number } })?.response?.status;
    if (httpStatus === 404) {
      return (
        <div className="rounded-2xl bg-background p-8 text-center shadow-sm">
          <p className="text-sm text-gray-500">No patient record linked to this account.</p>
        </div>
      );
    }
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-sm">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-400" />
        <p className="text-sm text-red-700">
          {(error as { response?: { data?: { error?: string } } })?.response?.data?.error || "Failed to load patient information"}
        </p>
      </div>
    );
  }

  const diseaseType = getDiseaseType();
  const showDiseaseTab = diseaseType !== "other";
  // Keyed rather than positional: a host that cannot serve wearable uploads
  // drops that tab, and arithmetic indices would silently shift every tab after it.
  const TAB_LABELS: Record<string, string> = {
    general: "General",
    treatment: "Treatment",
    labs: "Labs",
    behavior: "Behavior",
    wearable: "Wearable",
    genomics: "Genomics",
    history: "History",
  };
  const tabKeys = [
    "general",
    ...(showDiseaseTab ? ["disease"] : []),
    "treatment", "labs", "behavior",
    ...(federated ? [] : ["wearable"]),
    "genomics", "history",
  ];
  const tabLabels = tabKeys.map((key) => key === "disease" ? getDiseaseTabLabel() : TAB_LABELS[key]);

  const diseaseIdx = tabKeys.indexOf("disease");
  const treatmentIdx = tabKeys.indexOf("treatment");
  const labsIdx = tabKeys.indexOf("labs");
  const behaviorIdx = tabKeys.indexOf("behavior");
  const wearableIdx = tabKeys.indexOf("wearable");
  const genomicsIdx = tabKeys.indexOf("genomics");
  const historyIdx = tabKeys.indexOf("history");

  const tabDescriptions: Record<number, string> = {
    0: "Keep patient details up to date for accurate personalisation.",
    ...(diseaseIdx >= 0 ? { [diseaseIdx]: "Disease-specific clinical information." } : {}),
    [treatmentIdx]: "Therapy history, treatment lines, and planned therapies.",
    [labsIdx]: "Blood counts, chemistry, liver function, coagulation, cardiac and tumour markers.",
    [behaviorIdx]: "Lifestyle, socioeconomic, and behavioural health factors.",
    [wearableIdx]: "Apple wearable 30-day summaries derived from synced OMOP data.",
    [genomicsIdx]: "Genes, variants, origins, interpretations, and test details.",
    [historyIdx]: "Clinical timeline with conditions, treatments, labs, and procedures.",
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        {showHeading && <h1 className="text-lg font-medium text-foreground/70">Health Profile</h1>}
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

      <div>
        <div className="pb-4 pt-2">
          <h2 className="text-base font-medium text-foreground/70">{tabLabels[activeTab]}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{tabDescriptions[activeTab]}</p>
        </div>

        <div>
          {activeTab === 0 && (
            <GeneralTab
              showCoordinates={!federated}
              conditionsReadOnly={readOnly}
              patientMode={federated}
              formData={editedInfo}
              onChange={handleFieldChange}
              editedName={editedName}
              onNameChange={handleNameChange}
              onZipcodeChange={handleZipcodeChange}
              onConditionsSaved={async (answer) => {
                for (const field of Object.keys(answer)) dirtyFieldsRef.current.delete(field);
                serverInfoRef.current = { ...serverInfoRef.current, ...answer };
                if (pendingDataRef.current) {
                  pendingDataRef.current = { ...pendingDataRef.current, ...answer };
                }
                editedInfoRef.current = { ...editedInfoRef.current, ...answer };
                setEditedInfo(previous => ({ ...previous, ...answer }));
                await commitConditionsAnswer(answer);
              }}
            />
          )}
          {activeTab === diseaseIdx && (
            <DiseaseTab
              formData={editedInfo}
              onChange={handleFieldChange}
              diseaseType={diseaseType}
            />
          )}
          {activeTab === treatmentIdx && (
            <TreatmentTab
              formData={editedInfo}
              onChange={handleFieldChange}
              diseaseType={diseaseType}
              readOnly={readOnly}
              onRecordRefreshed={(info) => {
                setEditedInfo(info);
                serverInfoRef.current = { ...info };
              }}
            />
          )}
          {activeTab === labsIdx && <LabsTab formData={editedInfo} onChange={handleFieldChange} />}
          {activeTab === behaviorIdx && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
          {wearableIdx >= 0 && activeTab === wearableIdx && <WearableTab formData={editedInfo} onChange={handleFieldChange} />}
          {activeTab === genomicsIdx && <GenomicsTab formData={editedInfo} readOnly={readOnly} />}
          {activeTab === historyIdx && <ClinicalSummaryTab formData={editedInfo} onNavigateToLabs={() => setActiveTab(labsIdx)} />}
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
  federated,
  onPatientUpdated,
  showHeading,
}: PatientInfoProps) {
  return (
    <PatientInfoProvider
      apiClient={apiClient}
      apiBasePath={apiBasePath}
      queryClient={queryClient}
      theme={theme}
      className={className}
    >
      <PatientInfoInner readOnly={readOnly} federated={federated} onPatientUpdated={onPatientUpdated} showHeading={showHeading} />
    </PatientInfoProvider>
  );
}

export default PatientInfo;
