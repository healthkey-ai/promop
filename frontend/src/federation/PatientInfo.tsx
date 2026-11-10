import { useState, useEffect, useRef, useCallback } from 'react';
import { AlertCircle } from 'lucide-react';
import { PatientInfoProvider } from './PatientInfoProvider';
import { PatientFormReadOnlyContext, usePatientFormStore, usePatientFormSnapshot } from './PatientFormContext';
import type { PatientInfoProps } from './patientInfoTypes';
import GeneralTab from '@/components/PatientInfo/tabs/GeneralTab';
import DiseaseTab from '@/components/PatientInfo/tabs/DiseaseTab';
import GenomicsTab from '@/components/PatientInfo/tabs/GenomicsTab';
import TreatmentTab from '@/components/PatientInfo/tabs/TreatmentTab';
import LabsTab from '@/components/PatientInfo/tabs/LabsTab';
import BehaviorTab from '@/components/PatientInfo/tabs/BehaviorTab';
import WearableTab from '@/components/PatientInfo/tabs/WearableTab';
import ClinicalSummaryTab from '@/components/PatientInfo/tabs/ClinicalSummaryTab';
import { federatedTabKeys, tabIndexFor } from './tabOrder';

function PatientInfoInner({ readOnly, federated, onPatientUpdated, showHeading = true }: Pick<PatientInfoProps,
  'readOnly' | 'federated' | 'onPatientUpdated' | 'showHeading'>) {
  const store = usePatientFormStore()!;
  const state = usePatientFormSnapshot(store);
  // A failed draft stays on screen over the saved value until it is retried
  // or edited again, the way dev kept the typed values after "Save failed".
  const editedInfo = { ...state.values, ...state.failed, ...state.pending };
  const failedDrafts = Object.keys(state.failed).length > 0;
  const editedName = String(editedInfo.patient_name ?? '');
  const [activeTab, setActiveTab] = useState(0);
  const deepLinkAppliedRef = useRef(false);
  useEffect(() => onPatientUpdated ? store.onUpdated(onPatientUpdated) : undefined, [store, onPatientUpdated]);
  const disease = String(editedInfo.disease ?? '').toLowerCase();
  const diseaseType = disease.includes('breast') ? 'breast' : disease.includes('mantle') ? 'mcl'
    : disease.includes('lymphoma') ? 'lymphoma' : disease.includes('myeloma') ? 'myeloma'
    : disease.includes('cll') || disease.includes('chronic lymphocytic') ? 'cll' : 'other';
  const showDiseaseTab = diseaseType !== 'other';
  const diseaseLabel = { breast: 'Breast Cancer', lymphoma: 'Follicular Lymphoma',
    myeloma: 'Multiple Myeloma', cll: 'CLL', mcl: 'Mantle Cell Lymphoma', other: 'Disease Specific' }[diseaseType];
  const tabKeys = federatedTabKeys(showDiseaseTab, federated);
  const labels: Record<string, string> = { general: 'General', disease: diseaseLabel, treatment: 'Treatment',
    labs: 'Labs', behavior: 'Behavior', wearable: 'Wearable', genomics: 'Genomics', history: 'History' };
  const tabKey = tabKeys[activeTab] ?? 'general';
  // Edits are off for a read-only mount and for a caller the server denies
  // writes. This gates the controls that write, one by one; navigation and
  // display controls inside the tabs stay usable.
  const locked = Boolean(readOnly) || !state.writable;
  useEffect(() => {
    if (deepLinkAppliedRef.current || state.loading) return;
    deepLinkAppliedRef.current = true;
    const requested = new URLSearchParams(window.location.search).get('tab');
    const index = tabIndexFor(requested, showDiseaseTab, federated);
    if (index >= 0) setActiveTab(index);
  }, [state.loading, showDiseaseTab, federated]);
  const handleFieldChange = useCallback((name: string, value: unknown) => {
    if (!locked) store.enqueue(name, value);
  }, [store, locked]);
  const handleZipcodeChange = useCallback(async (zipcode: string) => {
    handleFieldChange('postal_code', zipcode);
    if (!/^\d{5}$/.test(zipcode) || locked) return;
    try {
      const response = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
      if (!response.ok) return;
      const result = await response.json();
      // Do not apply an old lookup to a newer ZIP draft.
      const current = store.getSnapshot();
      const draft = 'postal_code' in current.pending ? current.pending.postal_code
        : 'postal_code' in current.failed ? current.failed.postal_code : current.values.postal_code;
      if (draft !== zipcode) return;
      const place = result.places?.[0];
      if (place) {
        handleFieldChange('city', place['place name']);
        if (place['state abbreviation']) handleFieldChange('region', place['state abbreviation']);
      }
    } catch { /* ZIP lookup is optional; the typed ZIP still saves. */ }
  }, [store, locked, handleFieldChange]);

  if (state.loading) return <div role="status">Loading patient information…</div>;
  if (state.error) return <div role="alert" className="rounded-2xl bg-background p-8 text-center">
    <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-400" /><p>{state.error}</p>
    <button onClick={() => void store.load()}>Retry</button></div>;
  return <div className="space-y-4">
    <div className="flex items-center justify-between">
      {showHeading && <h1 className="text-lg font-medium text-foreground/70">Health Profile</h1>}
      {(state.saving || Object.keys(state.pending).length > 0) && <span role="status" className="text-xs">Saving…</span>}
      {Object.keys(state.errors).length > 0 && <span role="alert" className="text-xs text-red-600">
        Some values could not be saved. Review the field errors.
        {failedDrafts && !locked && <>{' '}<button type="button" className="font-semibold underline"
          onClick={() => store.retryFailed()}>Save failed · Retry</button></>}
      </span>}
    </div>
    <div className="border-b border-border"><nav className="flex gap-x-1 overflow-x-auto" aria-label="Patient info tabs">
      {tabKeys.map((key, index) => <button key={key} onClick={() => setActiveTab(index)} className={[
        'whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium -mb-px transition-colors focus:outline-none',
        index === activeTab ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground',
      ].join(' ')}>{labels[key]}</button>)}
    </nav></div>
    <div><div className="pb-4 pt-2"><h2 className="text-base font-medium text-foreground/70">{labels[tabKey]}</h2></div>
      {/* Let tab-local scroll containers bound wide tables instead of allowing
          a min-content width to stretch the host page. */}
      <PatientFormReadOnlyContext.Provider value={locked}>
      <div className="min-w-0" data-patient-info-panel>
        {tabKey === 'general' && <GeneralTab showCoordinates={!federated} patientMode={federated}
          conditionsReadOnly={locked}
          onConditionsSaved={(answer) => {
            store.applyServerValues({ ...answer });
            // The attestation endpoint bypasses the save queue, so the host's
            // `onUpdated` listeners would never hear about it.
            store.announceUpdated();
          }}
          formData={editedInfo} onChange={handleFieldChange} editedName={editedName}
          onNameChange={(value) => handleFieldChange('patient_name', value)} onZipcodeChange={handleZipcodeChange} />}
        {tabKey === 'disease' && <DiseaseTab formData={editedInfo} onChange={handleFieldChange} diseaseType={diseaseType} />}
        {tabKey === 'treatment' && <TreatmentTab formData={editedInfo} onChange={handleFieldChange}
          diseaseType={diseaseType} readOnly={locked}
          // Therapy mutations bypass the store's queue: reload the record and
          // tell the host, or its trial matches keep the old therapy history.
          onRecordRefreshed={() => {
            // Hold the host listeners until the reload/notification lands: the
            // reader can leave the tab while it runs.
            store.beginBypassWrite();
            void store.load()
              .then(() => store.announceUpdated())
              .finally(() => store.endBypassWrite());
          }} />}
        {tabKey === 'labs' && <LabsTab formData={editedInfo} onChange={handleFieldChange} />}
        {tabKey === 'behavior' && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
        {tabKey === 'wearable' && <WearableTab formData={editedInfo} onChange={handleFieldChange} readOnly={locked} />}
        {tabKey === 'genomics' && <GenomicsTab formData={editedInfo} readOnly={locked}
          onChanged={() => {
            store.beginBypassWrite();
            void store.load()
              .then(() => store.announceUpdated())
              .finally(() => store.endBypassWrite());
          }} />}
        {tabKey === 'history' && <ClinicalSummaryTab formData={editedInfo} onNavigateToLabs={() => setActiveTab(tabKeys.indexOf('labs'))} />}
      </div>
      </PatientFormReadOnlyContext.Provider>
    </div>
  </div>;
}

export function PatientInfo({ apiClient, apiBasePath, queryClient, className, theme, ...props }: PatientInfoProps) {
  return <PatientInfoProvider apiClient={apiClient} apiBasePath={apiBasePath} queryClient={queryClient}
    className={className} theme={theme}><PatientInfoInner {...props} /></PatientInfoProvider>;
}
export default PatientInfo;
