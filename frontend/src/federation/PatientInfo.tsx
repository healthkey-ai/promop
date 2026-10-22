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

function PatientInfoInner({ readOnly, federated, onPatientUpdated, showHeading = true }: Pick<PatientInfoProps,
  'readOnly' | 'federated' | 'onPatientUpdated' | 'showHeading'>) {
  const store = usePatientFormStore()!;
  const state = usePatientFormSnapshot(store);
  const editedInfo = { ...state.values, ...state.pending };
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
  const tabKeys = ['general', ...(showDiseaseTab ? ['disease'] : []), 'treatment', 'labs', 'behavior',
    ...(federated ? [] : ['wearable']), 'genomics', 'history'];
  const labels: Record<string, string> = { general: 'General', disease: diseaseLabel, treatment: 'Treatment',
    labs: 'Labs', behavior: 'Behavior', wearable: 'Wearable', genomics: 'Genomics', history: 'History' };
  const tabKey = tabKeys[activeTab] ?? 'general';
  useEffect(() => {
    if (deepLinkAppliedRef.current || state.loading) return;
    deepLinkAppliedRef.current = true;
    const requested = new URLSearchParams(window.location.search).get('tab');
    const aliases: Record<string, string> = { blood: 'labs', breast_cancer: 'disease', multiple_myeloma: 'disease',
      follicular_lymphoma: 'disease', chronic_lymphocytic_leukemia: 'disease', mantle_cell_lymphoma: 'disease' };
    const keys = ['general', ...(showDiseaseTab ? ['disease'] : []), 'treatment', 'labs', 'behavior',
      ...(federated ? [] : ['wearable']), 'genomics', 'history'];
    const index = requested ? keys.indexOf(aliases[requested] ?? requested) : -1;
    if (index >= 0) setActiveTab(index);
  }, [state.loading, showDiseaseTab, federated]);
  const handleFieldChange = useCallback((name: string, value: unknown) => {
    if (!readOnly) store.enqueue(name, value);
  }, [store, readOnly]);
  const handleZipcodeChange = useCallback(async (zipcode: string) => {
    handleFieldChange('postal_code', zipcode);
    if (!/^\d{5}$/.test(zipcode) || readOnly) return;
    try {
      const response = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
      if (!response.ok) return;
      const result = await response.json();
      // Do not apply an old lookup to a newer ZIP draft.
      const current = store.getSnapshot();
      if ((current.pending.postal_code ?? current.values.postal_code) !== zipcode) return;
      const place = result.places?.[0];
      if (place) {
        handleFieldChange('city', place['place name']);
        if (place['state abbreviation']) handleFieldChange('region', place['state abbreviation']);
      }
    } catch { /* ZIP lookup is optional; the typed ZIP still saves. */ }
  }, [store, readOnly, handleFieldChange]);

  if (state.loading) return <div role="status">Loading patient information…</div>;
  if (state.error) return <div role="alert" className="rounded-2xl bg-background p-8 text-center">
    <AlertCircle className="mx-auto mb-3 h-10 w-10 text-red-400" /><p>{state.error}</p>
    <button onClick={() => void store.load()}>Retry</button></div>;
  return <div className="space-y-4">
    <div className="flex items-center justify-between">
      {showHeading && <h1 className="text-lg font-medium text-foreground/70">Health Profile</h1>}
      {(state.saving || Object.keys(state.pending).length > 0) && <span role="status" className="text-xs">Saving…</span>}
      {Object.keys(state.errors).length > 0 && <span role="alert" className="text-xs text-red-600">Some values could not be saved. Review the field errors.</span>}
    </div>
    <div className="border-b border-border"><nav className="flex gap-x-1 overflow-x-auto" aria-label="Patient info tabs">
      {tabKeys.map((key, index) => <button key={key} onClick={() => setActiveTab(index)} className={[
        'whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium -mb-px transition-colors focus:outline-none',
        index === activeTab ? 'border-primary text-primary' : 'border-transparent text-muted-foreground hover:text-foreground',
      ].join(' ')}>{labels[key]}</button>)}
    </nav></div>
    <div><div className="pb-4 pt-2"><h2 className="text-base font-medium text-foreground/70">{labels[tabKey]}</h2></div>
      <fieldset disabled={readOnly || !state.writable}>
        {tabKey === 'general' && <GeneralTab showCoordinates={!federated} patientMode={federated}
          formData={editedInfo} onChange={handleFieldChange} editedName={editedName}
          onNameChange={(value) => handleFieldChange('patient_name', value)} onZipcodeChange={handleZipcodeChange} />}
        {tabKey === 'disease' && <DiseaseTab formData={editedInfo} onChange={handleFieldChange} diseaseType={diseaseType} />}
        {tabKey === 'treatment' && <TreatmentTab formData={editedInfo} onChange={handleFieldChange}
          diseaseType={diseaseType} readOnly={readOnly} onRecordRefreshed={() => { void store.load(); }} />}
        {tabKey === 'labs' && <LabsTab formData={editedInfo} onChange={handleFieldChange} />}
        {tabKey === 'behavior' && <BehaviorTab formData={editedInfo} onChange={handleFieldChange} />}
        {tabKey === 'wearable' && <WearableTab formData={editedInfo} onChange={handleFieldChange} />}
        {tabKey === 'genomics' && <GenomicsTab formData={editedInfo} readOnly={readOnly} />}
        {tabKey === 'history' && <ClinicalSummaryTab formData={editedInfo} onNavigateToLabs={() => setActiveTab(tabKeys.indexOf('labs'))} />}
      </fieldset>
    </div>
  </div>;
}

export function PatientInfo({ apiClient, apiBasePath, queryClient, className, theme, ...props }: PatientInfoProps) {
  return <PatientInfoProvider apiClient={apiClient} apiBasePath={apiBasePath} queryClient={queryClient}
    className={className} theme={theme}><PatientFormReadOnlyContext.Provider value={Boolean(props.readOnly)}>
      <PatientInfoInner {...props} /></PatientFormReadOnlyContext.Provider></PatientInfoProvider>;
}
export default PatientInfo;
